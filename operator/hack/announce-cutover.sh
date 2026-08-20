#!/usr/bin/env bash
#
# Hand the FRRConfiguration from the backend's reconcile loop to the operator,
# in an order that never has two writers.
#
# There is no atomic step available here: the writer flag lives on a Deployment
# and the dry-run flag lives on a custom resource, so the switch is two API
# calls and two rollouts. Only one order is safe.
#
#   phase 1 — stop the backend writing.  Writers: 1 -> 0.
#             The existing FRRConfiguration stays exactly as it is, frr-k8s
#             keeps applying it, and the dataplane does not change. A window
#             with no writer is harmless; a window with two is two `router bgp`
#             blocks over one session, because frr-k8s merges every
#             configuration in its namespace.
#
#   phase 2 — take the object over.      Writers: 0 -> 1.
#
# Backwards for a rollback, for the same reason: dry-run first, then the
# backend.
#
# This script verifies and refuses; it does not flip the backend for you. That
# is a change to a released deployment and belongs to whoever owns it.
#
# Usage:
#   hack/announce-cutover.sh status
#   hack/announce-cutover.sh phase2 [--apply]
#   hack/announce-cutover.sh rollback [--apply]

set -euo pipefail

POLICY="${ANNOUNCE_POLICY:-default}"
BACKEND_NS="${BACKEND_NAMESPACE:-kubevirt-ui-system}"
BACKEND_DEPLOY="${BACKEND_DEPLOYMENT:-kubevirt-ui-backend}"

action="${1:-status}"
apply=false
[[ "${2:-}" == "--apply" ]] && apply=true

# backend_writer_on asks the running pods, not the Deployment.
#
# A flag set in a spec is an intention; a pod that has not been replaced yet is
# still writing every thirty seconds. Reading the spec alone would let phase 2
# start while the old pod was mid-rollout — which is exactly the two-writer
# window this whole ordering exists to avoid.
#
# Any running pod without the flag counts as writing, and a rollout still in
# progress counts as writing too.
backend_writer_on() {
  if ! kubectl rollout status "deploy/$BACKEND_DEPLOY" -n "$BACKEND_NS" --timeout=5s >/dev/null 2>&1; then
    return 0   # mid-rollout: some old pod is still around, and it writes
  fi

  local pods pod value any=false
  pods=$(kubectl get pods -n "$BACKEND_NS" \
           -l "$(kubectl get deploy "$BACKEND_DEPLOY" -n "$BACKEND_NS" \
                 -o jsonpath='{range .spec.selector.matchLabels}{"\n"}{end}' >/dev/null 2>&1; \
                 kubectl get deploy "$BACKEND_DEPLOY" -n "$BACKEND_NS" -o json \
                 | jq -r '.spec.selector.matchLabels | to_entries | map("\(.key)=\(.value)") | join(",")')" \
           --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)

  for pod in $pods; do
    any=true
    value=$(kubectl get pod "$pod" -n "$BACKEND_NS" -o json \
            | jq -r '.spec.containers[0].env[]? | select(.name=="OPERATOR_ANNOUNCE_ENABLED") | .value' || true)
    case "${value,,}" in
      1|true|yes|on) ;;          # this pod has stepped aside
      *) return 0 ;;             # this one is still writing
    esac
  done

  # No running pod at all is not proof of anything, so treat it as writing.
  $any || return 0
  return 1
}

policy_dry_run() {
  kubectl get announcementpolicy "$POLICY" -o jsonpath='{.spec.dryRun}' 2>/dev/null
}

target() {
  local ns name
  ns=$(kubectl get announcementpolicy "$POLICY" -o jsonpath='{.spec.targetNamespace}')
  name=$(kubectl get announcementpolicy "$POLICY" -o jsonpath='{.spec.configurationName}')
  echo "${ns:-metallb-system}/${name:-kubevirt-ui-b3}"
}

show_status() {
  local t; t=$(target)
  echo "policy:            $POLICY"
  echo "target object:     $t"
  if backend_writer_on; then
    echo "backend writer:    ON  (OPERATOR_ANNOUNCE_ENABLED unset or false)"
  else
    echo "backend writer:    off"
  fi
  echo "policy dryRun:     $(policy_dry_run)"
  echo
  echo "live object last written by:"
  kubectl get frrconfiguration "${t#*/}" -n "${t%/*}" -o json 2>/dev/null \
    | jq -r '.metadata.managedFields[]? | "  \(.manager)  \(.operation)  \(.time)"' || echo "  (absent)"
  echo
  echo "would the handover change anything?"
  if [[ "$(policy_dry_run)" != "true" ]]; then
    echo "  (only answerable while the policy is in dryRun)"
    return
  fi
  kubectl get announcementpolicy "$POLICY" -o jsonpath='{.status.renderedConfiguration}' > /tmp/announce-operator.conf
  kubectl get frrconfiguration "${t#*/}" -n "${t%/*}" -o jsonpath='{.spec.raw.rawConfig}' > /tmp/announce-live.conf 2>/dev/null || true
  if diff -u /tmp/announce-live.conf /tmp/announce-operator.conf; then
    echo "  identical — taking it over changes nothing"
  else
    echo "  ^^^ the operator would write something different; do not proceed until this is understood"
  fi
}

case "$action" in
  status)
    show_status
    ;;

  phase2)
    if backend_writer_on; then
      cat >&2 <<MSG
Refusing: the backend is still writing this object.

Phase 1 first, and it is not this script's to make — it changes a released
deployment:

  kubectl set env deploy/$BACKEND_DEPLOY -n $BACKEND_NS OPERATOR_ANNOUNCE_ENABLED=true
  kubectl rollout status deploy/$BACKEND_DEPLOY -n $BACKEND_NS

Then re-run this. Doing it the other way round puts two writers on one BGP
configuration, which frr-k8s merges into a single FRR — two router bgp blocks
over one session.
MSG
      exit 2
    fi
    show_status
    echo
    if ! $apply; then
      echo "Nothing changed. Re-run with --apply to take the object over."
      exit 0
    fi
    t=$(target); ns="${t%/*}"; name="${t#*/}"
    kubectl get frrconfiguration "$name" -n "$ns" -o jsonpath='{.spec.raw.rawConfig}' > /tmp/announce-before.conf 2>/dev/null || true

    kubectl patch announcementpolicy "$POLICY" --type=merge -p '{"spec":{"dryRun":false}}'
    echo "The operator now owns $t. Checking that the dataplane did not move…"

    # Adoption must be a no-op on the wire. Give the controller a moment, then
    # compare what is actually on the object — the comparison in status was a
    # prediction, this is the outcome.
    sleep 10
    kubectl get frrconfiguration "$name" -n "$ns" -o jsonpath='{.spec.raw.rawConfig}' > /tmp/announce-after.conf 2>/dev/null || true
    if diff -u /tmp/announce-before.conf /tmp/announce-after.conf; then
      echo "  unchanged — the handover moved ownership and nothing else."
    else
      cat >&2 <<MSG

  ^^^ the live configuration CHANGED on adoption. Roll back now:

      $0 rollback --apply

  and work out the difference before trying again.
MSG
      exit 1
    fi
    ;;

  rollback)
    if [[ "$(policy_dry_run)" != "true" ]]; then
      if ! $apply; then
        echo "Would put the policy back into dryRun. Re-run with --apply."
        exit 0
      fi
      t=$(target); ns="${t%/*}"; name="${t#*/}"
      kubectl get frrconfiguration "$name" -n "$ns" -o jsonpath='{.spec.raw.rawConfig}' > /tmp/announce-rollback-before.conf 2>/dev/null || true

      kubectl patch announcementpolicy "$POLICY" --type=merge -p '{"spec":{"dryRun":true}}'
      sleep 5
      kubectl get frrconfiguration "$name" -n "$ns" -o jsonpath='{.spec.raw.rawConfig}' > /tmp/announce-rollback-after.conf 2>/dev/null || true
      if ! diff -q /tmp/announce-rollback-before.conf /tmp/announce-rollback-after.conf >/dev/null; then
        echo "WARNING: the configuration changed while stepping back; inspect it before continuing." >&2
      fi
      echo "The operator has stopped writing, and the object it leaves behind is what frr-k8s keeps applying."
      echo "Confirm, then give it back to the backend:"
    else
      echo "The operator is already in dryRun. Give the object back to the backend:"
    fi
    echo "  kubectl set env deploy/$BACKEND_DEPLOY -n $BACKEND_NS OPERATOR_ANNOUNCE_ENABLED-"
    ;;

  *)
    echo "Usage: $0 {status|phase2|rollback} [--apply]" >&2
    exit 2
    ;;
esac

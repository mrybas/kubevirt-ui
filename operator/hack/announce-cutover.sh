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

backend_writer_on() {
  local value
  value=$(kubectl get deploy "$BACKEND_DEPLOY" -n "$BACKEND_NS" -o json 2>/dev/null \
          | jq -r '.spec.template.spec.containers[0].env[]? | select(.name=="OPERATOR_ANNOUNCE_ENABLED") | .value' || true)
  case "${value,,}" in
    1|true|yes|on) return 1 ;;   # operator owns them: the backend is not writing
    *)             return 0 ;;   # unset or false: the backend is still writing
  esac
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
    kubectl patch announcementpolicy "$POLICY" --type=merge -p '{"spec":{"dryRun":false}}'
    echo "The operator now owns $(target)."
    ;;

  rollback)
    if [[ "$(policy_dry_run)" != "true" ]]; then
      if ! $apply; then
        echo "Would put the policy back into dryRun. Re-run with --apply."
        exit 0
      fi
      kubectl patch announcementpolicy "$POLICY" --type=merge -p '{"spec":{"dryRun":true}}'
      echo "The operator has stopped writing. Confirm, then give it back to the backend:"
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

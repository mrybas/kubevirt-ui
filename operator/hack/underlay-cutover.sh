#!/usr/bin/env bash
#
# Hand the egress underlay from the backend's handlers to the operator, in an
# order that never has two writers.
#
# As with the announcements, there is no atomic step: the writer flag lives on a
# Deployment and the operator's own off-switch is an annotation on a custom
# resource. Two API calls, and only one order is safe.
#
#   phase 1 — stop the backend writing.  Writers: 1 -> 0.
#             The four fabric objects, the node label and the two DaemonSets
#             stay exactly as they are. A window with no writer costs a window
#             with no healing, which is what the last two years looked like
#             anyway. A window with two costs a fight over the link-watcher pod
#             template, and that one restarts every watcher in the cluster on
#             every pass.
#
#   phase 2 — take the objects over.     Writers: 0 -> 1.
#
# Backwards for a rollback: pause the underlays first, then the backend.
#
# What "paused" is, and is not: the annotation makes the operator a no-op. It
# does NOT detach the children. Deleting a ManagedUnderlay — paused or not —
# takes its external Subnet with it, and every gateway attached to that subnet.
# There is no step in this script that deletes one, and there should not be.
#
# This script verifies and refuses; it does not flip the backend for you. That
# is a change to a released deployment and belongs to whoever owns it.
#
# Usage:
#   hack/underlay-cutover.sh status
#   hack/underlay-cutover.sh phase2 [--apply]
#   hack/underlay-cutover.sh rollback [--apply]

set -euo pipefail

BACKEND_NS="${BACKEND_NAMESPACE:-kubevirt-ui-system}"
BACKEND_DEPLOY="${BACKEND_DEPLOYMENT:-kubevirt-ui-backend}"
PAUSED_ANNOTATION="platform.kubevirt-ui.io/paused"

action="${1:-status}"
apply=false
[[ "${2:-}" == "--apply" ]] && apply=true

underlays() {
  kubectl get managedunderlays -o jsonpath='{.items[*].metadata.name}' 2>/dev/null
}

# backend_writer_on asks the running pods, not the Deployment.
#
# A flag set in a spec is an intention; a pod that has not been replaced yet
# still heals the node label on every GET and still rebuilds the fabric on every
# POST. Reading the spec alone would let phase 2 start mid-rollout, which is the
# two-writer window this ordering exists to avoid.
backend_writer_on() {
  if ! kubectl rollout status "deploy/$BACKEND_DEPLOY" -n "$BACKEND_NS" --timeout=5s >/dev/null 2>&1; then
    return 0   # mid-rollout: an old pod is still around, and it writes
  fi

  local selector pods pod value any=false
  selector=$(kubectl get deploy "$BACKEND_DEPLOY" -n "$BACKEND_NS" -o json \
             | jq -r '.spec.selector.matchLabels | to_entries | map("\(.key)=\(.value)") | join(",")')
  pods=$(kubectl get pods -n "$BACKEND_NS" -l "$selector" \
           --field-selector=status.phase=Running \
           -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)

  for pod in $pods; do
    any=true
    value=$(kubectl get pod "$pod" -n "$BACKEND_NS" -o json \
            | jq -r '.spec.containers[0].env[]? | select(.name=="OPERATOR_UNDERLAY_ENABLED") | .value' || true)
    case "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes|on) ;;          # this pod has stepped aside
      *) return 0 ;;             # this one is still writing
    esac
  done

  # No running pod at all is not proof of anything, so treat it as writing.
  $any || return 0
  return 1
}

paused() {
  kubectl get managedunderlay "$1" \
    -o jsonpath="{.metadata.annotations.${PAUSED_ANNOTATION//./\\.}}" 2>/dev/null
}

any_active() {
  local u
  for u in $(underlays); do
    [[ "$(paused "$u")" == "true" ]] || return 0
  done
  return 1
}

show_status() {
  local u
  if backend_writer_on; then
    echo "backend writer:    ON  (OPERATOR_UNDERLAY_ENABLED unset or false)"
  else
    echo "backend writer:    off"
  fi
  echo
  printf '%-20s %-8s %-8s %-8s %s\n' UNDERLAY PAUSED FABRIC NODES HEALS
  for u in $(underlays); do
    printf '%-20s %-8s %-8s %-8s %s\n' \
      "$u" \
      "$(paused "$u" || true)" \
      "$(kubectl get managedunderlay "$u" -o jsonpath='{.status.conditions[?(@.type=="FabricReady")].status}')" \
      "$(kubectl get managedunderlay "$u" -o jsonpath='{.status.conditions[?(@.type=="NodesLabelled")].status}')" \
      "$(kubectl get managedunderlay "$u" -o jsonpath='{.status.labelHeals}')"
  done
}

# snapshot records the fields the operator renders, and nothing else. kube-ovn
# writes its own defaults into the specs of its own objects; including those
# would report a difference on every run that says nothing about this handover.
snapshot() {
  local out="$1" u
  : > "$out"
  for u in $(underlays); do
    local pn vlan sub ns
    pn=$(kubectl get managedunderlay "$u" -o jsonpath='{.spec.providerNetworkName}')
    vlan=$(kubectl get managedunderlay "$u" -o jsonpath='{.spec.vlanName}')
    sub=$(kubectl get managedunderlay "$u" -o jsonpath='{.spec.subnetName}')
    ns=$(kubectl get managedunderlay "$u" -o jsonpath='{.spec.kubeOVNNamespace}')
    {
      echo "### $u"
      kubectl get provider-network "$pn" -o json 2>/dev/null \
        | jq -Sc '{defaultInterface:.spec.defaultInterface, excludeNodes:.spec.excludeNodes}'
      kubectl get vlan "$vlan" -o json 2>/dev/null \
        | jq -Sc '{id:.spec.id, provider:.spec.provider}'
      kubectl get subnet "$sub" -o json 2>/dev/null \
        | jq -Sc '{cidrBlock:.spec.cidrBlock, gateway:.spec.gateway, vlan:.spec.vlan,
                   provider:.spec.provider, natOutgoing:.spec.natOutgoing,
                   disableGatewayCheck:.spec.disableGatewayCheck, excludeIps:.spec.excludeIps}'
      # Key order is not meaning: the two renderers serialise the same four
      # fields differently, and comparing the raw string would report a
      # difference that is not one.
      kubectl get net-attach-def "$sub" -n "$ns" -o json 2>/dev/null \
        | jq -Sc '.spec.config | fromjson'
    } >> "$out"
  done
  # Found by their marker label, not by name in a known namespace: the Cilium
  # one lives wherever Cilium does, which nothing here is told.
  local one
  for one in $(kubectl get ds -A -l kubevirt-ui.io/workaround=true \
        -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {end}' 2>/dev/null); do
    kubectl get ds "${one#*/}" -n "${one%/*}" -o json 2>/dev/null \
      | jq -Sc '{name:.metadata.name, template:.spec.template.spec}' >> "$out"
  done
  sort -u "$out" -o "$out"
}

case "$action" in
  status)
    show_status
    ;;

  phase2)
    if backend_writer_on; then
      cat >&2 <<MSG
Refusing: the backend still writes this fabric.

Phase 1 first, and it is not this script's to make — it changes a released
deployment:

  kubectl set env deploy/$BACKEND_DEPLOY -n $BACKEND_NS OPERATOR_UNDERLAY_ENABLED=true
  kubectl rollout status deploy/$BACKEND_DEPLOY -n $BACKEND_NS

Then re-run this. The other way round puts two writers on the link-watcher pod
template, and each disagreement restarts every watcher in the cluster.
MSG
      exit 2
    fi
    show_status
    echo
    if ! $apply; then
      echo "Nothing changed. Re-run with --apply to activate the underlays."
      exit 0
    fi

    snapshot /tmp/underlay-before.txt
    for u in $(underlays); do
      kubectl annotate managedunderlay "$u" "$PAUSED_ANNOTATION-" >/dev/null 2>&1 || true
    done
    echo "The operator now owns the fabric. Checking that nothing moved…"

    # Adoption must be a no-op. The comparison up to now was a prediction; this
    # is the outcome.
    sleep 20
    snapshot /tmp/underlay-after.txt
    if diff -u /tmp/underlay-before.txt /tmp/underlay-after.txt; then
      echo "  unchanged — the handover moved ownership and nothing else."
    else
      cat >&2 <<MSG

  ^^^ the live fabric CHANGED on adoption. Roll back now:

      $0 rollback --apply

  and work out the difference before trying again.
MSG
      exit 1
    fi
    ;;

  rollback)
    if any_active; then
      if ! $apply; then
        echo "Would pause every underlay. Re-run with --apply."
        exit 0
      fi
      snapshot /tmp/underlay-rollback-before.txt
      for u in $(underlays); do
        kubectl annotate managedunderlay "$u" "$PAUSED_ANNOTATION=true" --overwrite
      done
      sleep 5
      snapshot /tmp/underlay-rollback-after.txt
      if ! diff -q /tmp/underlay-rollback-before.txt /tmp/underlay-rollback-after.txt >/dev/null; then
        echo "WARNING: the fabric changed while stepping back; inspect it before continuing." >&2
      fi
      echo "The operator has stopped writing. The objects it leaves behind are the live fabric;"
      echo "nothing was detached and nothing was deleted."
      echo "Confirm, then give it back to the backend:"
    else
      echo "Every underlay is already paused. Give the fabric back to the backend:"
    fi
    echo "  kubectl set env deploy/$BACKEND_DEPLOY -n $BACKEND_NS OPERATOR_UNDERLAY_ENABLED-"
    ;;

  *)
    echo "Usage: $0 {status|phase2|rollback} [--apply]" >&2
    exit 2
    ;;
esac

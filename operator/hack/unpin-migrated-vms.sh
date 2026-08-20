#!/usr/bin/env bash
#
# Remove the hostname nodeSelector that the old migrate path welded onto VMs.
#
# That path set a nodeSelector to steer a live migration and nothing ever took
# it off, so every machine migrated through it is stuck on the node it last
# landed on. Operations do not do this — the target lives on the migration
# object — but machines migrated before the change are still pinned.
#
# Not done by a controller, and not done automatically. A nodeSelector on a
# machine is also how a person deliberately places one, and this cannot tell the
# two apart: it lists what it would change and waits for someone to agree.
#
# Usage:
#   hack/unpin-migrated-vms.sh <namespace>            # plan only
#   hack/unpin-migrated-vms.sh <namespace> --apply
#   hack/unpin-migrated-vms.sh --all-namespaces [--apply]

set -euo pipefail

NS_ARG=""
APPLY=false
ALL_NS=false
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --all-namespaces) ALL_NS=true ;;
    *) NS_ARG="$arg" ;;
  esac
done

if [[ -n "$NS_ARG" ]]; then
  scope=(-n "$NS_ARG")
elif $ALL_NS; then
  scope=(-A)
else
  echo "Name a namespace, or pass --all-namespaces deliberately." >&2
  exit 2
fi

vms=$(kubectl get virtualmachine "${scope[@]}" -o json | jq '{items: [.items[] | select(
        .spec.template.spec.nodeSelector["kubernetes.io/hostname"] != null)]}')
count=$(echo "$vms" | jq '.items | length')

if [[ "$count" == "0" ]]; then
  echo "No machines are pinned to a node."
  exit 0
fi

echo "Machines pinned to a node:"
echo
for i in $(seq 0 $((count - 1))); do
  vm=$(echo "$vms" | jq ".items[$i]")
  name=$(echo "$vm" | jq -r '.metadata.name')
  ns=$(echo "$vm" | jq -r '.metadata.namespace')
  node=$(echo "$vm" | jq -r '.spec.template.spec.nodeSelector["kubernetes.io/hostname"]')

  if $APPLY; then
    kubectl patch virtualmachine "$name" -n "$ns" --type=json \
      -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector/kubernetes.io~1hostname"}]' >/dev/null
    printf '  %-34s UNPINNED was %s (%s)\n' "$name" "$node" "$ns"
  else
    printf '  %-34s pinned to %s (%s)\n' "$name" "$node" "$ns"
  fi
done

echo
if $APPLY; then
  echo "Unpinned $count. They can be migrated again."
else
  echo "$count pinned. Nothing was changed. Re-run with --apply."
  echo "Check first that none of these were placed deliberately."
fi

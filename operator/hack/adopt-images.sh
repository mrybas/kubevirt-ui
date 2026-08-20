#!/usr/bin/env bash
#
# Bring existing golden disks under management as ManagedImage objects.
#
# The disks are not touched and not recreated: each new object carries an adopt
# annotation naming the DataVolume that is already there, and the controller
# takes ownership of it in place. Its spec is filled in from the disk itself, so
# what the object says and what exists agree from the first reconcile.
#
# Run this BEFORE migrating templates. A template's image reference resolves to
# a ManagedImage; migrating templates first turns working templates into ones
# that report ImageNotFound, because the image they name is still only a
# DataVolume.
#
# Deliberately per-object and deliberately manual: adoption is a decision about
# who owns a disk, and a reconciler that made it on its own would be arguing
# with whoever put the disk there.
#
# Usage:
#   hack/adopt-images.sh <namespace>                    # plan only
#   hack/adopt-images.sh <namespace> --apply            # write the objects
#   hack/adopt-images.sh --all-namespaces [--apply]
#
# A namespace is required unless --all-namespaces is given: adoption transfers
# ownership, and doing that cluster-wide by default is how someone else's disk
# ends up managed by a run that was meant to cover one team.
#
# Not adopted here, on purpose: the Talos golden in the system namespace. The
# tenant path creates and owns it as a deterministic singleton, and giving it a
# second owner is the class of bug this whole migration exists to remove.

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

adopted=0; existing=0; skipped=0

# A golden disk is one that no VirtualMachine owns.
#
# The first version of this filtered on the absence of our own vm-disk label,
# which is a convention rather than a fact: disks the tenant machinery creates
# through dataVolumeTemplates carry no such label, and the plan cheerfully
# offered to adopt four tenant worker root disks as golden images. An
# ownerReference is written by KubeVirt itself and says what the disk is for.
dvs=$(kubectl get datavolume "${scope[@]}" -l 'kubevirt-ui.io/managed=true' -o json \
      | jq '{items: [.items[] | select(
            ((.metadata.ownerReferences // []) | map(select(.kind == "VirtualMachine")) | length) == 0
          )]}')

count=$(echo "$dvs" | jq '.items | length')
if [[ "$count" == "0" ]]; then
  echo "No unmanaged golden disks found."
  exit 0
fi

echo "Golden disks:"
echo

for i in $(seq 0 $((count - 1))); do
  dv=$(echo "$dvs" | jq ".items[$i]")
  name=$(echo "$dv" | jq -r '.metadata.name')
  ns=$(echo "$dv" | jq -r '.metadata.namespace')

  if [[ "$ns" == "${SYSTEM_NAMESPACE:-kubevirt-ui-system}" ]]; then
    # The disks here belong to the platform itself — the Talos golden above
    # all, which the tenant path creates and owns as a deterministic
    # singleton. Giving it a second owner is the class of bug this migration
    # exists to remove.
    printf '  %-34s SKIP   platform-owned namespace\n' "$name"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "$(echo "$dv" | jq -r '.metadata.labels["platform.kubevirt-ui.io/owner-kind"] // ""')" == "ManagedImage" ]]; then
    printf '  %-34s OWNED  already adopted in %s\n' "$name" "$ns"
    existing=$((existing + 1))
    continue
  fi

  # The object is named after the disk, so every template and VM that already
  # references the disk by name keeps resolving.
  if kubectl get managedimage "$name" -n "$ns" >/dev/null 2>&1; then
    printf '  %-34s EXISTS a ManagedImage of that name is already in %s\n' "$name" "$ns"
    existing=$((existing + 1))
    continue
  fi

  size=$(echo "$dv" | jq -r '.spec.storage.resources.requests.storage // .spec.pvc.resources.requests.storage // ""')
  if [[ -z "$size" ]]; then
    printf '  %-34s SKIP   disk declares no size; adopt it by hand\n' "$name"
    skipped=$((skipped + 1))
    continue
  fi

  manifest=$(echo "$dv" | jq --arg name "$name" --arg ns "$ns" --arg size "$size" '{
    apiVersion: "platform.kubevirt-ui.io/v1alpha1",
    kind: "ManagedImage",
    metadata: {
      name: $name,
      namespace: $ns,
      annotations: {"platform.kubevirt-ui.io/adopt": $name},
    },
    spec: ({
      source: (
        if .spec.source.http then {http: {url: .spec.source.http.url}}
        elif .spec.source.registry then {registry: {url: (.spec.source.registry.url // "")}}
        elif .spec.source.pvc then {pvc: {name: .spec.source.pvc.name, namespace: .spec.source.pvc.namespace}}
        else {blank: {}} end),
      size: $size,
      scope: (.metadata.labels["kubevirt-ui.io/scope"] // "environment"),
      diskType: (.metadata.labels["kubevirt-ui.io/disk-type"] // "image"),
      persistent: ((.metadata.labels["kubevirt-ui.io/persistent"] // "false") == "true"),
    }
    + (if (.metadata.annotations["kubevirt-ui.io/display-name"] // "") != ""
       then {displayName: .metadata.annotations["kubevirt-ui.io/display-name"]} else {} end)
    + (if (.metadata.labels["kubevirt-ui.io/os-type"] // "") != ""
       then {osType: .metadata.labels["kubevirt-ui.io/os-type"]} else {} end)
    + (if (.spec.storage.storageClassName // "") != ""
       then {storageClass: .spec.storage.storageClassName} else {} end))
  }')

  if $APPLY; then
    echo "$manifest" | kubectl apply -f - >/dev/null
    printf '  %-34s ADOPTED in %s\n' "$name" "$ns"
  else
    printf '  %-34s WOULD ADOPT in %s\n' "$name" "$ns"
  fi
  adopted=$((adopted + 1))
done

echo
if $APPLY; then
  echo "Adopted $adopted, already managed $existing, skipped $skipped."
  echo "The disks were not recreated; check that each object reports Ready."
else
  echo "Would adopt $adopted, already managed $existing, skipped $skipped."
  echo "Nothing was written. Re-run with --apply."
fi

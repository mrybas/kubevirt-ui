#!/usr/bin/env bash
#
# Copy VM templates out of the shared ConfigMap into ManagedVMTemplate objects.
#
# Deliberately not automatic and deliberately not a controller. A reconciler
# that imports whatever it finds argues with whoever left it there; this is a
# thing a person runs, once, and reads the output of. It prints a plan by
# default and only writes with --apply.
#
# It never removes anything from the ConfigMap. Both stores are readable while
# the migration is in progress, so a half-finished run leaves every template
# usable; retiring the ConfigMap is a separate, deliberate step taken after the
# report says every entry has a counterpart.
#
# Usage:
#   hack/migrate-templates.sh            # plan only
#   hack/migrate-templates.sh --apply    # write the objects
#
# Requires kubectl and jq, and a kubeconfig pointing at the target cluster.

set -euo pipefail

APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true

CONFIGMAP="${TEMPLATE_CONFIGMAP:-kubevirt-ui-templates}"
CONFIGMAP_NS="${TEMPLATE_NAMESPACE:-kubevirt-ui-system}"

created=0; existing=0; skipped=0

if ! data=$(kubectl get configmap "$CONFIGMAP" -n "$CONFIGMAP_NS" -o json 2>/dev/null); then
  echo "No ConfigMap $CONFIGMAP_NS/$CONFIGMAP — nothing to migrate."
  exit 0
fi

keys=$(echo "$data" | jq -r '.data // {} | keys[]')
if [[ -z "$keys" ]]; then
  echo "ConfigMap $CONFIGMAP_NS/$CONFIGMAP holds no templates."
  exit 0
fi

echo "Templates in $CONFIGMAP_NS/$CONFIGMAP:"
echo

while IFS= read -r key; do
  tpl=$(echo "$data" | jq -r --arg k "$key" '.data[$k]')

  image=$(echo "$tpl" | jq -r '.golden_image_name // ""')
  image_ns=$(echo "$tpl" | jq -r '.golden_image_namespace // ""')

  # The namespace of the image is where the template goes, because that is
  # where it is usable from. A template that never named one cannot be placed,
  # and guessing would put someone's template in a namespace they cannot see.
  if [[ -z "$image" || -z "$image_ns" ]]; then
    printf '  %-28s SKIP   names no image namespace; place it by hand\n' "$key"
    skipped=$((skipped + 1))
    continue
  fi

  if kubectl get managedvmtemplate "$key" -n "$image_ns" >/dev/null 2>&1; then
    printf '  %-28s EXISTS already a resource in %s\n' "$key" "$image_ns"
    existing=$((existing + 1))
    continue
  fi

  manifest=$(echo "$tpl" | jq --arg name "$key" --arg ns "$image_ns" '{
    apiVersion: "platform.kubevirt-ui.io/v1alpha1",
    kind: "ManagedVMTemplate",
    metadata: {name: $name, namespace: $ns},
    spec: ({
      displayName: (.display_name // $name),
      imageRef: {name: .golden_image_name},
      compute: {
        cores: (.compute.cpu_cores // 2),
        sockets: (.compute.cpu_sockets // 1),
        threads: (.compute.cpu_threads // 1),
        memory: (.compute.memory // "4Gi"),
      },
      rootDisk: {size: (.disk.size // "20Gi")},
      category: (.category // "linux"),
      osType: (.os_type // "linux"),
      console: {
        vnc: (.console.vnc_enabled // true),
        serial: (.console.serial_console_enabled // false),
      },
    }
    + (if (.description // "") != "" then {description: .description} else {} end)
    + (if (.cloud_init.user_data // "") != "" then {cloudInit: {userData: .cloud_init.user_data}} else {} end))
  }')

  if $APPLY; then
    echo "$manifest" | kubectl apply -f - >/dev/null
    printf '  %-28s CREATED in %s\n' "$key" "$image_ns"
  else
    printf '  %-28s WOULD CREATE in %s (image %s)\n' "$key" "$image_ns" "$image"
  fi
  created=$((created + 1))
done <<< "$keys"

echo
if $APPLY; then
  echo "Created $created, already present $existing, skipped $skipped."
else
  echo "Would create $created, already present $existing, skipped $skipped."
  echo "Nothing was written. Re-run with --apply."
fi

if (( skipped > 0 )); then
  echo
  echo "Skipped entries stay in the ConfigMap and keep working; both stores are read."
fi

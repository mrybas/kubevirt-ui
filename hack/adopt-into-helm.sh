#!/usr/bin/env bash
# Let Helm take over cluster-scoped objects that already exist.
#
# Only for a cluster that ran the product before the chart carried the operator.
# A fresh install needs none of this: there is nothing to adopt.
#
# Helm refuses to manage an object it did not create unless the object says it
# belongs to the release, and the refusal is the whole install — one unadopted
# ClusterRole and nothing is upgraded. Measured on the stand: three tenant-role
# ClusterRoles applied by hand from chart 0.1.0 (they carry the Helm *labels*,
# which is what makes this confusing, but not the ownership annotations) and all
# nine CRDs, applied by kustomize during the operator's dev cycle.
#
# What it writes is metadata only. Nothing about what the objects do changes, and
# deleting the CRDs instead — the other way to make Helm happy — would
# cascade-delete every tenant, network and VM described by them.
#
#   hack/adopt-into-helm.sh <release> <namespace> [--apply]
set -euo pipefail

release="${1:?release name, e.g. kubevirt-ui}"
namespace="${2:?release namespace, e.g. kubevirt-ui-system}"
apply="${3:-}"

adopt() {
	local kind="$1" name="$2"
	local owner
	owner="$(kubectl get "$kind" "$name" \
		-o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
	if [ -n "$owner" ]; then
		printf '%-28s %-46s вже за релізом %s\n' "$kind" "$name" "$owner"
		return
	fi
	if ! kubectl get "$kind" "$name" >/dev/null 2>&1; then
		printf '%-28s %-46s немає — нічого усиновлювати\n' "$kind" "$name"
		return
	fi
	if [ "$apply" != "--apply" ]; then
		printf '%-28s %-46s УСИНОВИТИ (запусти з --apply)\n' "$kind" "$name"
		return
	fi
	kubectl annotate "$kind" "$name" \
		"meta.helm.sh/release-name=$release" \
		"meta.helm.sh/release-namespace=$namespace" --overwrite >/dev/null
	kubectl label "$kind" "$name" "app.kubernetes.io/managed-by=Helm" --overwrite >/dev/null
	printf '%-28s %-46s усиновлено\n' "$kind" "$name"
}

for crd in $(kubectl get crd -o name | grep 'platform.kubevirt-ui.io$'); do
	adopt crd "${crd#customresourcedefinition.apiextensions.k8s.io/}"
done
for role in kubevirt-ui-tenant-admin kubevirt-ui-tenant-editor kubevirt-ui-tenant-viewer; do
	adopt clusterrole "$role"
done

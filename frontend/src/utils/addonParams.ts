/**
 * Values an addon needs that nobody types.
 *
 * The tenant addon catalog marks each parameter `auto_discover` when its value
 * comes from scanning the cluster, and the wizard fills only those. But
 * `INFRA_CLUSTER_NAMESPACE` is not discovered — it is `tenant-<name>`, known
 * before the tenant exists — and the catalog ships it as
 * `auto_discover: false`. So ticking "KubeVirt CSI Driver" by hand posted an
 * empty namespace, and the driver was installed unable to provision anything:
 *
 *     ProvisioningFailed  rpc error: code = Unknown desc = an empty namespace
 *                         may not be set when a resource name is provided
 *
 * Derivation is separate from discovery, so it lives outside that loop.
 */

export const CSI_ADDON_ID = 'kubevirt-csi-driver';

/** The namespace a tenant's host-side resources live in. */
export function tenantNamespace(tenantName: string): string {
  return `tenant-${tenantName}`;
}

/**
 * Fills in what can be derived, leaving anything already set untouched.
 * Returns a new object; the input is not modified.
 */
export function withDerivedAddonParams(
  addonId: string,
  params: Record<string, string>,
  tenantName: string,
): Record<string, string> {
  const out = { ...params };
  if (addonId === CSI_ADDON_ID && !out['INFRA_CLUSTER_NAMESPACE'] && tenantName) {
    out['INFRA_CLUSTER_NAMESPACE'] = tenantNamespace(tenantName);
  }
  return out;
}

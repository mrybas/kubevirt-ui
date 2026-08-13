/**
 * Which VPCs a folder/environment may attach to.
 *
 * A VPC carries `kubevirt-ui.io/folder` and optionally
 * `kubevirt-ui.io/environment`. The tenant wizard filters on those; the VM
 * wizard did not, so creating a VM in `acme-dev` offered `beta-net` (another
 * folder) and `acme-prod-net` (another environment of the same folder) as if
 * they belonged to it.
 */

export interface VpcScope {
  folder?: string | null;
  environment?: string | null;
}

/**
 * `undefined` scope means the VPC is unknown to us — treat as global rather
 * than hiding a network the user may legitimately need.
 */
export function isVpcInScope(
  scope: VpcScope | undefined,
  folder: string | null | undefined,
  environment: string | null | undefined,
): boolean {
  if (!scope || !scope.folder) return true;      // global VPC
  if (scope.folder !== folder) return false;
  if (scope.environment && scope.environment !== environment) return false;
  return true;
}

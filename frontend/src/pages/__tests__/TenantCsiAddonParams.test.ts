/**
 * The wizard posted an empty infra namespace for a hand-ticked CSI addon.
 *
 * The deployed catalog declares the parameters as
 *
 *     INFRA_CLUSTER_NAMESPACE   auto_discover: false
 *     INFRA_STORAGE_CLASS_NAME  auto_discover: true
 *
 * and `handleSubmit` filled only those with `auto_discover` set — so the
 * storage class arrived and the namespace did not. The tenant's namespace is
 * not discovered from the cluster, it is `tenant-<name>`; each layer assumed
 * the other would supply it, and the driver was installed with "", failing
 * every CreateVolume with
 *
 *     an empty namespace may not be set when a resource name is provided
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, it, expect } from 'vitest';

import { tenantNamespace, withDerivedAddonParams } from '@/utils/addonParams';

describe('the CSI addon ticked by hand', () => {
  it('gets the tenant namespace, though the catalog does not call it discoverable', () => {
    const params = withDerivedAddonParams('kubevirt-csi-driver', {}, 'tstor2');

    expect(params['INFRA_CLUSTER_NAMESPACE']).toBe('tenant-tstor2');
  });

  it('never carries an empty namespace', () => {
    for (const given of [{}, { INFRA_CLUSTER_NAMESPACE: '' }]) {
      const params = withDerivedAddonParams('kubevirt-csi-driver', given, 't1');
      expect(params['INFRA_CLUSTER_NAMESPACE']).toBeTruthy();
    }
  });

  it('keeps a namespace the operator typed', () => {
    const params = withDerivedAddonParams(
      'kubevirt-csi-driver',
      { INFRA_CLUSTER_NAMESPACE: 'somewhere-else' },
      'tstor2',
    );

    expect(params['INFRA_CLUSTER_NAMESPACE']).toBe('somewhere-else');
  });

  it('does not mutate what it was given', () => {
    const given: Record<string, string> = {};

    withDerivedAddonParams('kubevirt-csi-driver', given, 'tstor2');

    expect(given).toEqual({});
  });

  it('leaves other addons alone', () => {
    const params = withDerivedAddonParams('calico', {}, 'tstor2');

    expect(params['INFRA_CLUSTER_NAMESPACE']).toBeUndefined();
  });

  it('matches the namespace the backend derives', () => {
    // backend: `_tenant_ns(name)` → f"tenant-{name}"
    expect(tenantNamespace('tstor2')).toBe('tenant-tstor2');
  });
});


describe('the wizard actually applies it', () => {
  // A helper nobody calls fixes nothing: the bug was in the wizard's submit
  // path, not in the arithmetic of building a namespace string.
  const src = readFileSync(resolve(__dirname, '../Tenants.tsx'), 'utf8');
  const submit = src.slice(src.indexOf('const handleSubmit'), src.indexOf('const request: TenantCreateRequest'));

  it('derives the parameters for every addon it posts', () => {
    expect(submit).toContain('withDerivedAddonParams(id, params, form.name)');
  });

  it('does so outside the auto_discover loop', () => {
    const call = submit.indexOf('withDerivedAddonParams');
    const loop = submit.indexOf('p.auto_discover');
    expect(call).toBeGreaterThan(-1);
    expect(loop).toBeGreaterThan(-1);
    expect(call).toBeLessThan(loop);
  });
});

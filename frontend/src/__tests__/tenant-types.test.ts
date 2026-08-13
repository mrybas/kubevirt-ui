/**
 * Unit tests for tenant type shapes and wizard logic.
 */
import { describe, it, expect } from 'vitest';
import type { TenantCreateRequest, Tenant } from '@/types/tenant';

// ---------------------------------------------------------------------------
// TenantCreateRequest shape
// ---------------------------------------------------------------------------

describe('TenantCreateRequest', () => {
  it('should accept valid VM tenant request', () => {
    const req: TenantCreateRequest = {
      name: 'my-tenant',
      display_name: 'My Tenant',
      kubernetes_version: 'v1.30.0',
      control_plane_replicas: 2,
      worker_type: 'vm',
      worker_count: 2,
      worker_vcpu: 2,
      worker_memory: '4Gi',
      worker_disk: '20Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      admin_group: '',
      viewer_group: '',
      addons: [],
    };

    expect(req.worker_type).toBe('vm');
    expect(req.worker_disk).toBe('20Gi');
  });

  it('should accept bare_metal worker type', () => {
    const req: TenantCreateRequest = {
      name: 'bm-tenant',
      display_name: 'BM Tenant',
      kubernetes_version: 'v1.31.0',
      control_plane_replicas: 1,
      worker_type: 'bare_metal',
      worker_count: 3,
      worker_vcpu: 4,
      worker_memory: '8Gi',
      worker_disk: '50Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      admin_group: '',
      viewer_group: '',
      addons: [],
    };

    expect(req.worker_type).toBe('bare_metal');
  });

  it('should accept vpc_name for explicit VPC selection (T8)', () => {
    const req: TenantCreateRequest = {
      name: 'vpc-tenant',
      display_name: 'VPC Tenant',
      kubernetes_version: 'v1.31.5',
      control_plane_replicas: 1,
      worker_type: 'vm',
      worker_count: 1,
      worker_vcpu: 2,
      worker_memory: '2Gi',
      worker_disk: '20Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      admin_group: 'admins',
      viewer_group: 'viewers',
      vpc_name: 'team-a-shared',
      folder: 'team-a',
      environment: 'prod',
      addons: [{ addon_id: 'calico', parameters: {} }],
    };

    expect(req.vpc_name).toBe('team-a-shared');
    expect(req.folder).toBe('team-a');
    expect(req.environment).toBe('prod');
    expect(req.addons).toHaveLength(1);
  });

  it('should allow vpc_name to be omitted for default cluster network (T8)', () => {
    const req: TenantCreateRequest = {
      name: 'default-net-tenant',
      display_name: 'Default Net Tenant',
      kubernetes_version: 'v1.32.1',
      control_plane_replicas: 1,
      worker_type: 'vm',
      worker_count: 1,
      worker_vcpu: 2,
      worker_memory: '2Gi',
      worker_disk: '20Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      admin_group: '',
      viewer_group: '',
      addons: [],
    };

    expect(req.vpc_name).toBeUndefined();
  });

  it('should not have worker_image_url field', () => {
    const req: TenantCreateRequest = {
      name: 'test',
      display_name: 'Test',
      kubernetes_version: 'v1.30.0',
      control_plane_replicas: 1,
      worker_type: 'vm',
      worker_count: 1,
      worker_vcpu: 1,
      worker_memory: '2Gi',
      worker_disk: '20Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      admin_group: '',
      viewer_group: '',
      addons: [],
    };

    // Image URL fields should not exist — container disk is hardcoded in backend
    expect((req as any).worker_image_url).toBeUndefined();
    expect((req as any).worker_image_source_type).toBeUndefined();
    expect((req as any).worker_image_size).toBeUndefined();
    expect((req as any).worker_image_os_type).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Tenant response shape
// ---------------------------------------------------------------------------

describe('Tenant response type', () => {
  it('should include worker_type field', () => {
    const tenant: Tenant = {
      name: 'test-tenant',
      display_name: 'Test Tenant',
      namespace: 'tenant-test-tenant',
      kubernetes_version: 'v1.30.0',
      status: 'Ready',
      phase: 'Provisioned',
      endpoint: 'https://192.168.1.100:6443',
      control_plane_replicas: 2,
      control_plane_ready: true,
      worker_type: 'vm',
      worker_count: 2,
      workers_ready: 2,
      worker_vcpu: 2,
      worker_memory: '4Gi',
      pod_cidr: '10.244.0.0/16',
      service_cidr: '10.96.0.0/12',
      created: '2026-03-10T12:00:00Z',
      conditions: [],
      addons: [],
    };

    expect(tenant.worker_type).toBe('vm');
  });
});

/**
 * A Talos worker boots a raw disk image the backend imports over HTTP. The
 * wizard's worker image field holds a CAPK *container disk* reference, which
 * belongs to the cloud-init path only — sending it on the Talos path makes CDI
 * reject the DataVolume:
 *
 *   admission webhook "datavolume-validate.cdi.kubevirt.io" denied the request:
 *   spec.source Invalid source URL: quay.io/capk/ubuntu-2404-container-disk:v1.32.1
 *
 * and by then the tenant's Talos secrets and PKI are already written, so the
 * failure leaves half a tenant behind.
 */
describe('worker image is only sent on the cloud-init path', () => {
  function imageFields(form: { worker_image_url: string; worker_os: 'cloud-init' | 'talos' }) {
    return {
      ...(form.worker_image_url && form.worker_os === 'cloud-init'
        ? { worker_image_url: form.worker_image_url, worker_image_source_type: 'registry' as const }
        : {}),
    };
  }

  const CAPK = 'quay.io/capk/ubuntu-2404-container-disk:v1.32.1';

  it('carries the container disk for cloud-init workers', () => {
    expect(imageFields({ worker_image_url: CAPK, worker_os: 'cloud-init' })).toEqual({
      worker_image_url: CAPK,
      worker_image_source_type: 'registry',
    });
  });

  it('drops it for Talos workers', () => {
    expect(imageFields({ worker_image_url: CAPK, worker_os: 'talos' })).toEqual({});
  });

  it('sends nothing when the field is empty', () => {
    expect(imageFields({ worker_image_url: '', worker_os: 'cloud-init' })).toEqual({});
  });
});

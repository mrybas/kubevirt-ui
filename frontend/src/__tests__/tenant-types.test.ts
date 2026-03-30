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

  it('should accept network_isolation flag', () => {
    const req: TenantCreateRequest = {
      name: 'vpc-tenant',
      display_name: 'VPC Tenant',
      kubernetes_version: 'v1.30.0',
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
      network_isolation: true,
      addons: [{ addon_id: 'calico', parameters: {} }],
    };

    expect(req.network_isolation).toBe(true);
    expect(req.addons).toHaveLength(1);
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

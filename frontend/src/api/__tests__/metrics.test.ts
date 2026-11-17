/**
 * Non-admins get metrics by name; the backend adds the namespace selector.
 * No PromQL leaves the browser on these paths.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const apiRequest = vi.fn().mockResolvedValue({ status: 'success', data: { resultType: 'matrix', result: [] } });
vi.mock('../client', () => ({ apiRequest: (path: string) => apiRequest(path) }));

import { queryClusterRange, queryTopVMs, queryVMRange } from '../metrics';

describe('scoped metrics API', () => {
  beforeEach(() => apiRequest.mockClear());

  it('asks for one VM by namespace, name and metric', async () => {
    await queryVMRange('tenant-a', 'web-1', 'cpu', 0, 3600);
    expect(apiRequest).toHaveBeenCalledWith('/metrics/vms/tenant-a/web-1?metric=cpu&start=0&end=3600');
  });

  it('asks for the top VMs by metric only', async () => {
    await queryTopVMs('memory');
    expect(apiRequest).toHaveBeenCalledWith('/metrics/top?metric=memory');
  });

  it('asks for cluster metrics by name', async () => {
    await queryClusterRange('memory_total', 0, 60);
    expect(apiRequest).toHaveBeenCalledWith('/metrics/cluster?metric=memory_total&start=0&end=60');
  });
});

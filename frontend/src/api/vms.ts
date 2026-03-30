import { apiRequest } from './client';
import type {
  VM, VMListResponse, VMStatusResponse, VMCreateRequest, VMUpdateRequest,
  DiskDetailResponse, DiskResizeRequest,
  StopVMOptions, VMEvent, VMEventsResponse, AttachDiskRequest, HotplugCapabilities,
  VolumeSnapshotInfo, VMSnapshotInfo, VMInterfaceInfo, AddNICRequest, CloneVMRequest, ResizeVMRequest,
} from '@/types/vm';

export type {
  StopVMOptions, VMEvent, VMEventsResponse, AttachDiskRequest, HotplugCapabilities,
  VolumeSnapshotInfo, VMSnapshotInfo, VMInterfaceInfo, AddNICRequest, CloneVMRequest, ResizeVMRequest,
};

export async function listVMs(namespace?: string, page?: number, perPage?: number): Promise<VMListResponse> {
  const params = new URLSearchParams();
  if (page && page > 1) params.set('page', String(page));
  if (perPage) params.set('per_page', String(perPage));
  const query = params.toString() ? `?${params}` : '';
  if (namespace) {
    return apiRequest<VMListResponse>(`/namespaces/${namespace}/vms${query}`);
  }
  // If no namespace specified, use cluster-wide endpoint
  return apiRequest<VMListResponse>(`/vms${query}`);
}

export async function getVM(namespace: string, name: string): Promise<VM> {
  return apiRequest<VM>(`/namespaces/${namespace}/vms/${name}`);
}

export async function createVM(
  namespace: string,
  data: VMCreateRequest
): Promise<VM> {
  return apiRequest<VM>(`/namespaces/${namespace}/vms`, {
    method: 'POST',
    body: data,
  });
}

export async function updateVM(
  namespace: string,
  name: string,
  data: VMUpdateRequest
): Promise<VM> {
  return apiRequest<VM>(`/namespaces/${namespace}/vms/${name}`, {
    method: 'PUT',
    body: data,
  });
}

export async function deleteVM(namespace: string, name: string): Promise<void> {
  await apiRequest<void>(`/namespaces/${namespace}/vms/${name}`, {
    method: 'DELETE',
  });
}

export async function startVM(
  namespace: string,
  name: string
): Promise<VMStatusResponse> {
  return apiRequest<VMStatusResponse>(`/namespaces/${namespace}/vms/${name}/start`, {
    method: 'POST',
  });
}

export async function stopVM(
  namespace: string,
  name: string,
  options?: StopVMOptions
): Promise<VMStatusResponse> {
  return apiRequest<VMStatusResponse>(`/namespaces/${namespace}/vms/${name}/stop`, {
    method: 'POST',
    body: {
      force: options?.force ?? false,
      grace_period: options?.gracePeriod ?? 120,
    },
  });
}

export async function restartVM(
  namespace: string,
  name: string
): Promise<VMStatusResponse> {
  return apiRequest<VMStatusResponse>(
    `/namespaces/${namespace}/vms/${name}/restart`,
    { method: 'POST' }
  );
}

export async function migrateVM(
  namespace: string,
  name: string,
  targetNode: string
): Promise<VMStatusResponse> {
  return apiRequest<VMStatusResponse>(
    `/namespaces/${namespace}/vms/${name}/migrate`,
    {
      method: 'POST',
      body: { target_node: targetNode },
    }
  );
}

export async function getVMYaml(
  namespace: string,
  name: string
): Promise<unknown> {
  return apiRequest<unknown>(`/namespaces/${namespace}/vms/${name}/yaml`);
}

export async function getVMEvents(
  namespace: string,
  name: string
): Promise<VMEventsResponse> {
  return apiRequest<VMEventsResponse>(`/namespaces/${namespace}/vms/${name}/events`);
}

export async function getVMDisks(
  namespace: string,
  name: string
): Promise<DiskDetailResponse[]> {
  return apiRequest<DiskDetailResponse[]>(`/namespaces/${namespace}/vms/${name}/disks`);
}

export async function getHotplugCapabilities(
  namespace: string
): Promise<HotplugCapabilities> {
  return apiRequest<HotplugCapabilities>(
    `/namespaces/${namespace}/vms/hotplug-capabilities`
  );
}

export async function attachDisk(
  namespace: string,
  vmName: string,
  data: AttachDiskRequest
): Promise<{ status: string; message: string }> {
  return apiRequest<{ status: string; message: string }>(
    `/namespaces/${namespace}/vms/${vmName}/attach-disk`,
    { method: 'POST', body: data }
  );
}

export async function resizeVMDisk(
  namespace: string,
  vmName: string,
  diskName: string,
  data: DiskResizeRequest
): Promise<DiskDetailResponse> {
  return apiRequest<DiskDetailResponse>(
    `/namespaces/${namespace}/vms/${vmName}/disks/${diskName}/resize`,
    { method: 'PUT', body: data }
  );
}

export async function detachDisk(
  namespace: string,
  diskName: string
): Promise<{ status: string; disk: string; vm: string; method: string }> {
  return apiRequest<{ status: string; disk: string; vm: string; method: string }>(
    `/namespaces/${namespace}/disks/${diskName}/detach`,
    { method: 'POST' }
  );
}

export async function listDiskSnapshots(
  namespace: string,
  pvcName: string
): Promise<VolumeSnapshotInfo[]> {
  return apiRequest<VolumeSnapshotInfo[]>(
    `/namespaces/${namespace}/disks/${pvcName}/snapshots`
  );
}

export async function createDiskSnapshot(
  namespace: string,
  pvcName: string,
  data: { snapshot_name: string; snapshot_class?: string }
): Promise<VolumeSnapshotInfo> {
  return apiRequest<VolumeSnapshotInfo>(
    `/namespaces/${namespace}/disks/${pvcName}/snapshots`,
    { method: 'POST', body: data }
  );
}

export async function deleteDiskSnapshot(
  namespace: string,
  snapshotName: string
): Promise<void> {
  await apiRequest<void>(
    `/namespaces/${namespace}/snapshots/${snapshotName}`,
    { method: 'DELETE' }
  );
}

export async function rollbackDiskSnapshot(
  namespace: string,
  snapshotName: string
): Promise<{ status: string; snapshot: string; pvc: string; vm: string; was_running: boolean }> {
  return apiRequest<{ status: string; snapshot: string; pvc: string; vm: string; was_running: boolean }>(
    `/namespaces/${namespace}/snapshots/${snapshotName}/rollback`,
    { method: 'POST' }
  );
}

// ==================== VM Snapshots (VirtualMachineSnapshot) ====================

export async function listVMSnapshots(
  namespace: string,
  vmName: string
): Promise<VMSnapshotInfo[]> {
  return apiRequest<VMSnapshotInfo[]>(
    `/namespaces/${namespace}/vms/${vmName}/snapshots`
  );
}

export async function createVMSnapshot(
  namespace: string,
  vmName: string,
  data: { snapshot_name: string }
): Promise<VMSnapshotInfo> {
  return apiRequest<VMSnapshotInfo>(
    `/namespaces/${namespace}/vms/${vmName}/snapshots`,
    { method: 'POST', body: data }
  );
}

export async function deleteVMSnapshot(
  namespace: string,
  vmName: string,
  snapshotName: string
): Promise<void> {
  await apiRequest<void>(
    `/namespaces/${namespace}/vms/${vmName}/snapshots/${snapshotName}`,
    { method: 'DELETE' }
  );
}

export async function restoreVMSnapshot(
  namespace: string,
  vmName: string,
  snapshotName: string
): Promise<{ status: string; vm: string; snapshot: string; restore: string; was_running: boolean }> {
  return apiRequest<{ status: string; vm: string; snapshot: string; restore: string; was_running: boolean }>(
    `/namespaces/${namespace}/vms/${vmName}/snapshots/${snapshotName}/restore`,
    { method: 'POST', body: {} }
  );
}

// ==================== VM Recreate ====================

export async function recreateVM(
  namespace: string,
  name: string
): Promise<{ status: string; vm: string; root_disk: string; golden_image: string; was_running: boolean; message: string }> {
  return apiRequest<{ status: string; vm: string; root_disk: string; golden_image: string; was_running: boolean; message: string }>(
    `/namespaces/${namespace}/vms/${name}/recreate`,
    { method: 'POST' }
  );
}

// ==================== NIC Hotplug ====================

export async function listVMInterfaces(
  namespace: string,
  vmName: string
): Promise<VMInterfaceInfo[]> {
  return apiRequest<VMInterfaceInfo[]>(
    `/namespaces/${namespace}/vms/${vmName}/interfaces`
  );
}

export async function addVMInterface(
  namespace: string,
  vmName: string,
  data: AddNICRequest
): Promise<{ status: string; interface: string; network: string; binding: string }> {
  return apiRequest<{ status: string; interface: string; network: string; binding: string }>(
    `/namespaces/${namespace}/vms/${vmName}/interfaces`,
    { method: 'POST', body: data }
  );
}

export async function removeVMInterface(
  namespace: string,
  vmName: string,
  ifaceName: string
): Promise<{ status: string; interface: string; message: string }> {
  return apiRequest<{ status: string; interface: string; message: string }>(
    `/namespaces/${namespace}/vms/${vmName}/interfaces/${ifaceName}`,
    { method: 'DELETE' }
  );
}

// ==================== VM Clone ====================

export async function cloneVM(
  namespace: string,
  name: string,
  data: CloneVMRequest
): Promise<{ status: string; source: string; clone: string; start: boolean; volumes_cloned: string[] }> {
  return apiRequest<{ status: string; source: string; clone: string; start: boolean; volumes_cloned: string[] }>(
    `/namespaces/${namespace}/vms/${name}/clone`,
    { method: 'POST', body: data }
  );
}

// ==================== Capacity Resize ====================

export async function resizeVM(
  namespace: string,
  name: string,
  data: ResizeVMRequest
): Promise<{ status: string; vm: string; changes: Record<string, any>; needs_restart: boolean; message: string }> {
  return apiRequest<{ status: string; vm: string; changes: Record<string, any>; needs_restart: boolean; message: string }>(
    `/namespaces/${namespace}/vms/${name}/resize`,
    { method: 'PATCH', body: data }
  );
}

export async function saveDiskAsImage(
  namespace: string,
  pvcName: string,
  data: { image_name: string; display_name?: string }
): Promise<{ status: string; image_name: string; source_pvc: string; size: string }> {
  return apiRequest<{ status: string; image_name: string; source_pvc: string; size: string }>(
    `/namespaces/${namespace}/disks/${pvcName}/save-as-image`,
    { method: 'POST', body: data }
  );
}

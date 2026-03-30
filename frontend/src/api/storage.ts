import { apiRequest } from './client';
import type {
  DataVolume,
  DataVolumeCreateRequest,
  DataVolumeListResponse,
  PVCListResponse,
  StorageClassDetail,
  StorageClassListResponse,
} from '../types/storage';

export async function listDataVolumes(namespace: string): Promise<DataVolumeListResponse> {
  return apiRequest<DataVolumeListResponse>(
    `/namespaces/${namespace}/storage/datavolumes`
  );
}

export async function getDataVolume(namespace: string, name: string): Promise<DataVolume> {
  return apiRequest<DataVolume>(
    `/namespaces/${namespace}/storage/datavolumes/${name}`
  );
}

export async function createDataVolume(
  namespace: string,
  data: DataVolumeCreateRequest
): Promise<DataVolume> {
  return apiRequest<DataVolume>(
    `/namespaces/${namespace}/storage/datavolumes`,
    { method: 'POST', body: data }
  );
}

export async function deleteDataVolume(namespace: string, name: string): Promise<void> {
  await apiRequest<void>(
    `/namespaces/${namespace}/storage/datavolumes/${name}`,
    { method: 'DELETE' }
  );
}

export async function listPVCs(namespace: string): Promise<PVCListResponse> {
  return apiRequest<PVCListResponse>(
    `/namespaces/${namespace}/storage/pvcs`
  );
}

export async function listStorageClasses(): Promise<StorageClassListResponse> {
  return apiRequest<StorageClassListResponse>('/storage/storageclasses');
}

export async function listStorageClassDetails(): Promise<StorageClassDetail[]> {
  return apiRequest<StorageClassDetail[]>('/storage/storageclasses/details');
}

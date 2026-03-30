/** Velero Backup/Restore/Schedule/Storage API client */

import { apiRequest } from './client';
import type {
  VeleroBackup,
  VeleroSchedule,
  VeleroStorageLocation,
  VeleroRestore,
  CreateVeleroBackupRequest,
  CreateVeleroScheduleRequest,
  CreateVeleroRestoreRequest,
  CreateStorageLocationRequest,
} from '../types/velero';

// ── Backups ───────────────────────────────────────────────────────────────────

export async function listVeleroBackups(): Promise<VeleroBackup[]> {
  return apiRequest<VeleroBackup[]>('/velero/backups');
}

export async function createVeleroBackup(data: CreateVeleroBackupRequest): Promise<VeleroBackup> {
  return apiRequest<VeleroBackup>('/velero/backups', { method: 'POST', body: data });
}

export async function deleteVeleroBackup(name: string): Promise<void> {
  await apiRequest<void>(`/velero/backups/${name}`, { method: 'DELETE' });
}

export async function restoreVeleroBackup(
  backupName: string,
  data?: CreateVeleroRestoreRequest,
): Promise<VeleroRestore> {
  return apiRequest<VeleroRestore>(`/velero/backups/${backupName}/restore`, {
    method: 'POST',
    body: data ?? {},
  });
}

// ── Schedules ─────────────────────────────────────────────────────────────────

export async function listVeleroSchedules(): Promise<VeleroSchedule[]> {
  return apiRequest<VeleroSchedule[]>('/velero/schedules');
}

export async function createVeleroSchedule(data: CreateVeleroScheduleRequest): Promise<VeleroSchedule> {
  return apiRequest<VeleroSchedule>('/velero/schedules', { method: 'POST', body: data });
}

export async function deleteVeleroSchedule(name: string): Promise<void> {
  await apiRequest<void>(`/velero/schedules/${name}`, { method: 'DELETE' });
}

export async function patchVeleroSchedule(
  name: string,
  data: { paused?: boolean },
): Promise<VeleroSchedule> {
  return apiRequest<VeleroSchedule>(`/velero/schedules/${name}`, { method: 'PATCH', body: data });
}

// ── Storage ───────────────────────────────────────────────────────────────────

export async function listVeleroStorageLocations(): Promise<VeleroStorageLocation[]> {
  return apiRequest<VeleroStorageLocation[]>('/velero/storage');
}

export type { CreateStorageLocationRequest };

export async function createStorageLocation(data: CreateStorageLocationRequest): Promise<VeleroStorageLocation> {
  return apiRequest<VeleroStorageLocation>('/velero/storage', { method: 'POST', body: data });
}

export async function deleteStorageLocation(name: string): Promise<void> {
  await apiRequest<void>(`/velero/storage/${name}`, { method: 'DELETE' });
}

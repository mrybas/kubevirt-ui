/** React Query hooks for Velero backups/schedules/storage + cluster-wide VM snapshots */

import { useQuery, useMutation, useQueryClient, useQueries } from '@tanstack/react-query';
import {
  listVeleroBackups,
  createVeleroBackup,
  deleteVeleroBackup,
  restoreVeleroBackup,
  listVeleroSchedules,
  createVeleroSchedule,
  deleteVeleroSchedule,
  patchVeleroSchedule,
  listVeleroStorageLocations,
  createStorageLocation,
  deleteStorageLocation,
  type CreateStorageLocationRequest,
} from '../api/velero';
import { listVMs, listVMSnapshots } from '../api/vms';
import { listSchedules, createSchedule, deleteSchedule, updateSchedule } from '../api/schedules';
import type { ScheduleInfo, CreateScheduleRequest } from '../api/schedules';
import { notify } from '../store/notifications';
import type {
  CreateVeleroBackupRequest,
  CreateVeleroScheduleRequest,
  CreateVeleroRestoreRequest,
} from '../types/velero';
import type { VMSnapshotInfo } from '../types/vm';

// ── Backups ───────────────────────────────────────────────────────────────────

export function useVeleroBackups() {
  return useQuery({
    queryKey: ['velero-backups'],
    queryFn: listVeleroBackups,
    refetchInterval: 15000,
  });
}

export function useCreateVeleroBackup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateVeleroBackupRequest) => createVeleroBackup(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['velero-backups'] });
      notify.success('Backup started');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create backup');
    },
  });
}

export function useDeleteVeleroBackup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteVeleroBackup(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['velero-backups'] });
      notify.success('Backup deletion requested');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete backup');
    },
  });
}

export function useRestoreVeleroBackup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ backupName, data }: { backupName: string; data?: CreateVeleroRestoreRequest }) =>
      restoreVeleroBackup(backupName, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['velero-backups'] });
      notify.success('Restore initiated');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to restore backup');
    },
  });
}

// ── Velero Schedules ──────────────────────────────────────────────────────────

export function useVeleroSchedules() {
  return useQuery({
    queryKey: ['velero-schedules'],
    queryFn: listVeleroSchedules,
    refetchInterval: 30000,
  });
}

export function useCreateVeleroSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateVeleroScheduleRequest) => createVeleroSchedule(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['velero-schedules'] });
      notify.success('Schedule created');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create schedule');
    },
  });
}

export function useDeleteVeleroSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteVeleroSchedule(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['velero-schedules'] });
      notify.success('Schedule deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete schedule');
    },
  });
}

export function usePatchVeleroSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, paused }: { name: string; paused: boolean }) =>
      patchVeleroSchedule(name, { paused }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['velero-schedules'] });
      notify.success(vars.paused ? 'Schedule paused' : 'Schedule resumed');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to update schedule');
    },
  });
}

// ── Storage ───────────────────────────────────────────────────────────────────

export function useVeleroStorageLocations() {
  return useQuery({
    queryKey: ['velero-storage'],
    queryFn: listVeleroStorageLocations,
    refetchInterval: 60000,
  });
}

export function useCreateStorageLocation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateStorageLocationRequest) => createStorageLocation(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['velero-storage'] }),
  });
}

export function useDeleteStorageLocation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteStorageLocation(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['velero-storage'] }),
  });
}

// ── Cluster-wide VM Snapshots ─────────────────────────────────────────────────

export function useAllVMSnapshots() {
  // Step 1: load all VMs cluster-wide
  const vmsQuery = useQuery({
    queryKey: ['vms-for-snapshots'],
    queryFn: () => listVMs(),
    staleTime: 30000,
  });

  const vms = vmsQuery.data?.items ?? [];

  // Step 2: for each VM, load its snapshots in parallel
  const snapshotQueries = useQueries({
    queries: vms.map((vm) => ({
      queryKey: ['vm-snapshots', vm.namespace, vm.name],
      queryFn: () => listVMSnapshots(vm.namespace, vm.name),
      enabled: vms.length > 0,
      staleTime: 15000,
    })),
  });

  const snapshots: VMSnapshotInfo[] = snapshotQueries.flatMap((q) => q.data ?? []);
  const isLoading = vmsQuery.isLoading || snapshotQueries.some((q) => q.isLoading);

  const refetch = () => {
    vmsQuery.refetch();
    snapshotQueries.forEach((q) => q.refetch());
  };

  return { snapshots, isLoading, refetch };
}

// ── Cluster-wide VM Snapshot Schedules (CronJobs with action=snapshot) ────────

export type { ScheduleInfo, CreateScheduleRequest };

export function useAllSnapshotSchedules() {
  const vmsQuery = useQuery({
    queryKey: ['vms-for-snap-schedules'],
    queryFn: () => listVMs(),
    staleTime: 30000,
  });

  const namespaces = [...new Set((vmsQuery.data?.items ?? []).map((vm) => vm.namespace))];

  const scheduleQueries = useQueries({
    queries: namespaces.map((ns) => ({
      queryKey: ['schedules', ns],
      queryFn: () => listSchedules(ns),
      enabled: namespaces.length > 0,
      staleTime: 30000,
    })),
  });

  const schedules: ScheduleInfo[] = scheduleQueries
    .flatMap((q) => q.data ?? [])
    .filter((s) => s.action === 'snapshot');

  const isLoading = vmsQuery.isLoading || scheduleQueries.some((q) => q.isLoading);

  const refetch = () => {
    vmsQuery.refetch();
    scheduleQueries.forEach((q) => q.refetch());
  };

  return { schedules, isLoading, refetch };
}

export function useCreateSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ namespace, data }: { namespace: string; data: CreateScheduleRequest }) =>
      createSchedule(namespace, data),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', vars.namespace] });
      queryClient.invalidateQueries({ queryKey: ['vms-for-snap-schedules'] });
      notify.success('Snapshot schedule created');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to create snapshot schedule');
    },
  });
}

export function useDeleteSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      deleteSchedule(namespace, name),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', vars.namespace] });
      notify.success('Schedule deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete schedule');
    },
  });
}

export function usePatchSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      namespace,
      name,
      suspend,
    }: {
      namespace: string;
      name: string;
      suspend: boolean;
    }) => updateSchedule(namespace, name, { suspend }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', vars.namespace] });
      notify.success(vars.suspend ? 'Schedule suspended' : 'Schedule resumed');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to update schedule');
    },
  });
}

import { useQuery, useMutation, useQueryClient, Query } from '@tanstack/react-query';
import * as vmApi from '@/api/vms';
import type { VM, VMCreateRequest, VMUpdateRequest, DiskResizeRequest } from '@/types/vm';
import { notify } from '@/store/notifications';

export function useVMs(namespace?: string, page?: number, perPage?: number) {
  return useQuery({
    queryKey: ['vms', namespace || 'all', page, perPage],
    queryFn: () => vmApi.listVMs(namespace, page, perPage),
  });
}

// Transitional statuses/phases that require polling for status updates
const TRANSITIONAL_VALUES = ['Starting', 'Stopping', 'Migrating', 'Provisioning', 'Scheduling', 'Pending', 'WaitingForVolumeBinding'];

export function useVM(namespace: string, name: string) {
  return useQuery({
    queryKey: ['vm', namespace, name],
    queryFn: () => vmApi.getVM(namespace, name),
    enabled: !!namespace && !!name,
    // Auto-refresh every 3s when VM is in a transitional state
    refetchInterval: (query: Query<VM>) => {
      const vm = query.state.data;
      if (vm) {
        // Check both status and phase for transitional states
        const isTransitional = TRANSITIONAL_VALUES.includes(vm.status) || 
                               TRANSITIONAL_VALUES.includes(vm.phase || '');
        if (isTransitional) {
          return 3000;
        }
      }
      return false;
    },
  });
}

export function useCreateVM(namespace: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: VMCreateRequest) => vmApi.createVM(namespace, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
    },
  });
}

export function useUpdateVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name, data }: { namespace: string; name: string; data: VMUpdateRequest }) =>
      vmApi.updateVM(namespace, name, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
    },
  });
}

export function useDeleteVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      vmApi.deleteVM(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      notify.success(`VM "${variables.name}" deleted`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete VM');
    },
  });
}

export function useStartVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      vmApi.startVM(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      notify.success(`VM "${variables.name}" started`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to start VM');
    },
  });
}

export function useStopVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name, force }: { namespace: string; name: string; force?: boolean }) =>
      vmApi.stopVM(namespace, name, { force }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      notify.success(`VM "${variables.name}" stopped`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to stop VM');
    },
  });
}

export function useRestartVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      vmApi.restartVM(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      notify.success(`VM "${variables.name}" restarted`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to restart VM');
    },
  });
}

export function useMigrateVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name, targetNode }: { namespace: string; name: string; targetNode: string }) =>
      vmApi.migrateVM(namespace, name, targetNode),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      notify.success(`VM "${variables.name}" migration started`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to migrate VM');
    },
  });
}

export function useVMYaml(namespace: string, name: string) {
  return useQuery({
    queryKey: ['vm-yaml', namespace, name],
    queryFn: () => vmApi.getVMYaml(namespace, name),
    enabled: !!namespace && !!name,
  });
}

export function useVMEvents(namespace: string, name: string) {
  return useQuery({
    queryKey: ['vm-events', namespace, name],
    queryFn: () => vmApi.getVMEvents(namespace, name),
    enabled: !!namespace && !!name,
    refetchInterval: 15000,
  });
}

export function useVMDisks(namespace: string, name: string) {
  return useQuery({
    queryKey: ['vm-disks', namespace, name],
    queryFn: () => vmApi.getVMDisks(namespace, name),
    enabled: !!namespace && !!name,
  });
}

export function useHotplugCapabilities(namespace: string) {
  return useQuery({
    queryKey: ['hotplug-capabilities', namespace],
    queryFn: () => vmApi.getHotplugCapabilities(namespace),
    enabled: !!namespace,
    staleTime: 5 * 60 * 1000, // cache for 5 minutes
  });
}

export function useAttachVMDisk() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      data,
    }: {
      namespace: string;
      vmName: string;
      data: vmApi.AttachDiskRequest;
    }) => vmApi.attachDisk(namespace, vmName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-disks', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
    },
  });
}

export function useResizeVMDisk() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      diskName,
      data,
    }: {
      namespace: string;
      vmName: string;
      diskName: string;
      data: DiskResizeRequest;
    }) => vmApi.resizeVMDisk(namespace, vmName, diskName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-disks', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
    },
  });
}

export function useDetachVMDisk() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      diskName,
    }: {
      namespace: string;
      diskName: string;
      vmName: string;
    }) => vmApi.detachDisk(namespace, diskName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-disks', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
    },
  });
}

export function useDiskSnapshots(namespace: string, pvcName: string) {
  return useQuery({
    queryKey: ['disk-snapshots', namespace, pvcName],
    queryFn: () => vmApi.listDiskSnapshots(namespace, pvcName),
    enabled: !!namespace && !!pvcName,
  });
}

export function useCreateDiskSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      pvcName,
      data,
    }: {
      namespace: string;
      pvcName: string;
      data: { snapshot_name: string; snapshot_class?: string };
    }) => vmApi.createDiskSnapshot(namespace, pvcName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['disk-snapshots', variables.namespace, variables.pvcName] });
    },
  });
}

export function useDeleteDiskSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      snapshotName,
    }: {
      namespace: string;
      snapshotName: string;
      pvcName: string;
    }) => vmApi.deleteDiskSnapshot(namespace, snapshotName),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['disk-snapshots', variables.namespace, variables.pvcName] });
    },
  });
}

export function useRollbackDiskSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      snapshotName,
    }: {
      namespace: string;
      snapshotName: string;
      pvcName: string;
    }) => vmApi.rollbackDiskSnapshot(namespace, snapshotName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['disk-snapshots', variables.namespace, variables.pvcName] });
      queryClient.invalidateQueries({ queryKey: ['vm-disks'] });
      queryClient.invalidateQueries({ queryKey: ['vm'] });
    },
  });
}

// ==================== VM Snapshots (VirtualMachineSnapshot) ====================

export function useVMSnapshots(namespace: string, vmName: string) {
  return useQuery({
    queryKey: ['vm-snapshots', namespace, vmName],
    queryFn: () => vmApi.listVMSnapshots(namespace, vmName),
    enabled: !!namespace && !!vmName,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data?.some((s: vmApi.VMSnapshotInfo) => !s.ready && s.phase === 'InProgress')) {
        return 5000;
      }
      return false;
    },
  });
}

export function useCreateVMSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      data,
    }: {
      namespace: string;
      vmName: string;
      data: { snapshot_name: string };
    }) => vmApi.createVMSnapshot(namespace, vmName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-snapshots', variables.namespace, variables.vmName] });
    },
  });
}

export function useDeleteVMSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      snapshotName,
    }: {
      namespace: string;
      vmName: string;
      snapshotName: string;
    }) => vmApi.deleteVMSnapshot(namespace, vmName, snapshotName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-snapshots', variables.namespace, variables.vmName] });
    },
  });
}

export function useRestoreVMSnapshot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      snapshotName,
    }: {
      namespace: string;
      vmName: string;
      snapshotName: string;
    }) => vmApi.restoreVMSnapshot(namespace, vmName, snapshotName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-snapshots', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vms'] });
    },
  });
}

// ==================== VM Recreate ====================

export function useRecreateVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      vmApi.recreateVM(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      queryClient.invalidateQueries({ queryKey: ['vm-disks', variables.namespace, variables.name] });
      notify.success(`VM "${variables.name}" recreated`);
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to recreate VM');
    },
  });
}

// ==================== NIC Hotplug ====================

export function useVMInterfaces(namespace: string, vmName: string) {
  return useQuery({
    queryKey: ['vm-interfaces', namespace, vmName],
    queryFn: () => vmApi.listVMInterfaces(namespace, vmName),
    enabled: !!namespace && !!vmName,
  });
}

export function useAddVMInterface() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      data,
    }: {
      namespace: string;
      vmName: string;
      data: vmApi.AddNICRequest;
    }) => vmApi.addVMInterface(namespace, vmName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-interfaces', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
    },
  });
}

export function useRemoveVMInterface() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      vmName,
      ifaceName,
    }: {
      namespace: string;
      vmName: string;
      ifaceName: string;
    }) => vmApi.removeVMInterface(namespace, vmName, ifaceName),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-interfaces', variables.namespace, variables.vmName] });
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.vmName] });
    },
  });
}

// ==================== VM Clone ====================

export function useCloneVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      name,
      data,
    }: {
      namespace: string;
      name: string;
      data: vmApi.CloneVMRequest;
    }) => vmApi.cloneVM(namespace, name, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vms'] });
    },
  });
}

// ==================== Capacity Resize ====================

export function useResizeVM() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      name,
      data,
    }: {
      namespace: string;
      name: string;
      data: vmApi.ResizeVMRequest;
    }) => vmApi.resizeVM(namespace, name, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm', variables.namespace, variables.name] });
      queryClient.invalidateQueries({ queryKey: ['vms'] });
    },
  });
}

export function useSaveDiskAsImage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      pvcName,
      data,
    }: {
      namespace: string;
      pvcName: string;
      data: { image_name: string; display_name?: string };
    }) => vmApi.saveDiskAsImage(namespace, pvcName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['vm-disks', variables.namespace] });
    },
  });
}

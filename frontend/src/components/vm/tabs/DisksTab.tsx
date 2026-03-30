import React, { useState, useEffect } from 'react';
import {
  HardDrive,
  Loader2,
  Check,
  X,
  Settings,
  Camera,
  Copy,
  Unlink,
  Trash2,
  RotateCcw,
} from 'lucide-react';
import {
  useVMDisks,
  useResizeVMDisk,
  useAttachVMDisk,
  useDetachVMDisk,
  useDiskSnapshots,
  useCreateDiskSnapshot,
  useDeleteDiskSnapshot,
  useRollbackDiskSnapshot,
  useSaveDiskAsImage,
  useHotplugCapabilities,
} from '@/hooks/useVMs';
import { listPVCs } from '@/api/storage';
import { CustomSelect } from '@/components/common/CustomSelect';

function SnapshotCountBadge({ namespace, pvcName }: { namespace: string; pvcName: string }) {
  const { data: snapshots } = useDiskSnapshots(namespace, pvcName);
  if (!snapshots || snapshots.length === 0) return null;
  return (
    <span className="bg-primary-500/20 text-primary-400 text-xs px-1.5 py-0.5 rounded-full font-medium" title={`${snapshots.length} snapshot(s)`}>
      {snapshots.length}
    </span>
  );
}

function DiskSnapshotsPanel({
  namespace, pvcName, createSnapshot, deleteSnapshot,
  snapshotName, setSnapshotName, snapshotError, setSnapshotError, formatPvcSize,
}: {
  namespace: string; pvcName: string;
  createSnapshot: any; deleteSnapshot: any;
  snapshotName: string; setSnapshotName: (v: string) => void;
  snapshotError: string; setSnapshotError: (v: string) => void;
  formatPvcSize: (s: string | null | undefined) => string;
}) {
  const { data: snapshots, isLoading } = useDiskSnapshots(namespace, pvcName);
  const rollback = useRollbackDiskSnapshot();
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [rollbackConfirm, setRollbackConfirm] = useState<string | null>(null);
  const [rollbackMsg, setRollbackMsg] = useState('');

  const handleCreate = () => {
    if (!snapshotName) return;
    setSnapshotError('');
    createSnapshot.mutate(
      { namespace, pvcName, data: { snapshot_name: snapshotName } },
      {
        onSuccess: () => setSnapshotName(''),
        onError: (err: any) => setSnapshotError(err?.message || 'Failed to create snapshot'),
      }
    );
  };

  const handleDelete = (name: string) => {
    deleteSnapshot.mutate(
      { namespace, snapshotName: name, pvcName },
      { onSuccess: () => setDeleteConfirm(null) }
    );
  };

  const handleRollback = (snapName: string) => {
    setRollbackMsg('');
    rollback.mutate(
      { namespace, snapshotName: snapName, pvcName },
      {
        onSuccess: (res: any) => {
          setRollbackConfirm(null);
          setRollbackMsg(`Rolled back to ${snapName}. ${res?.was_running ? 'VM is restarting.' : 'VM is stopped.'}`);
        },
        onError: (err: any) => {
          setRollbackConfirm(null);
          setRollbackMsg(`Rollback failed: ${err?.message || 'Unknown error'}`);
        },
      }
    );
  };

  return (
    <div className="bg-surface-900/50 border-t border-surface-700 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-surface-300 flex items-center gap-2">
          <Camera className="h-4 w-4" />
          Snapshots for <span className="font-mono text-surface-200">{pvcName}</span>
          {snapshots && snapshots.length > 0 && (
            <span className="bg-primary-500/20 text-primary-400 text-xs px-1.5 py-0.5 rounded-full">{snapshots.length}</span>
          )}
        </h4>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={snapshotName}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSnapshotName(e.target.value)}
            placeholder={`${pvcName}-snap`}
            className="input text-sm py-1 px-2 w-64"
            onKeyDown={(e: React.KeyboardEvent) => e.key === 'Enter' && handleCreate()}
          />
          <button
            onClick={handleCreate}
            disabled={!snapshotName || createSnapshot.isPending}
            className="btn-primary text-sm py-1 px-3"
          >
            {createSnapshot.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              'Create Snapshot'
            )}
          </button>
        </div>
      </div>

      {snapshotError && <p className="text-red-400 text-xs">{snapshotError}</p>}
      {rollbackMsg && (
        <div className={`px-3 py-2 rounded-lg text-xs flex items-center justify-between ${rollbackMsg.includes('failed') ? 'bg-red-500/10 text-red-400 border border-red-500/20' : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'}`}>
          <span>{rollbackMsg}</span>
          <button onClick={() => setRollbackMsg('')} className="btn-ghost p-0.5"><X className="h-3 w-3" /></button>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-4">
          <Loader2 className="h-5 w-5 mx-auto text-primary-400 animate-spin" />
        </div>
      ) : snapshots && snapshots.length > 0 ? (
        <div className="space-y-1">
          {snapshots.map((snap: any) => (
            <div key={snap.name} className="flex items-center justify-between bg-surface-800 rounded-lg px-3 py-2 text-sm">
              <div className="flex items-center gap-3">
                <Camera className="h-3.5 w-3.5 text-surface-500" />
                <span className="font-mono text-surface-200">{snap.name}</span>
                <span className={`px-1.5 py-0.5 rounded text-xs ${snap.ready ? 'text-emerald-400 bg-emerald-500/20' : 'text-amber-400 bg-amber-500/20'}`}>
                  {snap.ready ? 'Ready' : 'Pending'}
                </span>
                {snap.size && (
                  <span className="text-surface-500 text-xs">{formatPvcSize(snap.size)}</span>
                )}
                <span className="text-surface-500 text-xs">
                  {snap.creation_time ? new Date(snap.creation_time).toLocaleString() : ''}
                </span>
              </div>
              <div className="flex items-center gap-1">
                {snap.ready && (
                  rollbackConfirm === snap.name ? (
                    <div className="flex items-center gap-1 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-1">
                      <span className="text-amber-400 text-xs">Rollback? VM will restart</span>
                      <button
                        onClick={() => handleRollback(snap.name)}
                        disabled={rollback.isPending}
                        className="btn-ghost p-1 text-amber-400 hover:text-amber-300"
                        title="Confirm rollback"
                      >
                        {rollback.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                      </button>
                      <button onClick={() => setRollbackConfirm(null)} className="btn-ghost p-1" title="Cancel">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setRollbackConfirm(snap.name)}
                      className="btn-ghost p-1 text-surface-400 hover:text-amber-400"
                      title="Rollback disk to this snapshot"
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                    </button>
                  )
                )}
                {deleteConfirm === snap.name ? (
                  <div className="flex items-center gap-1">
                    <button onClick={() => handleDelete(snap.name)} className="btn-ghost p-1 text-red-400 hover:text-red-300" title="Confirm delete">
                      <Check className="h-3.5 w-3.5" />
                    </button>
                    <button onClick={() => setDeleteConfirm(null)} className="btn-ghost p-1" title="Cancel">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ) : (
                  <button onClick={() => setDeleteConfirm(snap.name)} className="btn-ghost p-1 text-surface-500 hover:text-red-400" title="Delete snapshot">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-surface-500 text-xs text-center py-2">No snapshots yet</p>
      )}
    </div>
  );
}

export function DisksTab({ vm }: { vm: any }) {
  const { data: disks, isLoading } = useVMDisks(vm.namespace, vm.name);
  const resizeDisk = useResizeVMDisk();
  const attachDisk = useAttachVMDisk();
  const detachDisk = useDetachVMDisk();
  const { data: hotplugCaps } = useHotplugCapabilities(vm.namespace);
  const createSnapshot = useCreateDiskSnapshot();
  const deleteSnapshot = useDeleteDiskSnapshot();
  const saveAsImage = useSaveDiskAsImage();
  const [saveImageDisk, setSaveImageDisk] = useState<string | null>(null);
  const [saveImageName, setSaveImageName] = useState('');
  const [saveImageMsg, setSaveImageMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [resizeModalDisk, setResizeModalDisk] = useState<any>(null);
  const [showAttachModal, setShowAttachModal] = useState(false);
  const [attachPvcName, setAttachPvcName] = useState('');
  const [attachDiskName, setAttachDiskName] = useState('');
  const [attachBus, setAttachBus] = useState('virtio');
  const [attachError, setAttachError] = useState('');
  const [sizeValue, setSizeValue] = useState(0);
  const [sizeUnit, setSizeUnit] = useState<'Mi' | 'Gi' | 'Ti'>('Gi');
  const [detachConfirm, setDetachConfirm] = useState<string | null>(null);
  const [detachMsg, setDetachMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [expandedSnapshots, setExpandedSnapshots] = useState<string | null>(null);
  const [snapshotName, setSnapshotName] = useState('');
  const [snapshotError, setSnapshotError] = useState('');

  // Fetch available PVCs in this namespace for the attach modal
  const [availablePVCs, setAvailablePVCs] = useState<any[]>([]);
  const [pvcSearch, setPvcSearch] = useState('');
  useEffect(() => {
    if (showAttachModal && vm.namespace) {
      listPVCs(vm.namespace).then(res => setAvailablePVCs(res.items || [])).catch(() => setAvailablePVCs([]));
      setPvcSearch('');
    }
  }, [showAttachModal, vm.namespace]);

  // Format PVC size: handle both K8s format ("50Gi") and raw bytes (53687091200)
  const formatPvcSize = (size: string | null | undefined): string => {
    if (!size) return '—';
    // If it already has a unit suffix (Gi, Mi, Ti, Ki), return as-is
    if (/\d+(\.\d+)?\s*(Ki|Mi|Gi|Ti|Pi)$/i.test(size)) return size;
    // Raw bytes
    const b = parseFloat(size);
    if (isNaN(b)) return size;
    const GB = 1024 ** 3;
    const TB = 1024 ** 4;
    const MB = 1024 ** 2;
    if (b >= TB) return `${(b / TB).toFixed(1)} Ti`;
    if (b >= GB) return `${(b / GB).toFixed(0)} Gi`;
    if (b >= MB) return `${(b / MB).toFixed(0)} Mi`;
    return `${b} B`;
  };

  // Get set of PVC names already attached to this VM
  const attachedPvcNames = new Set(
    (disks || [])
      .map((d: any) => d.pvc_name || d.dataVolumeName)
      .filter(Boolean)
  );

  const handleAttachDisk = () => {
    if (!attachPvcName || !attachDiskName) return;
    setAttachError('');
    attachDisk.mutate(
      {
        namespace: vm.namespace,
        vmName: vm.name,
        data: { disk_name: attachDiskName, pvc_name: attachPvcName, bus: attachBus },
      },
      {
        onSuccess: (res: any) => {
          setShowAttachModal(false);
          setAttachPvcName('');
          setAttachDiskName('');
          setAttachBus('virtio');
          if (res?.restart_required) {
            setDetachMsg({ type: 'success', text: 'Disk attached to VM spec. Restart the VM to apply changes (hotplug not available).' });
          }
        },
        onError: (err: any) => {
          setAttachError(err?.message || 'Failed to attach disk');
        },
      }
    );
  };

  const UNITS: Record<string, number> = { Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4 };

  const parseBytesToUnit = (bytes: number | string, unit: string) => {
    const b = typeof bytes === 'string' ? parseFloat(bytes) || 0 : bytes;
    return Math.round((b / UNITS[unit]!) * 100) / 100;
  };

  const formatBytes = (bytes: number | string) => {
    const b = typeof bytes === 'string' ? parseFloat(bytes) || 0 : bytes;
    if (b >= UNITS['Ti']!) return `${(b / UNITS['Ti']!).toFixed(1)} Ti`;
    if (b >= UNITS['Gi']!) return `${(b / UNITS['Gi']!).toFixed(1)} Gi`;
    if (b >= UNITS['Mi']!) return `${(b / UNITS['Mi']!).toFixed(0)} Mi`;
    return `${b} B`;
  };

  const currentBytes = resizeModalDisk ? (parseFloat(resizeModalDisk.size) || 0) : 0;
  const currentInUnit = parseBytesToUnit(currentBytes, sizeUnit);
  const newBytes = sizeValue * UNITS[sizeUnit]!;
  const sliderMin = Math.ceil(currentInUnit);
  const sliderMax = Math.max(sliderMin * 4, sizeUnit === 'Ti' ? 10 : sizeUnit === 'Gi' ? 2000 : 2048000);

  const getDiskTypeLabel = (type: string, isCloudinit: boolean) => {
    if (isCloudinit) return { label: 'Cloud-Init', color: 'text-purple-400 bg-purple-500/20' };
    switch (type) {
      case 'dataVolume':
        return { label: 'Data Volume', color: 'text-primary-400 bg-primary-500/20' };
      case 'persistentVolumeClaim':
        return { label: 'PVC', color: 'text-amber-400 bg-amber-500/20' };
      case 'containerDisk':
        return { label: 'Container', color: 'text-emerald-400 bg-emerald-500/20' };
      default:
        return { label: type, color: 'text-surface-400 bg-surface-700' };
    }
  };

  const handleResize = () => {
    if (!resizeModalDisk || sizeValue <= 0 || newBytes <= currentBytes) return;
    resizeDisk.mutate(
      {
        namespace: vm.namespace,
        vmName: vm.name,
        diskName: resizeModalDisk.name,
        data: { new_size: `${sizeValue}${sizeUnit}` },
      },
      {
        onSuccess: () => {
          setResizeModalDisk(null);
          setSizeValue(0);
        },
      }
    );
  };

  const openResizeModal = (disk: any) => {
    setResizeModalDisk(disk);
    const bytes = parseFloat(disk.size) || 0;
    // Pick best unit
    let unit: 'Mi' | 'Gi' | 'Ti' = 'Gi';
    if (bytes >= UNITS['Ti']!) unit = 'Ti';
    else if (bytes >= UNITS['Gi']!) unit = 'Gi';
    else unit = 'Mi';
    setSizeUnit(unit);
    // Set initial value to current size + a small bump
    const val = Math.ceil(parseBytesToUnit(bytes, unit));
    setSizeValue(val);
  };

  return (
    <div className="space-y-4">
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h3 className="font-display text-lg font-semibold">Attached Disks</h3>
          <button className="btn-secondary text-sm" onClick={() => setShowAttachModal(true)}>
            <HardDrive className="h-4 w-4" />
            Attach Disk
          </button>
        </div>
        {detachMsg && (
          <div className={`mx-4 mt-2 px-4 py-2 rounded-lg text-sm flex items-center justify-between ${detachMsg.type === 'success' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
            <span>{detachMsg.text}</span>
            <button onClick={() => setDetachMsg(null)} className="btn-ghost p-1"><X className="h-3.5 w-3.5" /></button>
          </div>
        )}
        {saveImageMsg && (
          <div className={`mx-4 mt-2 px-4 py-2 rounded-lg text-sm flex items-center justify-between ${saveImageMsg.type === 'success' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
            <span>{saveImageMsg.text}</span>
            <button onClick={() => setSaveImageMsg(null)} className="btn-ghost p-1"><X className="h-3.5 w-3.5" /></button>
          </div>
        )}
        <div className="overflow-x-auto">
          {isLoading ? (
            <div className="card-body text-center py-12">
              <Loader2 className="h-8 w-8 mx-auto text-primary-400 animate-spin mb-3" />
              <p className="text-surface-400">Loading disks...</p>
            </div>
          ) : disks && disks.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Size</th>
                  <th>Storage Class</th>
                  <th>Bus</th>
                  <th>Boot</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {disks.map((disk) => {
                  const typeInfo = getDiskTypeLabel(disk.type, disk.is_cloudinit);
                  return (
                    <React.Fragment key={disk.name}>
                    <tr>
                      <td className="font-medium text-surface-100">
                        <div className="flex items-center gap-2">
                          <HardDrive className="h-4 w-4 text-surface-500" />
                          <div>
                            <span>{disk.name}</span>
                            {disk.source_name && disk.source_name !== disk.name && (
                              <p className="text-xs text-surface-500 font-mono">{disk.source_name}</p>
                            )}
                          </div>
                        </div>
                      </td>
                      <td>
                        <span className={`px-2 py-1 rounded text-xs font-medium ${typeInfo.color}`}>
                          {typeInfo.label}
                        </span>
                      </td>
                      <td className="text-surface-300 font-mono">
                        {formatPvcSize(disk.size)}
                      </td>
                      <td className="text-surface-400 text-sm">
                        {disk.storage_class || '-'}
                      </td>
                      <td className="text-surface-400">{disk.bus}</td>
                      <td className="text-surface-400">
                        {disk.boot_order ? (
                          <span className="px-2 py-0.5 bg-emerald-500/20 text-emerald-400 rounded text-xs">
                            #{disk.boot_order}
                          </span>
                        ) : '-'}
                      </td>
                      <td>
                        {disk.status && (
                          <span className={`text-xs ${
                            disk.status === 'Bound' ? 'text-emerald-400' : 'text-amber-400'
                          }`}>
                            {disk.status}
                          </span>
                        )}
                      </td>
                      <td>
                        <div className="flex items-center gap-1">
                          {!disk.is_cloudinit && disk.source_name && (
                            <button
                              onClick={() => setExpandedSnapshots(expandedSnapshots === disk.name ? null : disk.name)}
                              className={`btn-ghost text-sm p-1.5 flex items-center gap-1 ${expandedSnapshots === disk.name ? 'text-primary-400' : ''}`}
                              title="Snapshots"
                            >
                              <Camera className="h-4 w-4" />
                              <SnapshotCountBadge namespace={vm.namespace} pvcName={disk.source_name} />
                            </button>
                          )}
                          {!disk.is_cloudinit && disk.source_name && (
                            saveImageDisk === disk.name ? (
                              <div className="flex items-center gap-1">
                                <input
                                  type="text"
                                  value={saveImageName}
                                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSaveImageName(e.target.value)}
                                  placeholder={`${disk.source_name}-image`}
                                  className="input text-xs py-0.5 px-2 w-40"
                                  onKeyDown={(e: React.KeyboardEvent) => {
                                    if (e.key === 'Enter' && saveImageName) {
                                      saveAsImage.mutate(
                                        { namespace: vm.namespace, pvcName: disk.source_name!, data: { image_name: saveImageName } },
                                        {
                                          onSuccess: () => { setSaveImageDisk(null); setSaveImageName(''); setSaveImageMsg({ type: 'success', text: `Image ${saveImageName} is being created.` }); },
                                          onError: (err: any) => setSaveImageMsg({ type: 'error', text: err?.message || 'Failed to save as image' }),
                                        }
                                      );
                                    }
                                    if (e.key === 'Escape') setSaveImageDisk(null);
                                  }}
                                  autoFocus
                                />
                                <button
                                  onClick={() => {
                                    if (!saveImageName) return;
                                    saveAsImage.mutate(
                                      { namespace: vm.namespace, pvcName: disk.source_name!, data: { image_name: saveImageName } },
                                      {
                                        onSuccess: () => { setSaveImageDisk(null); setSaveImageName(''); setSaveImageMsg({ type: 'success', text: `Image ${saveImageName} is being created.` }); },
                                        onError: (err: any) => setSaveImageMsg({ type: 'error', text: err?.message || 'Failed to save as image' }),
                                      }
                                    );
                                  }}
                                  disabled={!saveImageName || saveAsImage.isPending}
                                  className="btn-ghost p-1 text-emerald-400 hover:text-emerald-300"
                                  title="Confirm save as image"
                                >
                                  {saveAsImage.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                                </button>
                                <button onClick={() => setSaveImageDisk(null)} className="btn-ghost p-1" title="Cancel">
                                  <X className="h-4 w-4" />
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={() => { setSaveImageDisk(disk.name); setSaveImageName(`${disk.source_name}-image`); }}
                                className="btn-ghost text-sm p-1.5 text-surface-500 hover:text-emerald-400"
                                title="Save disk as image"
                              >
                                <Copy className="h-4 w-4" />
                              </button>
                            )
                          )}
                          {disk.can_resize && (
                            <button
                              onClick={() => openResizeModal(disk)}
                              className="btn-ghost text-sm p-1.5"
                              title="Resize disk"
                            >
                              <Settings className="h-4 w-4" />
                            </button>
                          )}
                          {!disk.is_cloudinit && !disk.boot_order && (
                            detachConfirm === disk.name ? (
                              <div className="flex items-center gap-1">
                                <button
                                  onClick={() => {
                                    const sourceName = disk.source_name || disk.name;
                                    setDetachMsg(null);
                                    detachDisk.mutate(
                                      { namespace: vm.namespace, diskName: sourceName, vmName: vm.name },
                                      {
                                        onSuccess: (res: any) => {
                                          setDetachConfirm(null);
                                          if (res?.method === 'spec-patch-restart-required') {
                                            setDetachMsg({ type: 'success', text: `Disk ${sourceName} detached from VM spec. Restart the VM to apply.` });
                                          } else {
                                            setDetachMsg({ type: 'success', text: `Disk ${sourceName} detached.` });
                                          }
                                        },
                                        onError: (err: any) => {
                                          setDetachConfirm(null);
                                          setDetachMsg({ type: 'error', text: err?.message || 'Failed to detach disk' });
                                        },
                                      }
                                    );
                                  }}
                                  className="btn-ghost text-sm p-1.5 text-red-400 hover:text-red-300"
                                  title="Confirm detach"
                                >
                                  <Check className="h-4 w-4" />
                                </button>
                                <button
                                  onClick={() => setDetachConfirm(null)}
                                  className="btn-ghost text-sm p-1.5"
                                  title="Cancel"
                                >
                                  <X className="h-4 w-4" />
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={() => setDetachConfirm(disk.name)}
                                className="btn-ghost text-sm p-1.5 text-surface-500 hover:text-red-400"
                                title="Detach disk"
                              >
                                <Unlink className="h-4 w-4" />
                              </button>
                            )
                          )}
                        </div>
                      </td>
                    </tr>
                    {/* Snapshots expandable row */}
                    {expandedSnapshots === disk.name && disk.source_name && (
                      <tr>
                        <td colSpan={8} className="p-0">
                          <DiskSnapshotsPanel
                            namespace={vm.namespace}
                            pvcName={disk.source_name}
                            createSnapshot={createSnapshot}
                            deleteSnapshot={deleteSnapshot}
                            snapshotName={snapshotName}
                            setSnapshotName={setSnapshotName}
                            snapshotError={snapshotError}
                            setSnapshotError={setSnapshotError}
                            formatPvcSize={formatPvcSize}
                          />
                        </td>
                      </tr>
                    )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="card-body text-center py-12">
              <HardDrive className="h-12 w-12 mx-auto text-surface-600 mb-3" />
              <p className="text-surface-400">No disks attached</p>
            </div>
          )}
        </div>
      </div>

      {/* Resize Modal */}
      {resizeModalDisk && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-surface-800 rounded-xl border border-surface-700 shadow-2xl max-w-lg w-full animate-fade-in">
            <div className="flex items-center justify-between p-6 border-b border-surface-700">
              <div>
                <h2 className="font-display text-xl font-bold text-surface-100">Resize Disk</h2>
                <p className="text-surface-400 text-sm mt-1">{resizeModalDisk.name}</p>
              </div>
              <button onClick={() => setResizeModalDisk(null)} className="btn-ghost p-2">
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="p-6 space-y-5">
              {/* Current size */}
              <div className="flex items-center justify-between bg-surface-900/50 rounded-lg p-4">
                <span className="text-sm text-surface-400">Current Size</span>
                <span className="text-surface-100 font-mono font-medium">
                  {formatBytes(resizeModalDisk.size)}
                </span>
              </div>

              {/* New size controls */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-3">
                  New Size
                </label>

                {/* Slider */}
                <div className="mb-4">
                  <input
                    type="range"
                    min={sliderMin}
                    max={sliderMax}
                    step={sizeUnit === 'Mi' ? 512 : 1}
                    value={sizeValue}
                    onChange={(e) => setSizeValue(parseFloat(e.target.value))}
                    className="w-full h-2 bg-surface-700 rounded-full appearance-none cursor-pointer
                               [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5
                               [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary-500
                               [&::-webkit-slider-thumb]:shadow-lg [&::-webkit-slider-thumb]:shadow-primary-500/30
                               [&::-webkit-slider-thumb]:cursor-pointer [&::-webkit-slider-thumb]:transition-transform
                               [&::-webkit-slider-thumb]:hover:scale-110"
                  />
                  <div className="flex justify-between text-xs text-surface-500 mt-1">
                    <span>{sliderMin} {sizeUnit}</span>
                    <span>{sliderMax} {sizeUnit}</span>
                  </div>
                </div>

                {/* Number input + unit selector */}
                <div className="flex gap-2">
                  <input
                    type="number"
                    min={sliderMin}
                    step={sizeUnit === 'Mi' ? 512 : 1}
                    value={sizeValue}
                    onChange={(e) => {
                      const v = parseFloat(e.target.value) || 0;
                      setSizeValue(v);
                    }}
                    className="input flex-1 font-mono text-lg"
                  />
                  <CustomSelect
                    value={sizeUnit}
                    onChange={(v) => {
                      const newUnit = v as 'Mi' | 'Gi' | 'Ti';
                      const bytes = sizeValue * (UNITS[sizeUnit] ?? 1);
                      setSizeUnit(newUnit);
                      setSizeValue(Math.round((bytes / (UNITS[newUnit] ?? 1)) * 100) / 100);
                    }}
                    className="w-20"
                    options={[
                      { value: 'Mi', label: 'Mi' },
                      { value: 'Gi', label: 'Gi' },
                      { value: 'Ti', label: 'Ti' },
                    ]}
                  />
                </div>
              </div>

              {/* Size change indicator */}
              {newBytes > currentBytes && (
                <div className="flex items-center gap-2 text-sm text-emerald-400 bg-emerald-500/10 rounded-lg px-4 py-2">
                  <span>+{formatBytes(newBytes - currentBytes)}</span>
                  <span className="text-surface-500">({formatBytes(currentBytes)} → {formatBytes(newBytes)})</span>
                </div>
              )}
              {newBytes > 0 && newBytes <= currentBytes && (
                <div className="bg-amber-500/10 border border-amber-500/50 rounded-lg px-4 py-2">
                  <p className="text-amber-400 text-sm">New size must be larger than current size.</p>
                </div>
              )}

              {resizeDisk.error && (
                <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4">
                  <p className="text-red-400 text-sm">{resizeDisk.error.message}</p>
                </div>
              )}

              <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
                <button onClick={() => setResizeModalDisk(null)} className="btn-secondary">
                  Cancel
                </button>
                <button
                  onClick={handleResize}
                  disabled={resizeDisk.isPending || newBytes <= currentBytes}
                  className="btn-primary"
                >
                  {resizeDisk.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Resizing...
                    </>
                  ) : (
                    `Resize to ${sizeValue} ${sizeUnit}`
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Attach Disk Modal */}
      {showAttachModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-surface-800 rounded-xl border border-surface-700 shadow-2xl max-w-lg w-full animate-fade-in">
            <div className="flex items-center justify-between p-6 border-b border-surface-700">
              <div>
                <h2 className="font-display text-xl font-bold text-surface-100">Attach Disk</h2>
                <p className="text-surface-400 text-sm mt-1">Attach an existing PVC to this VM</p>
              </div>
              <button onClick={() => { setShowAttachModal(false); setAttachError(''); }} className="btn-ghost p-2">
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="p-6 space-y-4">
              {vm.status === 'Running' && (
                <div className="bg-primary-500/10 border border-primary-500/50 rounded-lg px-4 py-2">
                  <p className="text-primary-400 text-sm">Disk will be hot-plugged to the running VM.</p>
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">PVC to attach *</label>
                {availablePVCs.length > 0 ? (
                  <div className="space-y-1.5">
                    <input
                      type="text"
                      value={pvcSearch}
                      onChange={(e) => setPvcSearch(e.target.value)}
                      placeholder="Search disks..."
                      className="input w-full text-sm"
                    />
                    <div className="max-h-48 overflow-y-auto rounded-lg border border-surface-700 bg-surface-900/50">
                      {availablePVCs
                        .filter((pvc: any) => !pvcSearch || pvc.name.toLowerCase().includes(pvcSearch.toLowerCase()))
                        .map((pvc: any) => {
                          const isPersistent = pvc.labels?.['kubevirt-ui.io/persistent'] === 'true';
                          const pvcAttachedTo = pvc.labels?.['kubevirt-ui.io/attached-to'];
                          const isAlreadyOnThisVM = attachedPvcNames.has(pvc.name);
                          const isAttachedElsewhere = isPersistent && pvcAttachedTo && pvcAttachedTo !== vm.name;
                          const isDisabled = isAlreadyOnThisVM || isAttachedElsewhere;
                          const isSelected = attachPvcName === pvc.name;

                          return (
                            <button
                              key={pvc.name}
                              type="button"
                              disabled={isDisabled}
                              onClick={() => {
                                setAttachPvcName(pvc.name);
                                if (!attachDiskName) setAttachDiskName(pvc.name);
                              }}
                              className={`w-full flex items-center justify-between px-3 py-2 text-left text-sm transition-colors border-b border-surface-700/50 last:border-b-0 ${
                                isDisabled
                                  ? 'opacity-40 cursor-not-allowed'
                                  : isSelected
                                    ? 'bg-primary-500/20 text-primary-300'
                                    : 'hover:bg-surface-700/50 text-surface-200'
                              }`}
                            >
                              <div className="flex items-center gap-2 min-w-0">
                                <HardDrive className="h-3.5 w-3.5 flex-shrink-0 text-surface-500" />
                                <span className="truncate font-medium">{pvc.name}</span>
                                {isPersistent && (
                                  <span className="flex-shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400">
                                    PERSISTENT
                                  </span>
                                )}
                                {isAlreadyOnThisVM && (
                                  <span className="flex-shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-surface-600 text-surface-400">
                                    ATTACHED
                                  </span>
                                )}
                                {isAttachedElsewhere && (
                                  <span className="flex-shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">
                                    ON {pvcAttachedTo}
                                  </span>
                                )}
                              </div>
                              <span className="flex-shrink-0 text-xs text-surface-400 ml-2">
                                {formatPvcSize(pvc.size)}
                              </span>
                            </button>
                          );
                        })}
                      {availablePVCs.filter((pvc: any) => !pvcSearch || pvc.name.toLowerCase().includes(pvcSearch.toLowerCase())).length === 0 && (
                        <p className="px-3 py-3 text-sm text-surface-500 text-center">No disks found</p>
                      )}
                    </div>
                  </div>
                ) : (
                  <input
                    type="text"
                    value={attachPvcName}
                    onChange={(e) => {
                      setAttachPvcName(e.target.value);
                      if (!attachDiskName) setAttachDiskName(e.target.value);
                    }}
                    placeholder="PVC name"
                    className="input w-full"
                  />
                )}
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Disk name in VM *</label>
                <input
                  type="text"
                  value={attachDiskName}
                  onChange={(e) => setAttachDiskName(e.target.value)}
                  placeholder="e.g. data-disk"
                  className="input w-full"
                />
                <p className="text-xs text-surface-500 mt-1">Internal name used in the VM spec</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Bus type</label>
                <CustomSelect
                  value={attachBus}
                  onChange={setAttachBus}
                  options={(hotplugCaps?.supported_bus_types || ['virtio', 'scsi', 'sata']).map((bus: string) => ({ value: bus, label: `${bus}${bus === 'virtio' ? ' (recommended)' : ''}` }))}
                />
                {vm.status === 'Running' && hotplugCaps && !hotplugCaps.declarative && (
                  <p className="text-xs text-amber-400 mt-1">Hotplug requires scsi bus. Virtio available after KubeVirt upgrade.</p>
                )}
              </div>

              {attachError && (
                <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4">
                  <p className="text-red-400 text-sm">{attachError}</p>
                </div>
              )}

              <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
                <button onClick={() => { setShowAttachModal(false); setAttachError(''); }} className="btn-secondary">
                  Cancel
                </button>
                <button
                  onClick={handleAttachDisk}
                  disabled={attachDisk.isPending || !attachPvcName || !attachDiskName}
                  className="btn-primary"
                >
                  {attachDisk.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Attaching...
                    </>
                  ) : (
                    'Attach Disk'
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

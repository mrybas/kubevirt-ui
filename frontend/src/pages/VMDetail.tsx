import { useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Play,
  Square,
  RotateCw,
  Monitor,
  HardDrive,
  Network,
  Activity,
  FileCode,
  Settings,
  Clock,
  AlertTriangle,
  Loader2,
  Copy,
  Pencil,
  X,
  Trash2,
  ChevronDown,
  Power,
  Zap,
  ArrowRightLeft,
  BarChart3,
  Camera,
  RotateCcw,
  CalendarClock,
  MoreVertical,
  RefreshCw,
} from 'lucide-react';
import { useVM, useStartVM, useStopVM, useRestartVM, useMigrateVM, useVMYaml, useUpdateVM, useDeleteVM, useRecreateVM, useCloneVM, useResizeVM } from '@/hooks/useVMs';
import { StatusBadge } from '@/components/common/StatusBadge';
import VMMetricsPanel from '@/components/charts/VMMetricsPanel';
import { OverviewTab, ConsoleTab, DisksTab, NetworkTab, EventsTab, YamlTab, SnapshotsTab, ScheduleTab } from '@/components/vm/tabs';
import { EditVMModal } from '@/components/vm/EditVMModal';
import { MigrateVMModal } from '@/components/vm/MigrateVMModal';
import { ConfirmDeleteModal } from '@/components/common/ConfirmDeleteModal';

type TabId = 'overview' | 'monitoring' | 'console' | 'disks' | 'snapshots' | 'network' | 'schedule' | 'events' | 'yaml';

const tabs: { id: TabId; label: string; icon: typeof Activity }[] = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'monitoring', label: 'Monitoring', icon: BarChart3 },
  { id: 'console', label: 'Console', icon: Monitor },
  { id: 'disks', label: 'Disks', icon: HardDrive },
  { id: 'snapshots', label: 'Snapshots', icon: Camera },
  { id: 'network', label: 'Network', icon: Network },
  { id: 'schedule', label: 'Schedule', icon: CalendarClock },
  { id: 'events', label: 'Events', icon: Clock },
  { id: 'yaml', label: 'YAML', icon: FileCode },
];

export function VMDetail() {
  const { namespace, name } = useParams<{ namespace: string; name: string }>();
  const navigate = useNavigate();
  const { data: vm, isLoading, error, refetch: refetchVM } = useVM(namespace!, name!);
  const { data: vmYaml } = useVMYaml(namespace!, name!);
  const startVM = useStartVM();
  const stopVM = useStopVM();
  const restartVM = useRestartVM();
  const updateVM = useUpdateVM();
  const deleteVM = useDeleteVM();
  const migrateVM = useMigrateVM();
  const recreateVM = useRecreateVM();
  const cloneVM = useCloneVM();
  const resizeVM = useResizeVM();
  
  const [activeTab, setActiveTab] = useState<TabId>('overview');
  const [isRecreateConfirmOpen, setIsRecreateConfirmOpen] = useState(false);
  const [isCloneModalOpen, setIsCloneModalOpen] = useState(false);
  const [cloneName, setCloneName] = useState('');
  const [cloneStart, setCloneStart] = useState(false);
  const [isResizeModalOpen, setIsResizeModalOpen] = useState(false);
  const [resizeCpu, setResizeCpu] = useState('');
  const [resizeMemory, setResizeMemory] = useState('');
  const [consoleType, setConsoleType] = useState<'vnc' | 'serial'>('vnc');
  const [copied, setCopied] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [isStopMenuOpen, setIsStopMenuOpen] = useState(false);
  const [isMoreMenuOpen, setIsMoreMenuOpen] = useState(false);
  const [isMigrateModalOpen, setIsMigrateModalOpen] = useState(false);
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false);
  
  const handleStop = (force: boolean) => {
    setIsStopMenuOpen(false);
    stopVM.mutate({ namespace: namespace!, name: name!, force });
  };
  
  const handleDeleteConfirm = () => {
    deleteVM.mutate(
      { namespace: namespace!, name: name! },
      { onSuccess: () => navigate('/vms') }
    );
    setIsDeleteModalOpen(false);
  };

  const copyYaml = () => {
    if (vmYaml) {
      navigator.clipboard.writeText(JSON.stringify(vmYaml, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
      </div>
    );
  }

  if (error || !vm) {
    return (
      <div className="card border-red-500/50">
        <div className="card-body text-center py-12">
          <AlertTriangle className="h-12 w-12 mx-auto text-red-400 mb-4" />
          <p className="text-red-400 text-lg font-medium">Failed to load virtual machine</p>
          <p className="text-surface-500 mt-2">{error?.message || 'VM not found'}</p>
          <Link to="/vms" className="btn-secondary mt-6">
            <ArrowLeft className="h-4 w-4" />
            Back to VMs
          </Link>
        </div>
      </div>
    );
  }

  const isRunning = vm.status === 'Running';
  const isStarting = ['Starting', 'Provisioning', 'Pending', 'Scheduling', 'Scheduled'].includes(vm.status);
  const isStopped = ['Stopped', 'Halted', 'Failed'].includes(vm.status);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link to="/vms" className="btn-ghost p-2">
            <ArrowLeft className="h-5 w-5" />
          </Link>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="font-display text-2xl font-bold text-surface-100">
                {vm.name}
              </h1>
              <StatusBadge status={vm.status} />
            </div>
            <p className="text-surface-400 mt-1">
              {vm.namespace} • {vm.node ? `Node: ${vm.node}` : 'Not scheduled'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setIsEditModalOpen(true)}
            className="btn-secondary"
          >
            <Pencil className="h-4 w-4" />
            Edit
          </button>
          {isStarting ? (
            <button className="btn-secondary" disabled>
              <Loader2 className="h-4 w-4 animate-spin" />
              Starting...
            </button>
          ) : isStopped ? (
            <button
              onClick={() => startVM.mutate({ namespace: vm.namespace, name: vm.name })}
              className="btn-primary"
              disabled={startVM.isPending}
            >
              {startVM.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              Start
            </button>
          ) : isRunning ? (
            <>
              {/* Shutdown dropdown button */}
              <div className="relative">
                <div className="flex">
                  <button
                    onClick={() => handleStop(false)}
                    className="btn-secondary rounded-r-none border-r-0"
                    disabled={stopVM.isPending}
                  >
                    {stopVM.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                    Shutdown
                  </button>
                  <button
                    onClick={() => setIsStopMenuOpen(!isStopMenuOpen)}
                    className="btn-secondary rounded-l-none px-2"
                    disabled={stopVM.isPending}
                  >
                    <ChevronDown className="h-4 w-4" />
                  </button>
                </div>
                {isStopMenuOpen && (
                  <>
                    <div 
                      className="fixed inset-0 z-10" 
                      onClick={() => setIsStopMenuOpen(false)} 
                    />
                    <div className="absolute right-0 mt-1 w-56 bg-surface-800 border border-surface-700 rounded-lg shadow-xl z-20">
                      <button
                        onClick={() => handleStop(false)}
                        className="w-full flex items-center gap-2 px-4 py-2.5 text-left text-sm text-surface-200 hover:bg-surface-700 rounded-t-lg"
                      >
                        <Power className="h-4 w-4 text-amber-400" />
                        <div>
                          <div className="flex items-center gap-2">
                            Graceful
                            <span className="text-xs bg-surface-600 px-1.5 py-0.5 rounded">default</span>
                          </div>
                          <div className="text-xs text-surface-500">ACPI shutdown, 2min timeout</div>
                        </div>
                      </button>
                      <button
                        onClick={() => handleStop(true)}
                        className="w-full flex items-center gap-2 px-4 py-2.5 text-left text-sm text-surface-200 hover:bg-surface-700 rounded-b-lg border-t border-surface-700"
                      >
                        <Zap className="h-4 w-4 text-red-400" />
                        <div>
                          <div>Force</div>
                          <div className="text-xs text-surface-500">Immediate, may lose data</div>
                        </div>
                      </button>
                    </div>
                  </>
                )}
              </div>
              <button
                onClick={() => restartVM.mutate({ namespace: vm.namespace, name: vm.name })}
                className="btn-secondary"
                disabled={restartVM.isPending}
              >
                {restartVM.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCw className="h-4 w-4" />}
                Restart
              </button>
            </>
          ) : null}
          <button onClick={() => refetchVM()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          {/* More Actions dropdown */}
          <div className="relative">
            <button
              onClick={() => setIsMoreMenuOpen(!isMoreMenuOpen)}
              className="btn-secondary"
            >
              <MoreVertical className="h-4 w-4" />
              More
              <ChevronDown className="h-3 w-3" />
            </button>
            {isMoreMenuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setIsMoreMenuOpen(false)} />
                <div className="absolute right-0 mt-1 w-52 bg-surface-800 border border-surface-700 rounded-lg shadow-xl z-20 py-1">
                  {isRunning && (
                    <button
                      onClick={() => { setIsMoreMenuOpen(false); setIsMigrateModalOpen(true); }}
                      className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-sm text-surface-200 hover:bg-surface-700"
                      disabled={migrateVM.isPending}
                    >
                      <ArrowRightLeft className="h-4 w-4 text-surface-400" />
                      Migrate
                    </button>
                  )}
                  <button
                    onClick={() => { setIsMoreMenuOpen(false); setCloneName(`${name}-clone`); setIsCloneModalOpen(true); }}
                    className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-sm text-surface-200 hover:bg-surface-700"
                    disabled={cloneVM.isPending}
                  >
                    <Copy className="h-4 w-4 text-surface-400" />
                    Clone
                  </button>
                  <button
                    onClick={() => { setIsMoreMenuOpen(false); setResizeCpu(String(vm.cpu_cores || '')); setResizeMemory(vm.memory || ''); setIsResizeModalOpen(true); }}
                    className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-sm text-surface-200 hover:bg-surface-700"
                  >
                    <Settings className="h-4 w-4 text-surface-400" />
                    Resize
                  </button>
                  <button
                    onClick={() => { setIsMoreMenuOpen(false); setIsRecreateConfirmOpen(true); }}
                    className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-sm text-amber-400 hover:bg-surface-700"
                    disabled={recreateVM.isPending}
                  >
                    <RotateCcw className="h-4 w-4" />
                    Recreate
                  </button>
                  <div className="border-t border-surface-700 my-1" />
                  <button
                    onClick={() => { setIsMoreMenuOpen(false); setIsDeleteModalOpen(true); }}
                    className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-sm text-red-400 hover:bg-surface-700"
                    disabled={deleteVM.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                    Delete
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Migrate VM Modal */}
      {isMigrateModalOpen && (
        <MigrateVMModal
          vmName={vm.name}
          currentNode={vm.node}
          onClose={() => setIsMigrateModalOpen(false)}
          onMigrate={(targetNode) => {
            migrateVM.mutate(
              { namespace: namespace!, name: name!, targetNode },
              { onSuccess: () => setIsMigrateModalOpen(false) }
            );
          }}
          isLoading={migrateVM.isPending}
          error={migrateVM.error?.message}
        />
      )}

      {/* Edit VM Modal */}
      {isEditModalOpen && (
        <EditVMModal
          vm={vm}
          onClose={() => setIsEditModalOpen(false)}
          onSave={(data) => {
            updateVM.mutate(
              { namespace: vm.namespace, name: vm.name, data },
              {
                onSuccess: () => setIsEditModalOpen(false),
              }
            );
          }}
          isLoading={updateVM.isPending}
          error={updateVM.error?.message}
        />
      )}

      {/* Recreate Confirm Modal */}
      {isRecreateConfirmOpen && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl max-w-md w-full mx-4">
            <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700">
              <h2 className="text-lg font-semibold text-surface-100">
                <RotateCcw className="w-5 h-5 inline mr-2 text-amber-400" />
                Recreate VM
              </h2>
              <button onClick={() => setIsRecreateConfirmOpen(false)} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="p-6 space-y-4">
              <p className="text-surface-300 text-sm">
                This will <span className="font-medium text-amber-400">destroy all data</span> on <span className="font-medium text-surface-100">{vm.name}</span>'s root disk and re-clone it from the golden image.
              </p>
              <div className="bg-surface-900/50 rounded-lg p-3 text-sm space-y-1">
                <div className="text-surface-400">Preserved: <span className="text-surface-200">VM name, network config (IP), SSH keys, cloud-init</span></div>
                <div className="text-surface-400">Destroyed: <span className="text-red-400">Root disk contents (OS, installed packages, user data)</span></div>
              </div>
              {recreateVM.error && (
                <div className="text-red-400 text-sm bg-red-500/10 rounded-lg p-3">
                  {recreateVM.error.message}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-3 px-6 py-4 border-t border-surface-700">
              <button onClick={() => setIsRecreateConfirmOpen(false)} className="btn-secondary">Cancel</button>
              <button
                onClick={() => {
                  recreateVM.mutate(
                    { namespace: namespace!, name: name! },
                    { onSuccess: () => setIsRecreateConfirmOpen(false) }
                  );
                }}
                className="btn-primary bg-amber-600 hover:bg-amber-500"
                disabled={recreateVM.isPending}
              >
                {recreateVM.isPending ? (
                  <><Loader2 className="h-4 w-4 animate-spin" /> Recreating...</>
                ) : (
                  <><RotateCcw className="h-4 w-4" /> Recreate VM</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Clone Modal */}
      {isCloneModalOpen && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl max-w-md w-full mx-4">
            <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700">
              <h2 className="text-lg font-semibold text-surface-100">
                <Copy className="w-5 h-5 inline mr-2 text-primary-400" />
                Clone VM
              </h2>
              <button onClick={() => setIsCloneModalOpen(false)} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="p-6 space-y-4">
              <div>
                <label className="block text-sm text-surface-400 mb-1">Clone Name</label>
                <input
                  type="text"
                  value={cloneName}
                  onChange={(e) => setCloneName(e.target.value)}
                  className="input w-full"
                  placeholder="e.g. my-vm-clone"
                />
              </div>
              <label className="flex items-center gap-2 text-sm text-surface-300 cursor-pointer">
                <input type="checkbox" checked={cloneStart} onChange={(e) => setCloneStart(e.target.checked)} className="rounded" />
                Start clone immediately
              </label>
              {cloneVM.error && (
                <div className="text-red-400 text-sm bg-red-500/10 rounded-lg p-3">{cloneVM.error.message}</div>
              )}
            </div>
            <div className="flex justify-end gap-3 px-6 py-4 border-t border-surface-700">
              <button onClick={() => setIsCloneModalOpen(false)} className="btn-secondary">Cancel</button>
              <button
                onClick={() => {
                  cloneVM.mutate(
                    { namespace: namespace!, name: name!, data: { new_name: cloneName, start: cloneStart } },
                    { onSuccess: () => setIsCloneModalOpen(false) }
                  );
                }}
                className="btn-primary"
                disabled={!cloneName.trim() || cloneVM.isPending}
              >
                {cloneVM.isPending ? (
                  <><Loader2 className="h-4 w-4 animate-spin" /> Cloning...</>
                ) : (
                  <><Copy className="h-4 w-4" /> Clone VM</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Resize Modal */}
      {isResizeModalOpen && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-surface-800 border border-surface-700 rounded-xl shadow-2xl max-w-md w-full mx-4">
            <div className="flex items-center justify-between px-6 py-4 border-b border-surface-700">
              <h2 className="text-lg font-semibold text-surface-100">
                <Settings className="w-5 h-5 inline mr-2 text-primary-400" />
                Resize VM
              </h2>
              <button onClick={() => setIsResizeModalOpen(false)} className="p-1.5 text-surface-400 hover:text-surface-200 hover:bg-surface-700 rounded-lg">
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="p-6 space-y-4">
              <div>
                <label className="block text-sm text-surface-400 mb-1">CPU Cores</label>
                <input
                  type="number"
                  min={1}
                  max={256}
                  value={resizeCpu}
                  onChange={(e) => setResizeCpu(e.target.value)}
                  className="input w-full"
                  placeholder="e.g. 4"
                />
              </div>
              <div>
                <label className="block text-sm text-surface-400 mb-1">Memory</label>
                <input
                  type="text"
                  value={resizeMemory}
                  onChange={(e) => setResizeMemory(e.target.value)}
                  className="input w-full"
                  placeholder="e.g. 8Gi"
                />
              </div>
              <div className="bg-surface-900/50 rounded-lg p-3 text-xs text-surface-400">
                If the VM is running, a restart may be required for changes to take effect.
              </div>
              {resizeVM.error && (
                <div className="text-red-400 text-sm bg-red-500/10 rounded-lg p-3">{resizeVM.error.message}</div>
              )}
            </div>
            <div className="flex justify-end gap-3 px-6 py-4 border-t border-surface-700">
              <button onClick={() => setIsResizeModalOpen(false)} className="btn-secondary">Cancel</button>
              <button
                onClick={() => {
                  const data: any = {};
                  if (resizeCpu) data.cpu_cores = parseInt(resizeCpu, 10);
                  if (resizeMemory) data.memory = resizeMemory;
                  resizeVM.mutate(
                    { namespace: namespace!, name: name!, data },
                    { onSuccess: () => setIsResizeModalOpen(false) }
                  );
                }}
                className="btn-primary"
                disabled={(!resizeCpu && !resizeMemory) || resizeVM.isPending}
              >
                {resizeVM.isPending ? (
                  <><Loader2 className="h-4 w-4 animate-spin" /> Resizing...</>
                ) : (
                  <><Settings className="h-4 w-4" /> Apply</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDeleteModal
        isOpen={isDeleteModalOpen}
        onClose={() => setIsDeleteModalOpen(false)}
        onConfirm={handleDeleteConfirm}
        resourceName={vm.name}
        resourceType="Virtual Machine"
        isDeleting={deleteVM.isPending}
      />

      {/* Tabs */}
      <div className="border-b border-surface-700">
        <div className="flex gap-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 font-medium transition-colors border-b-2 ${
                activeTab === tab.id
                  ? 'text-primary-400 border-primary-400'
                  : 'text-surface-400 border-transparent hover:text-surface-200'
              }`}
            >
              <tab.icon className="h-4 w-4" />
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div className="animate-fade-in">
        {activeTab === 'overview' && (
          <OverviewTab vm={vm} />
        )}

        {activeTab === 'monitoring' && (
          <VMMetricsPanel vmName={vm.name} namespace={vm.namespace} />
        )}
        
        {activeTab === 'console' && (
          <ConsoleTab
            vm={vm}
            consoleType={consoleType}
            setConsoleType={setConsoleType}
            isRunning={isRunning}
          />
        )}
        
        {activeTab === 'disks' && (
          <DisksTab vm={vm} />
        )}

        {activeTab === 'snapshots' && (
          <SnapshotsTab vm={vm} />
        )}
        
        {activeTab === 'network' && (
          <NetworkTab vm={vm} />
        )}

        {activeTab === 'schedule' && (
          <ScheduleTab vm={vm} />
        )}
        
        {activeTab === 'events' && (
          <EventsTab namespace={vm.namespace} name={vm.name} />
        )}
        
        {activeTab === 'yaml' && (
          <YamlTab vmYaml={vmYaml} onCopy={copyYaml} copied={copied} />
        )}
      </div>
    </div>
  );
}


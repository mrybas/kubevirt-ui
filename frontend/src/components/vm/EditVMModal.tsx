import React, { useState } from 'react';
import { X, Loader2, AlertTriangle, Monitor, Terminal } from 'lucide-react';
import { CustomSelect } from '@/components/common/CustomSelect';
import type { VMUpdateRequest } from '@/types/vm';

export function EditVMModal({
  vm,
  onClose,
  onSave,
  isLoading,
  error,
}: {
  vm: { 
    name: string; 
    namespace: string; 
    cpu_cores?: number; 
    memory?: string; 
    run_strategy?: string; 
    status: string;
    console?: { vnc_enabled: boolean; serial_console_enabled: boolean };
  };
  onClose: () => void;
  onSave: (data: VMUpdateRequest) => void;
  isLoading: boolean;
  error?: string;
}) {
  const [cpuCores, setCpuCores] = useState(vm.cpu_cores ?? 2);
  const [memoryValue, setMemoryValue] = useState(parseInt(vm.memory?.replace(/[^0-9]/g, '') ?? '2'));
  const [memoryUnit, setMemoryUnit] = useState(vm.memory?.includes('Gi') ? 'Gi' : 'Mi');
  const [runStrategy, setRunStrategy] = useState(vm.run_strategy ?? 'Always');
  
  // Console settings
  const [vncEnabled, setVncEnabled] = useState(vm.console?.vnc_enabled ?? true);
  const [serialEnabled, setSerialEnabled] = useState(vm.console?.serial_console_enabled ?? false);
  const consoleChanged = vncEnabled !== (vm.console?.vnc_enabled ?? true) || 
                         serialEnabled !== (vm.console?.serial_console_enabled ?? false);

  const isStopped = ['Stopped', 'Halted', 'Failed'].includes(vm.status);
  const canEditResources = isStopped;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const data: VMUpdateRequest = {
      run_strategy: runStrategy as VMUpdateRequest['run_strategy'],
    };
    
    if (canEditResources) {
      data.cpu_cores = cpuCores;
      data.memory = `${memoryValue}${memoryUnit}`;
      
      // Include console settings if changed (requires restart)
      if (consoleChanged) {
        data.console = {
          vnc_enabled: vncEnabled,
          serial_console_enabled: serialEnabled,
        };
      }
    }
    
    onSave(data);
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-surface-800 rounded-xl border border-surface-700 shadow-2xl max-w-md w-full animate-fade-in">
        <div className="flex items-center justify-between p-6 border-b border-surface-700">
          <div>
            <h2 className="font-display text-xl font-bold text-surface-100">Edit VM</h2>
            <p className="text-surface-400 text-sm mt-1">{vm.name}</p>
          </div>
          <button onClick={onClose} className="btn-ghost p-2">
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-6">
          {error && (
            <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {!canEditResources && (
            <div className="bg-amber-500/10 border border-amber-500/50 rounded-lg p-4">
              <p className="text-amber-400 text-sm">
                <AlertTriangle className="h-4 w-4 inline mr-2" />
                Stop the VM to change CPU and memory settings.
              </p>
            </div>
          )}

          {/* CPU */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">
              CPU Cores
            </label>
            <input
              type="number"
              min="1"
              max="128"
              value={cpuCores}
              onChange={(e) => setCpuCores(parseInt(e.target.value) || 1)}
              disabled={!canEditResources}
              className="input w-full disabled:opacity-50 disabled:cursor-not-allowed"
            />
          </div>

          {/* Memory */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">
              Memory
            </label>
            <div className="flex gap-2">
              <input
                type="number"
                min="1"
                value={memoryValue}
                onChange={(e) => setMemoryValue(parseInt(e.target.value) || 1)}
                disabled={!canEditResources}
                className="input flex-1 disabled:opacity-50 disabled:cursor-not-allowed"
              />
              <CustomSelect
                value={memoryUnit}
                onChange={setMemoryUnit}
                disabled={!canEditResources}
                className="w-24"
                options={[
                  { value: 'Mi', label: 'Mi' },
                  { value: 'Gi', label: 'Gi' },
                ]}
              />
            </div>
          </div>

          {/* Run Strategy */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">
              Run Strategy
            </label>
            <CustomSelect
              value={runStrategy}
              onChange={setRunStrategy}
              options={[
                { value: 'Always', label: 'Always (auto-start)' },
                { value: 'Halted', label: 'Halted (manual start)' },
                { value: 'Manual', label: 'Manual' },
                { value: 'RerunOnFailure', label: 'Rerun on Failure' },
                { value: 'Once', label: 'Once' },
              ]}
            />
            <p className="text-surface-500 text-xs mt-1">
              Controls how the VM behaves when stopped or the node restarts.
            </p>
          </div>
          
          {/* Console Settings */}
          <div className="border-t border-surface-700 pt-4">
            <label className="block text-sm font-medium text-surface-300 mb-3">
              Console Settings
            </label>
            <div className="space-y-3">
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={vncEnabled}
                  onChange={(e) => setVncEnabled(e.target.checked)}
                  disabled={!canEditResources}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500 focus:ring-offset-0 disabled:opacity-50"
                />
                <div className="flex items-center gap-2">
                  <Monitor className="w-4 h-4 text-primary-400" />
                  <span className="text-sm text-surface-300">VNC Console</span>
                </div>
              </label>
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={serialEnabled}
                  onChange={(e) => setSerialEnabled(e.target.checked)}
                  disabled={!canEditResources}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 text-primary-500 focus:ring-primary-500 focus:ring-offset-0 disabled:opacity-50"
                />
                <div className="flex items-center gap-2">
                  <Terminal className="w-4 h-4 text-emerald-400" />
                  <span className="text-sm text-surface-300">Serial Console</span>
                </div>
              </label>
            </div>
            {!canEditResources && consoleChanged && (
              <p className="text-amber-400 text-xs mt-2">
                <AlertTriangle className="h-3 w-3 inline mr-1" />
                Stop VM to apply console changes.
              </p>
            )}
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-4 border-t border-surface-700">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={isLoading} className="btn-primary">
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                'Save Changes'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

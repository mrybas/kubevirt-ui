import {
  Cpu,
  MemoryStick,
  HardDrive,
  Network,
  CheckCircle,
  XCircle,
} from 'lucide-react';
import type { VM } from '../../../types/vm';
import { CopyableValue } from '@/components/common/CopyableValue';

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-surface-400">{label}</span>
      <span className="text-surface-100 font-medium">{value}</span>
    </div>
  );
}

export function OverviewTab({ vm }: { vm: VM }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 space-y-6">
        {/* VM Configuration */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold">Configuration</h3>
          </div>
          <div className="card-body">
            <div className="grid grid-cols-2 gap-6">
              <div className="space-y-4">
                <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-800/50">
                  <div className="p-2 rounded-lg bg-primary-500/20 text-primary-400">
                    <Cpu className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="text-sm text-surface-400">CPU</p>
                    <p className="text-lg font-semibold text-surface-100">
                      {vm.cpu_cores ?? '-'} cores
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-800/50">
                  <div className="p-2 rounded-lg bg-emerald-500/20 text-emerald-400">
                    <MemoryStick className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="text-sm text-surface-400">Memory</p>
                    <p className="text-lg font-semibold text-surface-100">
                      {vm.memory ?? '-'}
                    </p>
                  </div>
                </div>
              </div>
              <div className="space-y-4">
                <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-800/50">
                  <div className="p-2 rounded-lg bg-amber-500/20 text-amber-400">
                    <HardDrive className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="text-sm text-surface-400">Disks</p>
                    <p className="text-lg font-semibold text-surface-100">
                      {vm.volumes?.length ?? 0} volume{(vm.volumes?.length ?? 0) !== 1 ? 's' : ''}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-800/50">
                  <div className="p-2 rounded-lg bg-purple-500/20 text-purple-400">
                    <Network className="h-5 w-5" />
                  </div>
                  <div className="flex-1">
                    <p className="text-sm text-surface-400">IP Address</p>
                    <CopyableValue
                      value={vm.ip_address}
                      fallback="Not assigned"
                      className="text-lg font-semibold text-surface-100"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Runtime Info */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold">Runtime</h3>
          </div>
          <div className="card-body">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="p-3 rounded-lg bg-surface-800/50">
                <p className="text-sm text-surface-400">Status</p>
                <p className="text-surface-100 font-medium mt-1">{vm.status ?? '-'}</p>
              </div>
              <div className="p-3 rounded-lg bg-surface-800/50">
                <p className="text-sm text-surface-400">Node</p>
                <p className="text-surface-100 font-medium mt-1">{vm.node ?? 'Not scheduled'}</p>
              </div>
              <div className="p-3 rounded-lg bg-surface-800/50">
                <p className="text-sm text-surface-400">Phase</p>
                <p className="text-surface-100 font-medium mt-1">{vm.phase ?? '-'}</p>
              </div>
              <div className="p-3 rounded-lg bg-surface-800/50">
                <p className="text-sm text-surface-400">Run Strategy</p>
                <p className="text-surface-100 font-medium mt-1">{vm.run_strategy ?? '-'}</p>
              </div>
            </div>
          </div>
        </div>

        {/* Guest Agent Info */}
        {(vm as any).guest_agent && (
          <div className="card">
            <div className="card-header flex items-center justify-between">
              <h3 className="font-display text-lg font-semibold">Guest Agent</h3>
              <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs ${
                (vm as any).guest_agent.agent_connected
                  ? 'bg-emerald-500/10 text-emerald-400'
                  : 'bg-red-500/10 text-red-400'
              }`}>
                {(vm as any).guest_agent.agent_connected ? (
                  <><CheckCircle className="h-3 w-3" /> Connected</>
                ) : (
                  <><XCircle className="h-3 w-3" /> Disconnected</>
                )}
              </span>
            </div>
            <div className="card-body">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                {(vm as any).guest_agent.os_info?.pretty_name && (
                  <div className="p-3 rounded-lg bg-surface-800/50">
                    <p className="text-sm text-surface-400">OS</p>
                    <p className="text-surface-100 font-medium mt-1 text-sm">{(vm as any).guest_agent.os_info.pretty_name}</p>
                  </div>
                )}
                {(vm as any).guest_agent.hostname && (
                  <div className="p-3 rounded-lg bg-surface-800/50">
                    <p className="text-sm text-surface-400">Hostname</p>
                    <p className="text-surface-100 font-medium mt-1 text-sm">{(vm as any).guest_agent.hostname}</p>
                  </div>
                )}
                {(vm as any).guest_agent.os_info?.kernel_release && (
                  <div className="p-3 rounded-lg bg-surface-800/50">
                    <p className="text-sm text-surface-400">Kernel</p>
                    <p className="text-surface-100 font-medium mt-1 text-sm font-mono">{(vm as any).guest_agent.os_info.kernel_release}</p>
                  </div>
                )}
                {(vm as any).guest_agent.os_info?.machine && (
                  <div className="p-3 rounded-lg bg-surface-800/50">
                    <p className="text-sm text-surface-400">Architecture</p>
                    <p className="text-surface-100 font-medium mt-1 text-sm">{(vm as any).guest_agent.os_info.machine}</p>
                  </div>
                )}
                {(vm as any).guest_agent.timezone && (
                  <div className="p-3 rounded-lg bg-surface-800/50">
                    <p className="text-sm text-surface-400">Timezone</p>
                    <p className="text-surface-100 font-medium mt-1 text-sm">{(vm as any).guest_agent.timezone}</p>
                  </div>
                )}
              </div>
              {(vm as any).guest_agent.interfaces && (vm as any).guest_agent.interfaces.length > 0 && (
                <details className="mt-4 group">
                  <summary className="text-sm text-surface-400 cursor-pointer hover:text-surface-300 flex items-center gap-1">
                    <span className="group-open:rotate-90 transition-transform">▶</span>
                    Network Interfaces ({(vm as any).guest_agent.interfaces.length})
                  </summary>
                  <div className="mt-2 space-y-1">
                    {(vm as any).guest_agent.interfaces.map((iface: any, idx: number) => (
                      <div key={idx} className="flex items-center justify-between text-sm p-2 rounded bg-surface-800/30">
                        <span className="text-surface-300">{iface.interface_name || iface.name}</span>
                        <div className="flex gap-3 text-surface-400">
                          {iface.ip_address && <span className="font-mono">{iface.ip_address}</span>}
                          {iface.mac && <span className="font-mono text-xs">{iface.mac}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          </div>
        )}

        {/* Conditions - collapsed by default */}
        {vm.conditions && vm.conditions.length > 0 && (
          <details className="card group">
            <summary className="card-header cursor-pointer list-none flex items-center justify-between">
              <h3 className="font-display text-lg font-semibold">Conditions</h3>
              <span className="text-surface-400 text-sm group-open:rotate-180 transition-transform">▼</span>
            </summary>
            <div className="card-body border-t border-surface-700">
              <div className="space-y-2">
                {vm.conditions.map((condition: any, idx: number) => (
                  <div
                    key={idx}
                    className="flex items-center justify-between p-2 rounded-lg bg-surface-800/30 text-sm"
                  >
                    <div className="flex items-center gap-2">
                      {condition.status === 'True' ? (
                        <CheckCircle className="h-4 w-4 text-emerald-400" />
                      ) : (
                        <XCircle className="h-4 w-4 text-red-400" />
                      )}
                      <span className="text-surface-200">{condition.type}</span>
                      {condition.message && (
                        <span className="text-surface-500">— {condition.message}</span>
                      )}
                    </div>
                    <span className={condition.status === 'True' ? 'text-emerald-400' : 'text-red-400'}>
                      {condition.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </details>
        )}
      </div>

      {/* Sidebar */}
      <div className="space-y-6">
        {/* Details */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold">Details</h3>
          </div>
          <div className="card-body space-y-4">
            <DetailRow label="Namespace" value={vm.namespace ?? '-'} />
            <DetailRow label="Node" value={vm.node ?? 'Not scheduled'} />
            <DetailRow
              label="Created"
              value={
                vm.created
                  ? new Date(vm.created).toLocaleString()
                  : '-'
              }
            />
            <DetailRow label="Ready" value={vm.ready ? 'Yes' : 'No'} />
          </div>
        </div>

        {/* Labels */}
        <div className="card">
          <div className="card-header">
            <h3 className="font-display text-lg font-semibold">Labels</h3>
          </div>
          <div className="card-body">
            {vm.labels && Object.keys(vm.labels).length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {Object.entries(vm.labels).map(([key, value]) => (
                  <span key={key} className="badge-neutral text-xs">
                    {key}: {value as string}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-surface-400 text-center py-4">No labels</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

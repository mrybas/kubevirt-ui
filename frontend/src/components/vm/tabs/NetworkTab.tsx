import { useState } from 'react';
import { Network, Loader2, Check, X, Unlink, Globe, Plus, Shield } from 'lucide-react';
import { useVMInterfaces, useAddVMInterface, useRemoveVMInterface } from '@/hooks/useVMs';
import { useSubnets } from '@/hooks/useNetwork';
import { useVmSecurityGroups, useAssignSecurityGroupToVm, useRemoveSecurityGroupFromVm, useSecurityGroups } from '../../../hooks/useSecurityGroups';
import type { VM } from '../../../types/vm';
import { CopyableValue } from '@/components/common/CopyableValue';

export function NetworkTab({ vm }: { vm: VM }) {
  const { data: interfaces, isLoading } = useVMInterfaces(vm.namespace, vm.name);
  const addNIC = useAddVMInterface();
  const removeNIC = useRemoveVMInterface();
  const { data: subnets } = useSubnets();
  const [showAddForm, setShowAddForm] = useState(false);
  const [removeConfirm, setRemoveConfirm] = useState<string | null>(null);

  // Filter to external subnets (have a VLAN) matching the VM's namespace
  const externalSubnets = (subnets || []).filter(
    (s: any) => s.vlan && (!s.namespace || s.namespace === vm.namespace)
  );

  // Already-connected network names (from interfaces)
  const connectedNADs = new Set(
    (interfaces || []).map((i: any) => i.network_name).filter(Boolean)
  );

  const handleAddSubnet = (subnet: any) => {
    const vlanName = subnet.vlan || subnet.name;
    const nadRef = `${vm.namespace}/${vlanName}`;
    const ifaceName = vlanName;

    addNIC.mutate(
      { namespace: vm.namespace, vmName: vm.name, data: { name: ifaceName, network_name: nadRef, binding: 'bridge' } },
      { onSuccess: () => setShowAddForm(false) }
    );
  };

  const handleRemove = (ifaceName: string) => {
    removeNIC.mutate(
      { namespace: vm.namespace, vmName: vm.name, ifaceName },
      { onSuccess: () => setRemoveConfirm(null) }
    );
  };

  return (
    <div className="space-y-6">
      <SecurityGroupPicker namespace={vm.namespace} vmName={vm.name} />
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h3 className="font-display text-lg font-semibold">Network Interfaces</h3>
          <button onClick={() => setShowAddForm(!showAddForm)} className="btn-secondary text-sm">
            {showAddForm ? (
              <><X className="h-4 w-4" /> Cancel</>
            ) : (
              <><Plus className="h-4 w-4" /> Add Interface</>
            )}
          </button>
        </div>

        {showAddForm && (
          <div className="px-4 py-3 border-b border-surface-700 bg-surface-900/30">
            <p className="text-xs text-surface-400 mb-2">Select a network to attach (DHCP will be used automatically):</p>
            {externalSubnets.length === 0 ? (
              <div className="text-center py-4 text-surface-500 text-sm">
                <Network className="w-6 h-6 mx-auto mb-1.5 opacity-50" />
                No external networks available for this namespace.
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2">
                {externalSubnets.map((subnet: any) => {
                  const vlanName = subnet.vlan || subnet.name;
                  const nadRef = `${vm.namespace}/${vlanName}`;
                  const isConnected = connectedNADs.has(nadRef);
                  return (
                    <button
                      key={subnet.name}
                      onClick={() => !isConnected && handleAddSubnet(subnet)}
                      disabled={isConnected || addNIC.isPending}
                      className={`p-3 rounded-lg border text-left transition-all ${
                        isConnected
                          ? 'border-surface-700 bg-surface-800/50 opacity-50 cursor-not-allowed'
                          : 'border-surface-700 hover:border-emerald-500/40 hover:bg-emerald-500/5 bg-surface-800'
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        <div className={`p-1.5 rounded-lg ${isConnected ? 'bg-emerald-500/20' : 'bg-surface-700'}`}>
                          <Globe className={`w-4 h-4 ${isConnected ? 'text-emerald-400' : 'text-surface-400'}`} />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-surface-100">{subnet.name}</span>
                            <span className="text-xs font-mono text-surface-500">{subnet.cidr_block}</span>
                            {isConnected && <span className="text-xs text-emerald-400">connected</span>}
                          </div>
                          <div className="flex items-center gap-3 text-xs text-surface-500 mt-0.5">
                            <span>GW: {subnet.gateway}</span>
                            <span className="text-emerald-400">{subnet.statistics?.available || 0} IPs free</span>
                            <span>DHCP</span>
                            {subnet.vlan && <span>VLAN: {subnet.vlan}</span>}
                          </div>
                        </div>
                        {!isConnected && (
                          addNIC.isPending
                            ? <Loader2 className="w-4 h-4 animate-spin text-surface-400" />
                            : <Plus className="w-4 h-4 text-surface-500" />
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
            {addNIC.error && (
              <p className="text-red-400 text-sm mt-2">{(addNIC.error as any).message}</p>
            )}
          </div>
        )}

        {isLoading ? (
          <div className="p-8 flex items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-surface-400" />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Network</th>
                  <th>Binding</th>
                  <th>IP Address</th>
                  <th>MAC Address</th>
                  <th>State</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(interfaces || []).map((iface) => (
                  <tr key={iface.name} className={iface.state === 'absent' ? 'opacity-50' : ''}>
                    <td className="font-medium text-surface-100">
                      <div className="flex items-center gap-2">
                        <Network className="h-4 w-4 text-surface-500" />
                        {iface.name}
                        {iface.is_default && (
                          <span className="text-xs bg-surface-700 px-1.5 py-0.5 rounded text-surface-400">default</span>
                        )}
                      </div>
                    </td>
                    <td className="text-surface-400 text-sm">{iface.network_name || iface.network_type}</td>
                    <td className="text-surface-400 text-sm">{iface.binding}</td>
                    <td>
                      <CopyableValue
                        value={iface.ip_address ?? undefined}
                        className="text-surface-300"
                      />
                    </td>
                    <td className="font-mono text-surface-400 text-sm">{iface.mac || '-'}</td>
                    <td>
                      {iface.state === 'absent' ? (
                        <span className="text-xs text-red-400">removing</span>
                      ) : iface.hotplugged ? (
                        <span className="text-xs text-primary-400">hotplugged</span>
                      ) : (
                        <span className="text-xs text-surface-500">active</span>
                      )}
                    </td>
                    <td className="text-right">
                      {!iface.is_default && iface.state !== 'absent' && (
                        removeConfirm === iface.name ? (
                          <div className="flex items-center gap-1 justify-end">
                            <span className="text-xs text-red-400">Remove?</span>
                            <button onClick={() => handleRemove(iface.name)} className="btn-ghost p-1 text-red-400 hover:text-red-300" disabled={removeNIC.isPending}>
                              {removeNIC.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                            </button>
                            <button onClick={() => setRemoveConfirm(null)} className="btn-ghost p-1">
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        ) : (
                          <button onClick={() => setRemoveConfirm(iface.name)} className="btn-ghost p-1 text-surface-500 hover:text-red-400" title="Remove interface">
                            <Unlink className="h-3.5 w-3.5" />
                          </button>
                        )
                      )}
                    </td>
                  </tr>
                ))}
                {(!interfaces || interfaces.length === 0) && (
                  <tr>
                    <td colSpan={7} className="text-center text-surface-500 py-6">No interfaces found</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        {removeNIC.error && (
          <div className="px-4 py-2 border-t border-surface-700">
            <p className="text-red-400 text-sm">{removeNIC.error.message}</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SecurityGroupPicker
// ---------------------------------------------------------------------------

function SecurityGroupPicker({ namespace, vmName }: { namespace: string; vmName: string }) {
  const [showAdd, setShowAdd] = useState(false);
  const [selected, setSelected] = useState('');

  const { data: assigned, isLoading } = useVmSecurityGroups(namespace, vmName);
  const { data: allSgs } = useSecurityGroups();
  const assign = useAssignSecurityGroupToVm(namespace, vmName);
  const remove = useRemoveSecurityGroupFromVm(namespace, vmName);

  const assignedNames = assigned?.security_groups ?? [];
  const available = (allSgs?.items ?? []).filter((sg) => !assignedNames.includes(sg.name));

  const handleAssign = async () => {
    if (!selected) return;
    await assign.mutateAsync({ security_group: selected });
    setSelected('');
    setShowAdd(false);
  };

  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield className="h-4 w-4 text-amber-400" />
          <h3 className="font-display text-lg font-semibold">Security Groups</h3>
        </div>
        <button onClick={() => setShowAdd(!showAdd)} className="btn-secondary text-sm">
          {showAdd ? <><X className="h-4 w-4" /> Cancel</> : <><Plus className="h-4 w-4" /> Assign</>}
        </button>
      </div>

      {showAdd && (
        <div className="px-4 py-3 border-b border-surface-700 bg-surface-900/30">
          {available.length === 0 ? (
            <p className="text-sm text-surface-500 text-center py-2">No security groups available to assign.</p>
          ) : (
            <div className="flex items-center gap-2">
              <select
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                className="flex-1 input text-sm"
              >
                <option value="">Select a security group...</option>
                {available.map((sg) => (
                  <option key={sg.name} value={sg.name}>{sg.name}</option>
                ))}
              </select>
              <button
                onClick={handleAssign}
                disabled={!selected || assign.isPending}
                className="btn-primary text-sm"
              >
                {assign.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Assign'}
              </button>
            </div>
          )}
        </div>
      )}

      <div className="card-body">
        {isLoading ? (
          <div className="flex justify-center py-4">
            <Loader2 className="h-5 w-5 animate-spin text-surface-400" />
          </div>
        ) : assignedNames.length === 0 ? (
          <div className="text-center py-4 text-surface-500 text-sm">
            <Shield className="w-6 h-6 mx-auto mb-1.5 opacity-40" />
            No security groups assigned
          </div>
        ) : (
          <div className="space-y-2">
            {assignedNames.map((sgName) => (
              <div key={sgName} className="flex items-center justify-between p-2.5 bg-surface-900/50 border border-surface-700/50 rounded-lg">
                <div className="flex items-center gap-2">
                  <Shield className="h-4 w-4 text-amber-400" />
                  <span className="text-sm text-surface-200 font-mono">{sgName}</span>
                </div>
                <button
                  onClick={() => remove.mutateAsync(sgName)}
                  disabled={remove.isPending}
                  className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                  title="Remove"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

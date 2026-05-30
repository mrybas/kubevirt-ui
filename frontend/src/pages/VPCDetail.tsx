/**
 * VPC Detail Page — tabs: Overview, Subnets, Peerings, Routes
 */

import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Network,
  ArrowLeft,
  RefreshCw,
  Trash2,
  Plus,
  X,
  GitMerge,
  Route,
  Layers,
  Server,
  Shield,
  AlertCircle,
} from 'lucide-react';
import clsx from 'clsx';
import { useVpc, useDeleteVpc, useAddVpcPeering, useRemoveVpcPeering, useVpcRoutes, useUpdateVpcRoutes, useVpcDns, useUpdateVpcDns, useRecreateVpcDns, useVpcDnsPolicy, useUpdateVpcDnsPolicy, useRecreateVpcDnsPolicy } from '../hooks/useVpcs';
import { useEgressGateways, useDetachVpc } from '../hooks/useEgressGateways';
import { ApiError } from '../api/client';
import { notify } from '../store/notifications';
import type { VpcRoute, VpcSubnet, UpdateVpcDnsRequest } from '../types/vpc';

type Tab = 'overview' | 'subnets' | 'peerings' | 'routes' | 'dns' | 'dns-policy';

const TABS: { id: Tab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'overview', label: 'Overview', icon: Network },
  { id: 'subnets', label: 'Subnets', icon: Layers },
  { id: 'peerings', label: 'Peerings', icon: GitMerge },
  { id: 'routes', label: 'Static Routes', icon: Route },
  { id: 'dns', label: 'DNS', icon: Server },
  { id: 'dns-policy', label: 'DNS Policy', icon: Shield },
];

export default function VPCDetail() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  const { data: vpc, isLoading, refetch } = useVpc(name);
  const deleteVpc = useDeleteVpc();

  const handleDelete = async () => {
    if (!vpc) return;
    await deleteVpc.mutateAsync(vpc.name);
    navigate('/network/vpcs');
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-8 h-8 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!vpc) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-surface-400">
        <Network className="w-12 h-12 mb-4 opacity-50" />
        <p>VPC not found</p>
        <button onClick={() => navigate('/network/vpcs')} className="mt-4 btn-secondary text-sm">
          Back to VPCs
        </button>
      </div>
    );
  }

  const statusColor = vpc.ready
    ? 'bg-emerald-500 text-emerald-400'
    : 'bg-amber-500 text-amber-400';

  return (
    <div className="space-y-6">
      {/* Back */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/network/vpcs')}
          className="p-1.5 text-surface-500 hover:text-surface-300 hover:bg-surface-800 rounded-lg transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <span className="text-surface-500 text-sm">VPCs</span>
        <span className="text-surface-600">/</span>
        <span className="text-surface-200 text-sm font-mono">{vpc.name}</span>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 bg-blue-600/20 rounded-xl flex items-center justify-center">
            <Network className="w-6 h-6 text-blue-400" />
          </div>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold text-surface-100">{vpc.name}</h1>
              <span className={clsx('flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full bg-opacity-20', statusColor)}>
                <span className={clsx('w-1.5 h-1.5 rounded-full', statusColor.split(' ')[0])} />
                {vpc.ready ? 'Ready' : 'Pending'}
              </span>
            </div>
            <div className="flex items-center gap-2 mt-1">
              {(vpc.subnets ?? []).map((s) => (
                <span key={s.name} className="text-sm text-surface-400 font-mono">{s.cidr_block}</span>
              ))}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => refetch()} className="btn-secondary" title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
          <button
            onClick={() => setShowDeleteModal(true)}
            className="p-2 text-surface-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Stats row */}
      <div className="flex flex-wrap gap-4 text-sm text-surface-400">
        <span className="flex items-center gap-1.5">
          <Layers className="w-4 h-4" />
          {vpc.subnets.length} subnet{vpc.subnets.length !== 1 ? 's' : ''}
        </span>
        <span className="flex items-center gap-1.5">
          <GitMerge className="w-4 h-4" />
          {vpc.peerings.length} peering{vpc.peerings.length !== 1 ? 's' : ''}
        </span>
        {vpc.namespaces.length > 0 && (
          <span className="flex items-center gap-1.5">
            <Network className="w-4 h-4" />
            {vpc.namespaces.length} namespace{vpc.namespaces.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="border-b border-surface-700">
        <div className="flex items-center gap-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={clsx(
                'flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors',
                activeTab === tab.id
                  ? 'border-primary-400 text-primary-400'
                  : 'border-transparent text-surface-400 hover:text-surface-200'
              )}
            >
              <tab.icon className="h-4 w-4" />
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' && <OverviewTab vpc={vpc} />}
      {activeTab === 'subnets' && <SubnetsTab vpc={vpc} />}
      {activeTab === 'peerings' && <PeeringsTab vpc={vpc} />}
      {activeTab === 'routes' && <RoutesTab vpcName={vpc.name} />}
      {activeTab === 'dns' && <DnsTab vpcName={vpc.name} subnets={vpc.subnets} />}
      {activeTab === 'dns-policy' && <DnsPolicyTab vpcName={vpc.name} />}

      {showDeleteModal && (
        <DeleteVpcModal
          vpcName={vpc.name}
          onConfirm={handleDelete}
          onCancel={() => setShowDeleteModal(false)}
          isDeleting={deleteVpc.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OverviewTab
// ---------------------------------------------------------------------------

function EgressGatewaySection({ vpcName }: { vpcName: string }) {
  const { data: egressData } = useEgressGateways();
  const navigate = useNavigate();

  const attachedGateway = (egressData?.items ?? []).find((gw) =>
    gw.attached_vpcs.some((v) => v.vpc_name === vpcName)
  );
  const attachedVpc = attachedGateway?.attached_vpcs.find((v) => v.vpc_name === vpcName);

  const detachVpc = useDetachVpc(attachedGateway?.name ?? '');

  const handleDetach = async () => {
    if (!attachedGateway || !attachedVpc) return;
    await detachVpc.mutateAsync({
      vpc_name: vpcName,
      subnet_name: attachedVpc.subnet_name,
    });
  };

  if (!attachedGateway) {
    return (
      <div className="flex items-center justify-between text-sm">
        <div>
          <span className="text-surface-400">Egress Gateway</span>
          <span className="ml-3 text-surface-500 italic">None (no internet)</span>
        </div>
        <button
          onClick={() => navigate('/network/egress-gateways')}
          className="text-xs text-primary-400 hover:text-primary-300 transition-colors"
        >
          Configure →
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between text-sm">
      <div>
        <span className="text-surface-400">Egress Gateway</span>
        <span className="ml-3 text-surface-200 font-mono">{attachedGateway.name}</span>
        {attachedVpc && (
          <span className="ml-2 text-surface-500 text-xs">via {attachedVpc.subnet_name} ({attachedVpc.cidr})</span>
        )}
      </div>
      <button
        onClick={handleDetach}
        disabled={detachVpc.isPending}
        className="text-xs text-red-400 hover:text-red-300 transition-colors disabled:opacity-50"
        title="Detach from egress gateway"
      >
        {detachVpc.isPending ? 'Detaching...' : 'Detach'}
      </button>
    </div>
  );
}

function OverviewTab({ vpc }: { vpc: NonNullable<ReturnType<typeof useVpc>['data']> }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div className="card">
        <div className="card-body space-y-3">
          <h3 className="font-medium text-surface-100 mb-3">Details</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-surface-400">Name</span>
              <span className="text-surface-200 font-mono">{vpc.name}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-surface-400">Status</span>
              <span className="text-surface-200">{vpc.ready ? 'Ready' : 'Pending'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-surface-400">Subnets</span>
              <div className="text-right">
                {(vpc.subnets ?? []).map((s) => (
                  <div key={s.name} className="text-surface-200 font-mono text-xs">{s.cidr_block}</div>
                ))}
              </div>
            </div>
          </div>
          <div className="border-t border-surface-700 pt-3">
            <EgressGatewaySection vpcName={vpc.name} />
          </div>
        </div>
      </div>

      {vpc.namespaces.length > 0 && (
        <div className="card">
          <div className="card-body">
            <h3 className="font-medium text-surface-100 mb-3">Namespaces</h3>
            <div className="space-y-1">
              {vpc.namespaces.map((ns) => (
                <div key={ns} className="px-3 py-1.5 bg-surface-900 rounded-lg text-sm font-mono text-surface-300">
                  {ns}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SubnetsTab
// ---------------------------------------------------------------------------

function SubnetsTab({ vpc }: { vpc: NonNullable<ReturnType<typeof useVpc>['data']> }) {
  if (vpc.subnets.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <Layers className="w-10 h-10 mb-3 opacity-50" />
        <p>No subnets configured</p>
      </div>
    );
  }

  return (
    <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
      {vpc.subnets.map((subnet) => (
        <div key={subnet.name} className="flex items-center justify-between px-4 py-3 bg-surface-800/50 hover:bg-surface-800 transition-colors">
          <div>
            <span className="text-sm font-medium text-surface-200">{subnet.name}</span>
          </div>
          <div className="flex items-center gap-4 text-xs text-surface-400">
            <span className="font-mono">{subnet.cidr_block}</span>
            <span>GW: {subnet.gateway}</span>
            <span>{subnet.available_ips} IPs available</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// PeeringsTab
// ---------------------------------------------------------------------------

function PeeringsTab({ vpc }: { vpc: NonNullable<ReturnType<typeof useVpc>['data']> }) {
  const [showAdd, setShowAdd] = useState(false);
  const [remoteVpc, setRemoteVpc] = useState('');
  const addPeering = useAddVpcPeering(vpc.name);
  const removePeering = useRemoveVpcPeering(vpc.name);

  const handleAdd = async () => {
    const trimmed = remoteVpc.trim();
    if (!trimmed) return;
    await addPeering.mutateAsync({ remote_vpc: trimmed });
    setRemoteVpc('');
    setShowAdd(false);
  };

  return (
    <div className="space-y-3">
      {vpc.peerings.length === 0 && !showAdd ? (
        <div className="flex flex-col items-center justify-center h-48 text-surface-400">
          <GitMerge className="w-10 h-10 mb-3 opacity-50" />
          <p className="mb-3">No peerings configured</p>
          <button onClick={() => setShowAdd(true)} className="btn-primary text-sm">
            <Plus className="h-4 w-4" />
            Add Peering
          </button>
        </div>
      ) : (
        <>
          {vpc.peerings.length > 0 && (
            <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
              {vpc.peerings.map((p) => (
                <div key={p.remote_vpc} className="flex items-center justify-between px-4 py-3 bg-surface-800/50 hover:bg-surface-800 transition-colors">
                  <div className="flex items-center gap-3">
                    <GitMerge className="w-4 h-4 text-surface-400" />
                    <span className="text-sm font-medium text-surface-200 font-mono">{p.remote_vpc}</span>
                    <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-900/30 text-emerald-400">
                      Active
                    </span>
                  </div>
                  <button
                    onClick={() => removePeering.mutateAsync(p.remote_vpc)}
                    disabled={removePeering.isPending}
                    className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {showAdd ? (
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={remoteVpc}
                onChange={(e) => setRemoteVpc(e.target.value)}
                placeholder="Remote VPC name"
                className="flex-1 input font-mono text-sm"
                onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
                autoFocus
              />
              <button
                onClick={handleAdd}
                disabled={!remoteVpc.trim() || addPeering.isPending}
                className="btn-primary text-sm"
              >
                {addPeering.isPending ? '...' : 'Add'}
              </button>
              <button
                onClick={() => { setShowAdd(false); setRemoteVpc(''); }}
                className="p-2 text-surface-400 hover:text-surface-200"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowAdd(true)}
              className="flex items-center gap-1.5 text-sm text-surface-500 hover:text-primary-400 transition-colors"
            >
              <Plus className="w-4 h-4" />
              Add Peering
            </button>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RoutesTab
// ---------------------------------------------------------------------------

function RoutesTab({ vpcName }: { vpcName: string }) {
  const { data, isLoading } = useVpcRoutes(vpcName);
  const updateRoutes = useUpdateVpcRoutes(vpcName);

  const [editing, setEditing] = useState(false);
  const [routes, setRoutes] = useState<VpcRoute[]>([]);

  const startEdit = () => {
    setRoutes(data?.routes ?? []);
    setEditing(true);
  };

  const addRoute = () => setRoutes([...routes, { cidr: '', next_hop: '' }]);
  const removeRoute = (i: number) => setRoutes(routes.filter((_, idx) => idx !== i));
  const updateRoute = (i: number, field: keyof VpcRoute, value: string) => {
    setRoutes(routes.map((r, idx) => idx === i ? { ...r, [field]: value } : r));
  };

  const handleSave = async () => {
    await updateRoutes.mutateAsync({ routes: routes.filter((r) => r.cidr && r.next_hop) });
    setEditing(false);
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const currentRoutes = editing ? routes : (data?.routes ?? []);

  return (
    <div className="space-y-3">
      <div className="flex justify-end gap-2">
        {editing ? (
          <>
            <button onClick={() => setEditing(false)} className="btn-secondary text-sm">Cancel</button>
            <button onClick={handleSave} disabled={updateRoutes.isPending} className="btn-primary text-sm">
              {updateRoutes.isPending ? 'Saving...' : 'Save Routes'}
            </button>
          </>
        ) : (
          <button onClick={startEdit} className="btn-secondary text-sm">Edit Routes</button>
        )}
      </div>

      {currentRoutes.length === 0 && !editing ? (
        <div className="flex flex-col items-center justify-center h-48 text-surface-400">
          <Route className="w-10 h-10 mb-3 opacity-50" />
          <p className="mb-3">No static routes</p>
          <button onClick={startEdit} className="btn-primary text-sm">Add Routes</button>
        </div>
      ) : (
        <div className="space-y-2">
          {editing && (
            <div className="grid grid-cols-2 gap-2 px-4 py-2 text-xs text-surface-500 font-medium uppercase tracking-wider">
              <span>Destination CIDR</span>
              <span>Next Hop</span>
            </div>
          )}
          {!editing && currentRoutes.length > 0 && (
            <div className="divide-y divide-surface-700 border border-surface-700 rounded-xl overflow-hidden">
              {currentRoutes.map((r, i) => (
                <div key={i} className="grid grid-cols-2 gap-4 px-4 py-3 bg-surface-800/50 text-sm">
                  <span className="font-mono text-surface-200">{r.cidr}</span>
                  <span className="font-mono text-surface-400">{r.next_hop}</span>
                </div>
              ))}
            </div>
          )}
          {editing && routes.map((r, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                value={r.cidr}
                onChange={(e) => updateRoute(i, 'cidr', e.target.value)}
                placeholder="0.0.0.0/0"
                className="flex-1 input font-mono text-sm"
              />
              <input
                type="text"
                value={r.next_hop}
                onChange={(e) => updateRoute(i, 'next_hop', e.target.value)}
                placeholder="10.0.0.1"
                className="flex-1 input font-mono text-sm"
              />
              <button
                onClick={() => removeRoute(i)}
                className="p-1.5 text-surface-500 hover:text-red-400 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
          {editing && (
            <button
              onClick={addRoute}
              className="flex items-center gap-1.5 text-sm text-surface-500 hover:text-primary-400 transition-colors"
            >
              <Plus className="w-4 h-4" />
              Add Route
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DnsTab
// ---------------------------------------------------------------------------

function DnsTab({ vpcName, subnets }: { vpcName: string; subnets: VpcSubnet[] }) {
  const { data: dns, isLoading, error } = useVpcDns(vpcName);
  const updateDns = useUpdateVpcDns(vpcName);
  const recreateDns = useRecreateVpcDns(vpcName);

  const [subnet, setSubnet] = useState('');
  const [replicas, setReplicas] = useState(1);
  const [showRecreateModal, setShowRecreateModal] = useState(false);

  // Sync form state when data loads
  useEffect(() => {
    if (dns) {
      setSubnet(dns.subnet);
      setReplicas(dns.replicas);
    }
  }, [dns]);

  const isDirty = dns ? (subnet !== dns.subnet || replicas !== dns.replicas) : false;

  const handleSave = async () => {
    const req: UpdateVpcDnsRequest = {};
    if (dns && subnet !== dns.subnet) req.subnet = subnet;
    if (dns && replicas !== dns.replicas) req.replicas = replicas;
    try {
      await updateDns.mutateAsync(req);
      notify.success('DNS configuration saved');
    } catch (e) {
      notify.error('Failed to update DNS', e instanceof Error ? e.message : String(e));
    }
  };

  const handleRecreate = async () => {
    setShowRecreateModal(false);
    try {
      await recreateDns.mutateAsync();
      notify.success('VpcDns recreated');
    } catch (e) {
      notify.error('Failed to recreate VpcDns', e instanceof Error ? e.message : String(e));
    }
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  // 404 — VpcDns was not auto-created (legacy VPC or creation failure)
  if (error instanceof ApiError && error.status === 404) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <Server className="w-10 h-10 mb-3 opacity-50" />
        <p className="text-sm mb-1">DNS not configured for this VPC</p>
        <p className="text-xs text-surface-500 mb-4">VpcDns CR was not auto-created (legacy VPC or provisioning failure)</p>
        <button
          onClick={() => handleRecreate()}
          disabled={recreateDns.isPending}
          className="btn-primary text-sm"
        >
          {recreateDns.isPending ? 'Creating...' : 'Create VpcDns'}
        </button>
      </div>
    );
  }

  // Other error (network, 5xx, etc.)
  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <AlertCircle className="w-10 h-10 mb-3 text-red-400 opacity-70" />
        <p className="text-sm">Failed to load DNS config</p>
        <p className="text-xs text-red-400 mt-1">{error.message}</p>
      </div>
    );
  }

  if (!dns) return null;

  const phaseStyles: Record<string, { dot: string; badge: string; label: string }> = {
    ready:   { dot: 'bg-emerald-500', badge: 'bg-emerald-500/20 text-emerald-400', label: 'Ready' },
    pending: { dot: 'bg-amber-500',   badge: 'bg-amber-500/20 text-amber-400',     label: 'Pending' },
    error:   { dot: 'bg-red-500',     badge: 'bg-red-500/20 text-red-400',         label: 'Error' },
    absent:  { dot: 'bg-surface-500', badge: 'bg-surface-500/20 text-surface-400', label: 'Absent' },
  };
  const phase = phaseStyles[dns.status.phase] ?? phaseStyles.pending;

  return (
    <div className="space-y-4">
      {/* Header card */}
      <div className="card">
        <div className="card-body space-y-3">
          <h3 className="font-medium text-surface-100">VpcDns</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-surface-400">Name</span>
              <span className="text-surface-200 font-mono">{dns.name}</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-surface-400">Status</span>
              <span className={clsx('flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full', phase.badge)}>
                <span className={clsx('w-1.5 h-1.5 rounded-full', phase.dot)} />
                {phase.label}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-surface-400">CoreDNS VIP</span>
              <span className="text-surface-200 font-mono">{dns.coredns_vip || '—'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-surface-400">Active Pods</span>
              <span className="text-surface-200">{dns.status.active_pods}</span>
            </div>
            {dns.status.message && (
              <p className="text-xs text-amber-400 pt-1">{dns.status.message}</p>
            )}
          </div>
        </div>
      </div>

      {/* Spec card */}
      <div className="card">
        <div className="card-body space-y-4">
          <h3 className="font-medium text-surface-100">Configuration</h3>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-surface-400 mb-1">Subnet</label>
              <select
                value={subnet}
                onChange={(e) => setSubnet(e.target.value)}
                disabled={subnets.length <= 1}
                className="input w-full text-sm font-mono"
              >
                {subnets.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name} ({s.cidr_block})
                  </option>
                ))}
                {/* Fallback: current subnet not in the VPC's subnet list */}
                {!subnets.some((s) => s.name === subnet) && subnet && (
                  <option value={subnet}>{subnet}</option>
                )}
              </select>
            </div>
            <div>
              <label className="block text-sm text-surface-400 mb-1">Replicas</label>
              <input
                type="number"
                min={1}
                max={5}
                value={replicas}
                onChange={(e) => setReplicas(Math.max(1, Math.min(5, parseInt(e.target.value) || 1)))}
                className="input w-28 text-sm"
              />
            </div>
          </div>
          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={handleSave}
              disabled={!isDirty || updateDns.isPending}
              className="btn-primary text-sm"
            >
              {updateDns.isPending ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={() => setShowRecreateModal(true)}
              className="text-sm px-3 py-1.5 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded-lg transition-colors"
            >
              Recreate
            </button>
          </div>
        </div>
      </div>

      {showRecreateModal && (
        <RecreateVpcDnsModal
          dnsName={dns.name}
          onConfirm={handleRecreate}
          onCancel={() => setShowRecreateModal(false)}
          isPending={recreateDns.isPending}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RecreateVpcDnsModal
// ---------------------------------------------------------------------------

function RecreateVpcDnsModal({
  dnsName,
  onConfirm,
  onCancel,
  isPending,
}: {
  dnsName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-sm mx-4 shadow-2xl p-5">
        <div className="w-12 h-12 bg-amber-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <RefreshCw className="w-6 h-6 text-amber-400" />
        </div>
        <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Recreate VpcDns</h2>
        <p className="text-sm text-surface-400 text-center mb-4">
          This will delete and recreate{' '}
          <strong className="font-mono text-surface-200">{dnsName}</strong>.{' '}
          DNS resolution in this VPC will be briefly interrupted.
        </p>
        <div className="flex gap-3">
          <button onClick={onCancel} disabled={isPending} className="flex-1 btn-secondary">
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="flex-1 px-4 py-2 bg-amber-600 hover:bg-amber-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isPending ? 'Recreating...' : 'Recreate'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeleteVpcModal
// ---------------------------------------------------------------------------

function DeleteVpcModal({
  vpcName,
  onConfirm,
  onCancel,
  isDeleting,
}: {
  vpcName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isDeleting: boolean;
}) {
  const [confirmName, setConfirmName] = useState('');

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-surface-800 border border-surface-700 rounded-xl w-full max-w-md mx-4 shadow-2xl p-5">
        <div className="w-12 h-12 bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <Trash2 className="w-6 h-6 text-red-400" />
        </div>
        <h2 className="text-lg font-semibold text-surface-100 text-center mb-2">Delete VPC</h2>
        <p className="text-sm text-surface-400 text-center mb-4">
          This will delete <strong>{vpcName}</strong> and all subnets. Cannot be undone.
        </p>
        <div className="mb-4">
          <label className="block text-sm text-surface-400 mb-1">
            Type <strong>{vpcName}</strong> to confirm:
          </label>
          <input
            type="text"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            placeholder={vpcName}
            className="input w-full focus:border-red-500"
          />
        </div>
        <div className="flex gap-3">
          <button onClick={onCancel} className="flex-1 btn-secondary">Cancel</button>
          <button
            onClick={onConfirm}
            disabled={confirmName !== vpcName || isDeleting}
            className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:bg-surface-700 disabled:text-surface-500 text-white rounded-lg transition-colors"
          >
            {isDeleting ? 'Deleting...' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// DnsPolicyTab — view + edit the per-VPC Kyverno ClusterPolicy that
// injects VpcDns into pods landing in this VPC's namespaces.
// ---------------------------------------------------------------------------

function DnsPolicyTab({ vpcName }: { vpcName: string }) {
  const { data: policy, isLoading, error } = useVpcDnsPolicy(vpcName);
  const updatePolicy = useUpdateVpcDnsPolicy(vpcName);
  const recreatePolicy = useRecreateVpcDnsPolicy(vpcName);

  const [draft, setDraft] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);

  // Sync draft when policy loads (and on cancel).
  useEffect(() => {
    if (policy) setDraft(JSON.stringify(policy, null, 2));
  }, [policy]);

  const handleSave = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(draft);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : String(e));
      return;
    }
    setParseError(null);
    try {
      await updatePolicy.mutateAsync(parsed);
      notify.success('DNS policy saved');
      setIsEditing(false);
    } catch (e) {
      notify.error('Failed to save policy', e instanceof Error ? e.message : String(e));
    }
  };

  const handleRecreate = async () => {
    try {
      await recreatePolicy.mutateAsync();
      notify.success('DNS policy recreated with default body');
      setIsEditing(false);
    } catch (e) {
      notify.error('Failed to recreate policy', e instanceof Error ? e.message : String(e));
    }
  };

  const handleCancel = () => {
    if (policy) setDraft(JSON.stringify(policy, null, 2));
    setParseError(null);
    setIsEditing(false);
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error instanceof ApiError && error.status === 404) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <Shield className="w-10 h-10 mb-3 opacity-50" />
        <p className="text-sm mb-1">No DNS policy for this VPC</p>
        <p className="text-xs text-surface-500 mb-4 text-center max-w-md">
          {error.message || 'Either Kyverno is not installed, or this VPC pre-dates the auto-create.'}
        </p>
        <button
          onClick={handleRecreate}
          disabled={recreatePolicy.isPending}
          className="btn-primary text-sm"
        >
          {recreatePolicy.isPending ? 'Creating...' : 'Create default policy'}
        </button>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-surface-400">
        <AlertCircle className="w-10 h-10 mb-3 text-red-400 opacity-70" />
        <p className="text-sm">Failed to load DNS policy</p>
        <p className="text-xs text-red-400 mt-1">{error.message}</p>
      </div>
    );
  }

  if (!policy) return null;

  return (
    <div className="space-y-4">
      <div className="card">
        <div className="card-body space-y-3">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="font-medium text-surface-100">Kyverno ClusterPolicy</h3>
              <p className="text-xs text-surface-500 mt-0.5">
                Mutates pods in this VPC's namespaces to use the VpcDns VIP as DNS.
                Created automatically at VPC create; edit only if you know what you're doing.
              </p>
            </div>
            <div className="flex items-center gap-2">
              {!isEditing && (
                <button onClick={() => setIsEditing(true)} className="btn-secondary text-sm">
                  Edit
                </button>
              )}
              {isEditing && (
                <>
                  <button
                    onClick={handleCancel}
                    className="btn-secondary text-sm"
                    disabled={updatePolicy.isPending}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleSave}
                    className="btn-primary text-sm"
                    disabled={updatePolicy.isPending}
                  >
                    {updatePolicy.isPending ? 'Saving...' : 'Save'}
                  </button>
                </>
              )}
              <button
                onClick={handleRecreate}
                disabled={recreatePolicy.isPending || isEditing}
                className="btn-secondary text-sm"
                title="Discard current and recreate default policy"
              >
                {recreatePolicy.isPending ? 'Recreating...' : 'Recreate default'}
              </button>
            </div>
          </div>

          {parseError && (
            <div className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-xs text-red-400">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span>JSON parse error: {parseError}</span>
            </div>
          )}

          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            readOnly={!isEditing}
            spellCheck={false}
            className={clsx(
              'w-full h-[600px] font-mono text-xs p-3 rounded-lg border resize-y',
              isEditing
                ? 'bg-surface-900 border-surface-700 text-surface-100 focus:border-primary-500 focus:outline-none'
                : 'bg-surface-950 border-surface-800 text-surface-300 cursor-default',
            )}
          />
        </div>
      </div>
    </div>
  );
}

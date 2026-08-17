/**
 * BGP Peering Page
 *
 * Manage kube-ovn-speaker deployment and BGP route announcements.
 * Sections: Speaker Status | Deploy/Update Form | Sessions | Announcements
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  RefreshCw,
  Plus,
  Trash2,
  CheckCircle,
  XCircle,
  Activity,
  AlertTriangle,
  Radio,
  Pencil,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import {
  useSpeakerStatus,
  useDeploySpeaker,
  useUpdateSpeaker,
  useDeleteSpeaker,
  useAnnouncements,
  useCreateAnnouncement,
  useDeleteAnnouncement,
  useBgpSessions,
  useBgpConfs,
  useUpsertBgpConf,
  useDeleteBgpConf,
} from '../hooks/useBgp';
import { useSubnets } from '../hooks/useNetwork';
import { getGatewayConfigExamples, type GatewayConfigExample } from '../api/bgp';
import type {
  SpeakerDeployRequest,
  AnnouncementRequest,
  BgpConfRequest,
  BgpConfResponse,
} from '../types/bgp';
import { Modal } from '@/components/common/Modal';
import { ActionBar } from '@/components/common/ActionBar';
import { listNodes } from '../api/cluster';
import { useEgressGateways } from '../hooks/useEgressGateways';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function SessionStateBadge({ state }: { state: string }) {
  const established = state === 'Established';
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium',
        established
          ? 'text-emerald-400 bg-emerald-500/10'
          : 'text-red-400 bg-red-500/10',
      )}
    >
      {established ? (
        <CheckCircle className="h-3.5 w-3.5" />
      ) : (
        <XCircle className="h-3.5 w-3.5" />
      )}
      {state}
    </span>
  );
}

function Toggle({
  enabled,
  onChange,
  disabled,
}: {
  enabled: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!enabled)}
      className={clsx(
        'relative inline-flex h-6 w-11 items-center rounded-full transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
        enabled ? 'bg-primary-500' : 'bg-surface-600',
      )}
    >
      <span
        className={clsx(
          'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
          enabled ? 'translate-x-6' : 'translate-x-1',
        )}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Speaker Form (used for both deploy and update)
// ---------------------------------------------------------------------------

const DEFAULT_FORM: SpeakerDeployRequest = {
  neighbor_address: '',
  neighbor_as: 65000,
  cluster_as: 65001,
  announce_cluster_ip: true,
  node_names: [],
};

function SpeakerForm({
  initial,
  isUpdate,
  onClose,
}: {
  initial?: Partial<SpeakerDeployRequest>;
  isUpdate: boolean;
  onClose: () => void;
}) {
  const [form, setForm] = useState<SpeakerDeployRequest>({
    ...DEFAULT_FORM,
    ...initial,
  });

  const deploy = useDeploySpeaker();
  const update = useUpdateSpeaker();
  const mutation = isUpdate ? update : deploy;

  const { data: nodesData } = useQuery({
    queryKey: ['cluster-nodes'],
    queryFn: listNodes,
  });
  const workerNodes = (nodesData?.items ?? []).filter(
    (n) => !n.roles.includes('control-plane') || n.roles.includes('worker'),
  );

  const set = <K extends keyof SpeakerDeployRequest>(
    field: K,
    value: SpeakerDeployRequest[K],
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const toggleNode = (name: string) => {
    setForm((prev) => ({
      ...prev,
      node_names: prev.node_names.includes(name)
        ? prev.node_names.filter((n) => n !== name)
        : [...prev.node_names, name],
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await mutation.mutateAsync(form);
    onClose();
  };

  const isValid = form.neighbor_address.length > 0 && form.node_names.length > 0;

  return (
    <Modal
      isOpen
      onClose={onClose}
      title={isUpdate ? 'Update BGP Speaker' : 'Deploy BGP Speaker'}
      size="lg"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Neighbor Address */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">Neighbor Address</label>
          <input
            type="text"
            value={form.neighbor_address}
            onChange={(e) => set('neighbor_address', e.target.value)}
            placeholder="192.168.196.200"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
            required
          />
        </div>

        {/* ASN fields */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-surface-300 mb-1">Neighbor ASN</label>
            <input
              type="number"
              value={form.neighbor_as}
              onChange={(e) => set('neighbor_as', Number(e.target.value))}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
          <div>
            <label className="block text-sm text-surface-300 mb-1">Cluster ASN</label>
            <input
              type="number"
              value={form.cluster_as}
              onChange={(e) => set('cluster_as', Number(e.target.value))}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
        </div>

        {/* Announce ClusterIP */}
        <div className="flex items-center justify-between p-3 bg-surface-900 rounded-lg border border-surface-700">
          <div>
            <p className="text-sm font-medium text-surface-200">Announce ClusterIP</p>
            <p className="text-xs text-surface-500 mt-0.5">
              Announce ClusterIP services via BGP
            </p>
          </div>
          <Toggle
            enabled={form.announce_cluster_ip}
            onChange={(v) => set('announce_cluster_ip', v)}
          />
        </div>

        {/* Node Selection */}
        <div>
          <label className="block text-sm text-surface-300 mb-2">
            Nodes{' '}
            <span className="text-surface-500 text-xs">
              (select nodes to label with ovn.kubernetes.io/bgp=true)
            </span>
          </label>
          {workerNodes.length === 0 ? (
            <p className="text-sm text-surface-500 italic">Loading nodes...</p>
          ) : (
            <div className="space-y-1.5">
              {workerNodes.map((node) => (
                <label
                  key={node.name}
                  className="flex items-center gap-3 p-2.5 bg-surface-900 border border-surface-700 rounded-lg cursor-pointer hover:border-surface-600 transition-colors"
                >
                  <input
                    type="checkbox"
                    checked={form.node_names.includes(node.name)}
                    onChange={() => toggleNode(node.name)}
                    className="accent-primary-500"
                  />
                  <span className="font-mono text-sm text-surface-200">{node.name}</span>
                  <span className="text-xs text-surface-500 ml-auto">{node.roles.join(', ')}</span>
                </label>
              ))}
            </div>
          )}
        </div>

        {mutation.isError && (
          <div className="flex items-start gap-2 p-3 bg-red-900/10 border border-red-800/30 rounded-lg">
            <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
            <p className="text-sm text-red-400">
              {(mutation.error as Error)?.message || 'Operation failed'}
            </p>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || mutation.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {mutation.isPending
              ? isUpdate
                ? 'Updating...'
                : 'Deploying...'
              : isUpdate
              ? 'Update Speaker'
              : 'Deploy Speaker'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Announce Resource Modal
// ---------------------------------------------------------------------------

const RESOURCE_TYPES = ['subnet', 'service', 'eip'] as const;

function AnnounceModal({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState<AnnouncementRequest>({
    resource_type: 'subnet',
    resource_name: '',
    resource_namespace: '',
    policy: 'cluster',
  });
  const createAnnouncement = useCreateAnnouncement();
  const { data: subnets, isLoading: subnetsLoading } = useSubnets();
  // Filter out the kube-ovn system subnets that operators shouldn't announce
  const subnetOptions = (subnets || [])
    .filter((s: any) => s.name !== 'join' && s.name !== 'ovn-default')
    .sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''));

  const set = <K extends keyof AnnouncementRequest>(
    field: K,
    value: AnnouncementRequest[K],
  ) => setForm((prev) => ({ ...prev, [field]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await createAnnouncement.mutateAsync(form);
    onClose();
  };

  const isValid =
    form.resource_name.length > 0 &&
    (form.resource_type !== 'service' || form.resource_namespace.length > 0);

  return (
    <Modal isOpen onClose={onClose} title="Announce Resource via BGP">
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Resource Type */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">Resource Type</label>
          <div className="flex gap-2">
            {RESOURCE_TYPES.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setForm((prev) => ({ ...prev, resource_type: t, resource_name: '' }))}
                className={clsx(
                  'px-4 py-2 rounded-lg border text-sm font-medium transition-colors capitalize',
                  form.resource_type === t
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 bg-surface-900 text-surface-300 hover:border-surface-600',
                )}
              >
                {t}
              </button>
            ))}
          </div>
        </div>

        {/* Resource Name — dropdown for subnet, free-text otherwise */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">
            {form.resource_type === 'subnet' ? 'Subnet' :
             form.resource_type === 'eip' ? 'EIP Name' : 'Service Name'}
          </label>
          {form.resource_type === 'subnet' ? (
            <select
              value={form.resource_name}
              onChange={(e) => set('resource_name', e.target.value)}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 focus:outline-none focus:border-primary-500 text-sm"
              required
              disabled={subnetsLoading}
            >
              <option value="">
                {subnetsLoading ? 'Loading…' : (subnetOptions.length ? 'Select a subnet' : 'No subnets available')}
              </option>
              {subnetOptions.map((s: any) => (
                <option key={s.name} value={s.name}>
                  {s.vpc ? `${s.vpc} / ${s.name}` : s.name}
                  {s.cidr_block ? ` — ${s.cidr_block}` : ''}
                </option>
              ))}
            </select>
          ) : (
            <input
              type="text"
              value={form.resource_name}
              onChange={(e) => set('resource_name', e.target.value)}
              placeholder={form.resource_type === 'eip' ? 'e.g. my-eip' : 'e.g. my-service'}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          )}
        </div>

        {/* Namespace (only for services) */}
        {form.resource_type === 'service' && (
          <div>
            <label className="block text-sm text-surface-300 mb-1">Namespace</label>
            <input
              type="text"
              value={form.resource_namespace}
              onChange={(e) => set('resource_namespace', e.target.value)}
              placeholder="default"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
              required
            />
          </div>
        )}

        {/* Policy */}
        <div>
          <label className="block text-sm text-surface-300 mb-1">Policy</label>
          <div className="flex gap-2">
            {(['cluster', 'local'] as const).map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => set('policy', p)}
                className={clsx(
                  'px-4 py-2 rounded-lg border text-sm font-medium transition-colors capitalize',
                  form.policy === p
                    ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                    : 'border-surface-700 bg-surface-900 text-surface-300 hover:border-surface-600',
                )}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {createAnnouncement.isError && (
          <p className="text-sm text-red-400">
            {(createAnnouncement.error as Error)?.message || 'Failed to add announcement'}
          </p>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || createAnnouncement.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {createAnnouncement.isPending ? 'Announcing...' : 'Announce'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Gateway BGP config (BgpConf)
// ---------------------------------------------------------------------------

const EMPTY_CONF: BgpConfRequest = {
  name: '',
  local_asn: 65001,
  peer_asn: 65000,
  neighbours: [],
  graceful_restart: true,
  hold_time: '30s',
  keepalive_time: '10s',
};

/**
 * The one thing an egress gateway needs before it can announce anything.
 *
 * The backend has had full CRUD for these all along and the hooks existed
 * unused, so the create-gateway form's "create one under Network → BGP
 * Peering first" pointed at a page with no such control — the only way to get
 * a BgpConf was the API. A gateway without one comes up with `bgp: None` and
 * its tenants' prefixes are never announced.
 */
function BgpConfModal({
  initial,
  onClose,
}: {
  initial?: BgpConfResponse;
  onClose: () => void;
}) {
  const [form, setForm] = useState<BgpConfRequest>(
    initial
      ? {
          name: initial.name,
          local_asn: initial.local_asn,
          peer_asn: initial.peer_asn,
          neighbours: initial.neighbours,
          graceful_restart: initial.graceful_restart,
          hold_time: initial.hold_time,
          keepalive_time: initial.keepalive_time,
        }
      : EMPTY_CONF,
  );
  const [neighbourInput, setNeighbourInput] = useState('');
  const upsert = useUpsertBgpConf();

  const isValid =
    (form.name ?? '').length > 0 &&
    form.local_asn > 0 &&
    form.peer_asn > 0 &&
    form.neighbours.length > 0;

  const addNeighbours = (raw: string) => {
    const added = raw
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0 && !form.neighbours.includes(s));
    if (added.length) setForm((f) => ({ ...f, neighbours: [...f.neighbours, ...added] }));
    setNeighbourInput('');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await upsert.mutateAsync(form);
    onClose();
  };

  return (
    <Modal isOpen onClose={onClose} title={initial ? `Edit ${initial.name}` : 'New Gateway BGP Config'}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-surface-300 mb-1">Name</label>
          <input
            type="text"
            value={form.name ?? ''}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            disabled={!!initial}
            placeholder="lab-gateway-common"
            className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500 disabled:opacity-60"
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-surface-300 mb-1">Local ASN</label>
            <input
              type="number"
              value={form.local_asn}
              onChange={(e) => setForm((f) => ({ ...f, local_asn: Number(e.target.value) }))}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500"
            />
            <p className="text-xs text-surface-500 mt-1">The gateway's own AS.</p>
          </div>
          <div>
            <label className="block text-sm text-surface-300 mb-1">Peer ASN</label>
            <input
              type="number"
              value={form.peer_asn}
              onChange={(e) => setForm((f) => ({ ...f, peer_asn: Number(e.target.value) }))}
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500"
            />
            <p className="text-xs text-surface-500 mt-1">The upstream router's AS.</p>
          </div>
        </div>

        <div>
          <label className="block text-sm text-surface-300 mb-1">Neighbours</label>
          <div className="flex gap-2">
            <input
              type="text"
              value={neighbourInput}
              onChange={(e) => setNeighbourInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addNeighbours(neighbourInput);
                }
              }}
              placeholder="10.199.4.254"
              className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500"
            />
            <button
              type="button"
              onClick={() => addNeighbours(neighbourInput)}
              disabled={!neighbourInput.trim()}
              className="btn-secondary disabled:opacity-50"
            >
              Add
            </button>
          </div>
          {form.neighbours.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {form.neighbours.map((n) => (
                <span
                  key={n}
                  className="inline-flex items-center gap-1 px-2 py-0.5 bg-surface-800 border border-surface-700 rounded text-xs font-mono text-surface-300"
                >
                  {n}
                  <button
                    type="button"
                    onClick={() =>
                      setForm((f) => ({ ...f, neighbours: f.neighbours.filter((x) => x !== n) }))
                    }
                    className="text-surface-500 hover:text-red-400"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <p className="text-xs text-surface-500 mt-1">
            Addresses the gateway's FRR peers with. The router needs a matching
            neighbour statement for each gateway pod address.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-surface-300 mb-1">Hold time</label>
            <input
              type="text"
              value={form.hold_time ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, hold_time: e.target.value }))}
              placeholder="30s"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500"
            />
          </div>
          <div>
            <label className="block text-sm text-surface-300 mb-1">Keepalive</label>
            <input
              type="text"
              value={form.keepalive_time ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, keepalive_time: e.target.value }))}
              placeholder="10s"
              className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 text-sm font-mono focus:outline-none focus:border-primary-500"
            />
          </div>
        </div>

        <div className="flex items-center justify-between p-3 bg-surface-900 rounded-lg border border-surface-700">
          <div>
            <p className="text-sm font-medium text-surface-200">Graceful restart</p>
            <p className="text-xs text-surface-500 mt-0.5">
              Keep the router forwarding through these prefixes while the
              gateway pods roll.
            </p>
          </div>
          <Toggle
            enabled={form.graceful_restart ?? true}
            onChange={(v) => setForm((f) => ({ ...f, graceful_restart: v }))}
          />
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || upsert.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
          >
            {upsert.isPending ? 'Saving...' : 'Save'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function BgpPeering() {
  const [showDeploy, setShowDeploy] = useState(false);
  const [showUpdate, setShowUpdate] = useState(false);
  const [showAnnounce, setShowAnnounce] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [showGatewayConfig, setShowGatewayConfig] = useState(false);
  const [gatewayConfigs, setGatewayConfigs] = useState<GatewayConfigExample[]>([]);
  const [activeConfigTab, setActiveConfigTab] = useState('frr');

  const { data: speaker, isLoading: speakerLoading, refetch: refetchSpeaker } = useSpeakerStatus();
  const { data: sessions, isLoading: sessionsLoading, refetch: refetchSessions } = useBgpSessions();
  const { data: announcements, isLoading: announcementsLoading } = useAnnouncements();
  // Egress gateways run their own FRR against a BgpConf. Their sessions never
  // appear in the speaker's view, which is the only view this page had.
  const { data: gatewaysData } = useEgressGateways();
  // The BgpConf list is a page section now, not just a dropdown source.
  const { data: confs, isLoading: confsLoading } = useBgpConfs();
  // `{}` opens the form empty; a conf opens it for editing.
  const [editConf, setEditConf] = useState<BgpConfResponse | {} | null>(null);
  const [deleteConf, setDeleteConf] = useState<string | null>(null);
  const deleteBgpConf = useDeleteBgpConf();
  const bgpGateways = (gatewaysData?.items ?? []).filter((g) => g.bgp_conf);
  const deleteSpeaker = useDeleteSpeaker();
  const deleteAnnouncement = useDeleteAnnouncement();

  const handleRefresh = () => {
    refetchSpeaker();
    refetchSessions();
  };

  // Build initial form values from current speaker config
  const speakerInitial: Partial<SpeakerDeployRequest> | undefined = speaker?.deployed
    ? {
        neighbor_address: speaker.config['neighbor-address'] ?? '',
        neighbor_as: Number(speaker.config['neighbor-as'] ?? 65000),
        cluster_as: Number(speaker.config['cluster-as'] ?? 65001),
        announce_cluster_ip: speaker.config['announce-cluster-ip'] === 'true',
        node_names: speaker.node_labels,
      }
    : undefined;

  return (
    <div className="space-y-6">
      <ActionBar
        title="BGP Peering"
        subtitle="Manage kube-ovn-speaker and BGP route announcements"
      >
        <button onClick={handleRefresh} className="btn-secondary" title="Refresh">
          <RefreshCw className="h-4 w-4" />
        </button>
        {speaker?.deployed ? (
          <>
            <button onClick={() => setShowUpdate(true)} className="btn-secondary">
              Update Speaker
            </button>
            <button
              onClick={() => setDeleteConfirm(true)}
              className="flex items-center gap-2 px-3 py-2 rounded-lg border border-red-800/50 text-red-400 hover:bg-red-900/20 text-sm font-medium transition-colors"
            >
              <Trash2 className="h-4 w-4" />
              Remove Speaker
            </button>
          </>
        ) : (
          <button onClick={() => setShowDeploy(true)} className="btn-primary">
            <Plus className="w-4 h-4" />
            Deploy Speaker
          </button>
        )}
      </ActionBar>

      {/* ------------------------------------------------------------------ */}
      {/* Section 1: Speaker Status                                           */}
      {/* ------------------------------------------------------------------ */}
      <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider mb-4">
          Speaker Status
        </h2>

        {speakerLoading ? (
          <p className="text-sm text-surface-500">Loading...</p>
        ) : !speaker?.deployed ? (
          <div className="flex items-center gap-3">
            <XCircle className="h-5 w-5 text-surface-500" />
            <div>
              <p className="text-sm font-medium text-surface-300">Speaker Not Deployed</p>
              <p className="text-xs text-surface-500 mt-0.5">
                Deploy kube-ovn-speaker to enable BGP route announcements.
              </p>
            </div>
            <button
              onClick={() => setShowDeploy(true)}
              className="ml-auto btn-primary"
            >
              <Plus className="w-4 h-4" />
              Deploy Speaker
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Status header */}
            <div className="flex items-center gap-3">
              <CheckCircle className="h-5 w-5 text-emerald-400" />
              <span className="text-sm font-medium text-emerald-400">Speaker Deployed</span>
              <span className="text-xs text-surface-500 ml-2">
                {speaker.pods.length} pod{speaker.pods.length !== 1 ? 's' : ''}
              </span>
            </div>

            {/* Config */}
            {Object.keys(speaker.config).length > 0 && (
              <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                {Object.entries(speaker.config).map(([k, v]) => (
                  <div key={k}>
                    <span className="text-xs text-surface-400">{k}</span>
                    <p className="font-mono text-surface-200 truncate">{v}</p>
                  </div>
                ))}
              </div>
            )}

            {/* Pods */}
            {speaker.pods.length > 0 && (
              <div className="border border-surface-700 rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-800/80 text-xs text-surface-400">
                      <th className="text-left px-4 py-2 font-medium">Pod</th>
                      <th className="text-left px-4 py-2 font-medium">Node</th>
                      <th className="text-left px-4 py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-surface-700">
                    {speaker.pods.map((pod) => (
                      <tr
                        key={pod.name}
                        className="bg-surface-800/50 hover:bg-surface-800 transition-colors"
                      >
                        <td className="px-4 py-2 font-mono text-surface-200 text-xs">{pod.name}</td>
                        <td className="px-4 py-2 font-mono text-surface-300 text-xs">{pod.node}</td>
                        <td className="px-4 py-2">
                          <span
                            className={clsx(
                              'text-xs font-medium',
                              pod.status === 'Running'
                                ? 'text-emerald-400'
                                : 'text-amber-400',
                            )}
                          >
                            {pod.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Section 2: BGP Sessions                                             */}
      {/* ------------------------------------------------------------------ */}
      <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">
            BGP Sessions
          </h2>
          <span className="text-xs text-surface-500">Auto-refresh every 10s</span>
        </div>

        {sessionsLoading ? (
          <p className="text-sm text-surface-500">Loading...</p>
        ) : !sessions || sessions.length === 0 ? (
          <div className="flex items-center gap-3 py-4">
            <Activity className="h-5 w-5 text-surface-600" />
            <p className="text-sm text-surface-500">
              No BGP sessions.{' '}
              {!speaker?.deployed && 'Deploy the speaker first.'}
            </p>
          </div>
        ) : (
          <div className="border border-surface-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800/80 text-xs text-surface-400">
                  <th className="text-left px-4 py-2 font-medium">Peer IP</th>
                  <th className="text-left px-4 py-2 font-medium">ASN</th>
                  <th className="text-left px-4 py-2 font-medium">State</th>
                  <th className="text-left px-4 py-2 font-medium">Announced</th>
                  <th className="text-left px-4 py-2 font-medium">Node</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-700">
                {sessions.map((s, i) => (
                  <tr
                    key={`${s.peer_address}-${s.node}-${i}`}
                    className="bg-surface-800/50 hover:bg-surface-800 transition-colors"
                  >
                    <td className="px-4 py-2 font-mono text-primary-400">{s.peer_address}</td>
                    <td className="px-4 py-2 font-mono text-surface-300">{s.peer_asn}</td>
                    <td className="px-4 py-2">
                      <SessionStateBadge state={s.state} />
                    </td>
                    <td className="px-4 py-2 text-surface-300">{s.announced}</td>
                    <td className="px-4 py-2 font-mono text-surface-400 text-xs">{s.node || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Gateway BGP configs — the prerequisite the create form points at     */}
      {/* ------------------------------------------------------------------ */}
      <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">
              Gateway BGP Config
            </h2>
            <p className="text-xs text-surface-500 mt-1">
              What an egress gateway's FRR peers with. A gateway created without
              one comes up unpeered and none of its tenants' prefixes are
              announced.
            </p>
          </div>
          <button
            onClick={() => setEditConf({})}
            className="flex items-center gap-1.5 text-xs text-primary-400 hover:text-primary-300 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            New config
          </button>
        </div>

        {confsLoading ? (
          <p className="text-sm text-surface-500 italic">Loading...</p>
        ) : (confs?.items ?? []).length === 0 ? (
          <p className="text-sm text-surface-500 italic">
            No BGP config yet. Create one here before creating a routed egress gateway.
          </p>
        ) : (
          <div className="border border-surface-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800/80 text-xs text-surface-400">
                  <th className="text-left px-4 py-2 font-medium">Name</th>
                  <th className="text-left px-4 py-2 font-medium">Local AS</th>
                  <th className="text-left px-4 py-2 font-medium">Peer AS</th>
                  <th className="text-left px-4 py-2 font-medium">Neighbours</th>
                  <th className="text-left px-4 py-2 font-medium">Timers</th>
                  <th className="w-20" />
                </tr>
              </thead>
              <tbody>
                {(confs?.items ?? []).map((c) => {
                  const inUse = bgpGateways.filter((g) => g.bgp_conf === c.name);
                  return (
                    <tr key={c.name} className="border-t border-surface-800">
                      <td className="px-4 py-2 font-mono text-surface-200">
                        {c.name}
                        {inUse.length > 0 && (
                          <span className="ml-2 text-xs text-surface-500">
                            used by {inUse.map((g) => g.name).join(', ')}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2 font-mono text-surface-300">{c.local_asn}</td>
                      <td className="px-4 py-2 font-mono text-surface-300">{c.peer_asn}</td>
                      <td className="px-4 py-2 font-mono text-primary-400">
                        {c.neighbours.join(', ') || '-'}
                      </td>
                      <td className="px-4 py-2 text-surface-400 text-xs">
                        hold {c.hold_time || '-'} / keepalive {c.keepalive_time || '-'}
                        {c.graceful_restart ? ' · graceful' : ''}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-1 justify-end">
                          <button
                            onClick={() => setEditConf(c)}
                            className="p-1 text-surface-500 hover:text-primary-400 transition-colors"
                            title="Edit"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => setDeleteConf(c.name)}
                            // Deleting a config a gateway is announcing through
                            // silently unpeers it, so that case is refused
                            // rather than confirmed.
                            disabled={inUse.length > 0}
                            className="p-1 text-surface-500 hover:text-red-400 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                            title={
                              inUse.length > 0
                                ? `In use by ${inUse.map((g) => g.name).join(', ')}`
                                : 'Delete'
                            }
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Egress gateway sessions — announced through FRR, not the speaker     */}
      {/* ------------------------------------------------------------------ */}
      {bgpGateways.length > 0 && (
        <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider mb-4">
            Egress Gateway Sessions
          </h2>
          <div className="border border-surface-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800/80 text-xs text-surface-400">
                  <th className="text-left px-4 py-2 font-medium">Gateway</th>
                  <th className="text-left px-4 py-2 font-medium">BgpConf</th>
                  <th className="text-left px-4 py-2 font-medium">External IPs</th>
                  <th className="text-left px-4 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {bgpGateways.map((g) => (
                  <tr key={g.name} className="border-t border-surface-800">
                    <td className="px-4 py-2 font-mono text-surface-200">{g.name}</td>
                    <td className="px-4 py-2 font-mono text-surface-400">{g.bgp_conf}</td>
                    <td className="px-4 py-2 font-mono text-primary-400">
                      {(g.external_ips ?? []).join(', ') || '-'}
                    </td>
                    <td className="px-4 py-2">
                      {g.degraded_reason ? (
                        <span className="text-orange-400" title={g.degraded_reason}>Degraded</span>
                      ) : g.ready ? (
                        <span className="text-emerald-400">Ready</span>
                      ) : (
                        <span className="text-amber-400">Not Ready</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Section 3: Announcements                                            */}
      {/* ------------------------------------------------------------------ */}
      <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">
            Announcements
          </h2>
          <button
            onClick={() => setShowAnnounce(true)}
            className="flex items-center gap-1.5 text-xs text-primary-400 hover:text-primary-300 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            Announce Resource
          </button>
        </div>

        {announcementsLoading ? (
          <p className="text-sm text-surface-500">Loading...</p>
        ) : !announcements || announcements.length === 0 ? (
          <div className="flex items-center gap-3 py-4">
            <Radio className="h-5 w-5 text-surface-600" />
            <p className="text-sm text-surface-500">No BGP announcements configured.</p>
          </div>
        ) : (
          <div className="border border-surface-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800/80 text-xs text-surface-400">
                  <th className="text-left px-4 py-2 font-medium">Resource</th>
                  <th className="text-left px-4 py-2 font-medium">Type</th>
                  <th className="text-left px-4 py-2 font-medium">BGP</th>
                  <th className="text-left px-4 py-2 font-medium">Policy</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-700">
                {announcements.map((ann) => {
                  const req: AnnouncementRequest = {
                    resource_type: ann.resource_type,
                    resource_name: ann.resource_name,
                    resource_namespace: ann.resource_namespace,
                    policy: ann.policy,
                  };
                  return (
                    <tr
                      key={`${ann.resource_type}/${ann.resource_namespace ? ann.resource_namespace + '/' : ''}${ann.resource_name}`}
                      className="bg-surface-800/50 hover:bg-surface-800 transition-colors"
                    >
                      <td className="px-4 py-2 font-mono text-surface-200">
                        {ann.resource_namespace
                          ? `${ann.resource_namespace}/${ann.resource_name}`
                          : ann.resource_name}
                      </td>
                      <td className="px-4 py-2">
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-700 text-surface-300 capitalize">
                          {ann.resource_type}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <Toggle
                          enabled={ann.bgp_enabled}
                          onChange={(enabled) => {
                            if (!enabled) {
                              deleteAnnouncement.mutate(req);
                            }
                          }}
                          disabled={deleteAnnouncement.isPending}
                        />
                      </td>
                      <td className="px-4 py-2 text-surface-400 text-xs capitalize">
                        {ann.policy || '-'}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <button
                          onClick={() => deleteAnnouncement.mutate(req)}
                          disabled={deleteAnnouncement.isPending}
                          className="p-1 text-surface-500 hover:text-red-400 rounded transition-colors"
                          title="Remove announcement"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Modals                                                               */}
      {/* ------------------------------------------------------------------ */}

      {showDeploy && (
        <SpeakerForm isUpdate={false} onClose={() => setShowDeploy(false)} />
      )}

      {showUpdate && (
        <SpeakerForm
          isUpdate
          initial={speakerInitial}
          onClose={() => setShowUpdate(false)}
        />
      )}

      {showAnnounce && <AnnounceModal onClose={() => setShowAnnounce(false)} />}

      {editConf && (
        <BgpConfModal
          initial={'name' in editConf ? (editConf as BgpConfResponse) : undefined}
          onClose={() => setEditConf(null)}
        />
      )}

      {deleteConf && (
        <Modal isOpen onClose={() => setDeleteConf(null)} title="Delete BGP Config" size="sm">
          <p className="text-sm text-surface-400 text-center mb-4">
            Delete <span className="font-mono text-surface-200">{deleteConf}</span>? Any
            gateway that later names it will come up unpeered.
          </p>
          <div className="flex justify-end gap-2">
            <button onClick={() => setDeleteConf(null)} className="btn-secondary">
              Cancel
            </button>
            <button
              onClick={async () => {
                await deleteBgpConf.mutateAsync(deleteConf);
                setDeleteConf(null);
              }}
              disabled={deleteBgpConf.isPending}
              className="px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {deleteBgpConf.isPending ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </Modal>
      )}

      {/* Gateway Config Examples — only visible once speaker DaemonSet is deployed */}
      {speaker?.deployed && (
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-surface-200 flex items-center gap-2">
            <Radio className="w-4 h-4 text-primary-400" />
            Gateway Router Configuration
          </h2>
          <button
            onClick={async () => {
              if (!showGatewayConfig) {
                try {
                  const configs = await getGatewayConfigExamples();
                  setGatewayConfigs(configs);
                } catch { /* ignore */ }
              }
              setShowGatewayConfig(!showGatewayConfig);
            }}
            className="btn-secondary text-xs"
          >
            {showGatewayConfig ? 'Hide' : 'Show Config Examples'}
          </button>
        </div>

        {!showGatewayConfig && (
          <p className="text-xs text-surface-500">
            Example configurations for your BGP gateway router (FRR, BIRD) with actual ASNs and node IPs pre-filled.
          </p>
        )}

        {showGatewayConfig && gatewayConfigs.length > 0 && (
          <div>
            <div className="flex gap-1 mb-3">
              {gatewayConfigs.map((cfg) => (
                <button
                  key={cfg.name}
                  onClick={() => setActiveConfigTab(cfg.name)}
                  className={clsx(
                    'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
                    activeConfigTab === cfg.name
                      ? 'bg-primary-500/20 text-primary-400 border border-primary-500/40'
                      : 'bg-surface-700 text-surface-400 hover:bg-surface-600 border border-transparent',
                  )}
                >
                  {cfg.title}
                </button>
              ))}
            </div>
            {gatewayConfigs.filter((c) => c.name === activeConfigTab).map((cfg) => (
              <div key={cfg.name}>
                <p className="text-xs text-surface-400 mb-2">{cfg.description}</p>
                <div className="relative">
                  <pre className="bg-surface-900 border border-surface-700 rounded-lg p-4 text-xs text-surface-300 font-mono overflow-auto max-h-72 whitespace-pre">
                    {cfg.config}
                  </pre>
                  <button
                    onClick={() => navigator.clipboard.writeText(cfg.config)}
                    className="absolute top-2 right-2 px-2 py-1 bg-surface-700 hover:bg-surface-600 rounded text-xs text-surface-400 transition-colors"
                  >
                    Copy
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      )}

      {deleteConfirm && (
        <Modal
          isOpen
          onClose={() => setDeleteConfirm(false)}
          title="Remove BGP Speaker"
          size="sm"
        >
          <p className="text-sm text-surface-400 text-center mb-4">
            Remove the kube-ovn-speaker DaemonSet and clear all BGP node labels? This will stop
            all BGP route advertisements.
          </p>
          <div className="flex gap-3">
            <button
              onClick={() => setDeleteConfirm(false)}
              className="flex-1 btn-secondary"
            >
              Cancel
            </button>
            <button
              onClick={async () => {
                await deleteSpeaker.mutateAsync();
                setDeleteConfirm(false);
              }}
              disabled={deleteSpeaker.isPending}
              className="flex-1 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {deleteSpeaker.isPending ? 'Removing...' : 'Remove'}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

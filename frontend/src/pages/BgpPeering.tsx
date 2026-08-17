/**
 * BGP Peering Page
 *
 * Sections: Routed Egress | Gateway BGP Configs | Egress Gateway Sessions.
 *
 * kube-ovn-speaker used to own this page. It only ever announces the default
 * VPC, so on a stand where every tenant lives in its own VPC it is not merely
 * unhelpful — its "No BGP sessions. Deploy the speaker first." was shown next
 * to an Established session and five announced prefixes. The controls are
 * gone; the API and hooks stay, should upstream ever teach it custom VPCs.
 */

import { useState } from 'react';
import {
  RefreshCw,
  Plus,
  Trash2,
  Radio,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Pencil,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import {
  useRoutedEgress,
  useBgpConfs,
  useUpsertBgpConf,
  useDeleteBgpConf,
} from '../hooks/useBgp';
import { getGatewayConfigExamples, type GatewayConfigExample } from '../api/bgp';
import type {
  BgpConfRequest,
  BgpConfResponse,
} from '../types/bgp';
import { Modal } from '@/components/common/Modal';
import { ActionBar } from '@/components/common/ActionBar';
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
  const [showGatewayConfig, setShowGatewayConfig] = useState(false);
  const [gatewayConfigs, setGatewayConfigs] = useState<GatewayConfigExample[]>([]);
  const [activeConfigTab, setActiveConfigTab] = useState('bird');
  // The routed external plane is what this cluster actually announces.
  // kube-ovn-speaker was removed from this page: it announces from the default
  // VPC only, so for a custom VPC it has nothing to say — and its empty view
  // read as "no BGP here" while five prefixes were live.
  const { data: routed, refetch: refetchRouted } = useRoutedEgress();
  // Egress gateways run their own FRR against a BgpConf.
  const { data: gatewaysData } = useEgressGateways();
  // The BgpConf list is a page section now, not just a dropdown source.
  const { data: confs, isLoading: confsLoading } = useBgpConfs();
  // `{}` opens the form empty; a conf opens it for editing.
  const [editConf, setEditConf] = useState<BgpConfResponse | {} | null>(null);
  const [deleteConf, setDeleteConf] = useState<string | null>(null);
  const deleteBgpConf = useDeleteBgpConf();
  const bgpGateways = (gatewaysData?.items ?? []).filter((g) => g.bgp_conf);


  return (
    <div className="space-y-6">
      <ActionBar
        title="BGP Peering"
        subtitle="Routed egress announcements and gateway BGP configuration"
      >
        <button
          onClick={() => refetchRouted()}
          className="btn-secondary"
          title="Refresh"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
      </ActionBar>

      {/* ------------------------------------------------------------------ */}
      {/* Routed external plane — each VPC announced from its own router leg   */}
      {/* ------------------------------------------------------------------ */}
      <div className="bg-surface-800/50 border border-surface-700 rounded-xl p-5">
        <div className="mb-4">
          <h2 className="text-sm font-semibold text-surface-300 uppercase tracking-wider">
            Routed Egress
          </h2>
          <p className="text-xs text-surface-500 mt-1">
            Each VPC is announced to the upstream router with its own leg as the
            next hop — no gateway pods in the path.
          </p>
        </div>

        {!routed?.enabled ? (
          <p className="text-sm text-surface-500 italic">
            Not configured on this deployment (B3_BGP_PEER / B3_VPC_GATEWAY).
          </p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-surface-400">
              <span>peer <span className="font-mono text-surface-300">{routed.peer}</span></span>
              <span>local AS <span className="font-mono text-surface-300">{routed.local_asn}</span></span>
              <span>
                announced from{' '}
                <span className="font-mono text-surface-300">
                  {routed.nodes.join(', ') || '—'}
                </span>
              </span>
            </div>

            {Object.entries(routed.config_errors ?? {}).map(([node, err]) => (
              // FRR keeps the previous configuration when it refuses a new one,
              // so live announcements survive — and a newly attached VPC is
              // silently not added. That is the state worth shouting about.
              <div
                key={node}
                className="flex items-start gap-2 p-2.5 rounded-lg bg-red-900/10 border border-red-800/30"
              >
                <AlertTriangle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
                <div className="text-xs text-red-400">
                  <span className="font-mono">{node}</span> refused the generated
                  configuration — new VPCs are not being announced from it.
                  <div className="text-surface-400 mt-1 font-mono">{err}</div>
                </div>
              </div>
            ))}

            <div>
              <div className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">
                Sessions
              </div>
              {routed.sessions.length === 0 ? (
                <p className="text-sm text-surface-500 italic">
                  No session state reported yet.
                </p>
              ) : (
                <div className="border border-surface-700 rounded-lg overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-surface-800/80 text-xs text-surface-400">
                        <th className="text-left px-4 py-2 font-medium">Node</th>
                        <th className="text-left px-4 py-2 font-medium">Peer</th>
                        <th className="text-left px-4 py-2 font-medium">BGP</th>
                        <th className="text-left px-4 py-2 font-medium">BFD</th>
                      </tr>
                    </thead>
                    <tbody>
                      {routed.sessions.map((s) => (
                        <tr key={`${s.node}-${s.peer}`} className="border-t border-surface-800">
                          <td className="px-4 py-2 font-mono text-surface-200">{s.node}</td>
                          <td className="px-4 py-2 font-mono text-surface-400">{s.peer}</td>
                          <td className="px-4 py-2">
                            <SessionStateBadge state={s.status} />
                          </td>
                          <td className="px-4 py-2 text-surface-400">{s.bfd || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div>
              <div className="text-xs font-semibold text-surface-400 uppercase tracking-wider mb-2">
                Prefixes ({routed.intended.length})
              </div>
              {routed.intended.length === 0 ? (
                <p className="text-sm text-surface-500 italic">
                  No VPC is on the routed plane yet.
                </p>
              ) : (
                <div className="border border-surface-700 rounded-lg overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-surface-800/80 text-xs text-surface-400">
                        <th className="text-left px-4 py-2 font-medium">VPC</th>
                        <th className="text-left px-4 py-2 font-medium">Prefix</th>
                        <th className="text-left px-4 py-2 font-medium">Next hop</th>
                      </tr>
                    </thead>
                    <tbody>
                      {routed.intended.map((a) => (
                        <tr key={`${a.vpc}-${a.cidr}`} className="border-t border-surface-800">
                          <td className="px-4 py-2 font-mono text-surface-200">{a.vpc}</td>
                          <td className="px-4 py-2 font-mono text-surface-300">{a.cidr}</td>
                          <td className="px-4 py-2 font-mono text-primary-400">{a.next_hop}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-xs text-surface-500 mt-2">
                This is what the generator derived from the VPCs and handed to
                FRR. Whether the upstream router accepted each prefix is only
                visible on the router itself — a configuration can be flawless
                and advertise nothing.
              </p>
            </div>
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
      <div className="bg-surface-800 border border-surface-700 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-sm font-semibold text-surface-200 flex items-center gap-2">
              <Radio className="w-4 h-4 text-primary-400" />
              Upstream Router Configuration
            </h2>
            <p className="text-xs text-surface-500 mt-1">
              What the router on the other side of the session has to be told
              before any of the prefixes above are usable.
            </p>
          </div>
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
            Generated with this cluster's ASNs, node range and tenant supernet
            filled in — the filter comes from the same setting that allocates
            the VPCs, so a prefix this cluster hands out is a prefix the router
            is told to accept.
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

      {/* ------------------------------------------------------------------ */}
      {/* Modals                                                               */}
      {/* ------------------------------------------------------------------ */}

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


    </div>
  );
}

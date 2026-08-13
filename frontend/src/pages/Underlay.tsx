/**
 * Underlay fabric — the physical path VPC egress gateways need.
 *
 * The backend has built this since `vpc_underlay.py` landed, but nothing in
 * the UI ever called it, so on a fresh cluster the fabric had to be applied by
 * hand from lab manifests and a wizard-created VPC came up attached to
 * nothing. This page is the missing caller.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle,
  CircleDashed,
  Info,
  Loader2,
  MinusCircle,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import { useUnderlay, useEnsureUnderlay } from '../hooks/useUnderlay';
import { listNodes } from '../api/cluster';
import { useQuery } from '@tanstack/react-query';
import type { UnderlayObject } from '../api/underlay';

const inputCls =
  'w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 ' +
  'placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm';

interface FormState {
  interface: string;
  external_cidr: string;
  external_gateway: string;
  vlan_id: string;
  exclude_nodes: string[];
  exclude_ips: string;
  provider_network_name: string;
  vlan_name: string;
  subnet_name: string;
  link_watcher: boolean;
  /** null = let the backend read cilium-config and decide. */
  cilium_source_ip_exempt: boolean | null;
  cilium_namespace: string;
}

const DEFAULTS: FormState = {
  interface: '',
  external_cidr: '',
  external_gateway: '',
  vlan_id: '0',
  exclude_nodes: [],
  exclude_ips: '',
  provider_network_name: 'external',
  vlan_name: 'vlan-external',
  subnet_name: 'ext-sub',
  link_watcher: true,
  // Both left unset: the cluster's own cilium-config says whether Cilium
  // chains and which namespace it runs in. Defaulting to false/kube-system
  // here skipped the workaround on a cluster that needed it and offered
  // the wrong namespace when it was ticked by hand.
  cilium_source_ip_exempt: null,
  cilium_namespace: '',
};

function StateIcon({ state }: { state: string }) {
  switch (state) {
    case 'exists':
    case 'created':
      return <CheckCircle className="h-4 w-4 text-green-400" />;
    case 'missing':
      return <CircleDashed className="h-4 w-4 text-surface-500" />;
    case 'skipped':
      return <MinusCircle className="h-4 w-4 text-surface-500" />;
    default:
      return <XCircle className="h-4 w-4 text-red-400" />;
  }
}

function ObjectRow({ o }: { o: UnderlayObject }) {
  return (
    <div className="flex items-start gap-3 px-4 py-2.5 border-b border-surface-800 last:border-0">
      <div className="mt-0.5">
        <StateIcon state={o.state} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm text-surface-200">{o.kind}</span>
          <span className="font-mono text-sm text-surface-400">
            {o.namespace ? `${o.namespace}/${o.name}` : o.name}
          </span>
          {o.workaround && (
            <span
              className="px-1.5 py-0.5 text-[10px] font-medium rounded bg-amber-900/30 text-amber-300"
              title="A workaround for upstream behaviour, not architecture — delete it when the upstream bug is gone."
            >
              WORKAROUND
            </span>
          )}
        </div>
        {o.detail && <p className="text-xs text-surface-500 mt-0.5">{o.detail}</p>}
      </div>
      <span className="text-xs text-surface-500 uppercase shrink-0">{o.state}</span>
    </div>
  );
}

export function Underlay() {
  const [form, setForm] = useState<FormState>(DEFAULTS);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const names = {
    provider_network_name: form.provider_network_name,
    vlan_name: form.vlan_name,
    subnet_name: form.subnet_name,
  };
  const { data: status, isLoading, refetch, isFetching } = useUnderlay(names);
  const ensure = useEnsureUnderlay();

  const { data: nodes } = useQuery({ queryKey: ['nodes'], queryFn: listNodes });

  // Control planes usually lack the second NIC, and picking them is the
  // failure the ProviderNetwork's excludeNodes exists to prevent. Preselect
  // them so the common case needs no thought, but leave it editable.
  const nodeNames = useMemo(
    () => (nodes?.items ?? []).map((n) => n.name),
    [nodes],
  );
  const [nodesTouched, setNodesTouched] = useState(false);
  useEffect(() => {
    if (nodesTouched || !nodes?.items?.length) return;
    const cps = nodes.items
      .filter((n) => (n.roles || []).some((r) => r.includes('control-plane') || r.includes('master')))
      .map((n) => n.name);
    if (cps.length) setForm((f) => ({ ...f, exclude_nodes: cps }));
  }, [nodes, nodesTouched]);

  const isValid =
    form.interface.trim() !== '' &&
    form.external_cidr.trim() !== '' &&
    form.external_gateway.trim() !== '' &&
    form.provider_network_name.trim() !== '' &&
    form.vlan_name.trim() !== '' &&
    form.subnet_name.trim() !== '';

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!isValid) return;
    ensure.mutate({
      interface: form.interface.trim(),
      external_cidr: form.external_cidr.trim(),
      external_gateway: form.external_gateway.trim(),
      vlan_id: Number(form.vlan_id) || 0,
      exclude_nodes: form.exclude_nodes,
      exclude_ips: form.exclude_ips
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter(Boolean),
      provider_network_name: form.provider_network_name.trim(),
      vlan_name: form.vlan_name.trim(),
      subnet_name: form.subnet_name.trim(),
      link_watcher: form.link_watcher,
      cilium_source_ip_exempt: form.cilium_source_ip_exempt,
      cilium_namespace: form.cilium_namespace.trim() || 'kube-system',
    });
  };

  const shown = ensure.data ?? status;

  return (
    <div className="space-y-4">
      {/* Status */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <div>
            <h2 className="text-base font-medium text-surface-100">Underlay Fabric</h2>
            <p className="text-sm text-surface-400">
              ProviderNetwork, VLAN and external subnet that VPC egress gateways attach to
            </p>
          </div>
          <button onClick={() => refetch()} className="btn-secondary flex items-center gap-2" title="Refresh">
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {isLoading ? (
          <div className="p-8 flex justify-center">
            <Loader2 className="h-6 w-6 text-primary-400 animate-spin" />
          </div>
        ) : shown ? (
          <>
            <div
              className={`mx-4 mt-4 flex gap-3 p-3 rounded-lg border ${
                shown.ready
                  ? 'bg-green-900/10 border-green-800/30'
                  : 'bg-amber-900/10 border-amber-800/30'
              }`}
            >
              {shown.ready ? (
                <CheckCircle className="w-4 h-4 text-green-400 shrink-0 mt-0.5" />
              ) : (
                <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
              )}
              <p className={`text-xs ${shown.ready ? 'text-green-300/80' : 'text-amber-300/80'}`}>
                {shown.detail}
              </p>
            </div>
            <div className="card-body pt-4">
              <div className="border border-surface-800 rounded-lg overflow-hidden">
                {shown.objects.map((o) => (
                  <ObjectRow key={`${o.kind}/${o.namespace}/${o.name}`} o={o} />
                ))}
              </div>
            </div>
          </>
        ) : null}
      </div>

      {/* Build form */}
      <form onSubmit={submit} className="card">
        <div className="card-header">
          <h2 className="text-base font-medium text-surface-100">Build / Reconcile</h2>
          <p className="text-sm text-surface-400">
            Idempotent — run once per cluster, re-run freely
          </p>
        </div>
        <div className="card-body space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-surface-300 mb-2">Provider interface *</label>
              <input
                className={inputCls}
                placeholder="eth1"
                value={form.interface}
                onChange={(e) => set('interface', e.target.value)}
              />
              <p className="text-xs text-surface-500 mt-1">
                A dedicated NIC. Not the management interface — kube-ovn enslaves it into
                br-external and migrates its address to the bridge, which on Talos does not
                hold: the node loses its address and stays NotReady until rebooted.
              </p>
            </div>
            <div>
              <label className="block text-sm text-surface-300 mb-2">VLAN id</label>
              <input
                className={inputCls}
                placeholder="0"
                value={form.vlan_id}
                onChange={(e) => set('vlan_id', e.target.value)}
              />
              <p className="text-xs text-surface-500 mt-1">
                0 for untagged. Tagged frames do not always survive an overlay underneath —
                on OpenNebula VXLAN two pods on different workers could not ARP each other
                while untagged frames between the same NICs were fine.
              </p>
            </div>
            <div>
              <label className="block text-sm text-surface-300 mb-2">External CIDR *</label>
              <input
                className={inputCls}
                placeholder="10.198.176.0/20"
                value={form.external_cidr}
                onChange={(e) => set('external_cidr', e.target.value)}
              />
            </div>
            <div>
              <label className="block text-sm text-surface-300 mb-2">External gateway *</label>
              <input
                className={inputCls}
                placeholder="10.198.191.254"
                value={form.external_gateway}
                onChange={(e) => set('external_gateway', e.target.value)}
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-surface-300 mb-2">Excluded IP ranges</label>
            <textarea
              className={`${inputCls} h-20`}
              placeholder={'10.198.176.1..10.198.190.199\n10.198.190.221..10.198.191.254'}
              value={form.exclude_ips}
              onChange={(e) => set('exclude_ips', e.target.value)}
            />
            <p className="text-xs text-surface-500 mt-1">
              One range per line (or comma-separated). Addresses kube-ovn must not hand out —
              anything already leased on that segment by the hosting fabric.
            </p>
          </div>

          <div>
            <label className="block text-sm text-surface-300 mb-2">Nodes without the NIC</label>
            <div className="flex flex-wrap gap-2">
              {nodeNames.length === 0 && (
                <span className="text-xs text-surface-500">No nodes loaded</span>
              )}
              {nodeNames.map((n) => {
                const on = form.exclude_nodes.includes(n);
                return (
                  <button
                    key={n}
                    type="button"
                    onClick={() => {
                      setNodesTouched(true);
                      set(
                        'exclude_nodes',
                        on ? form.exclude_nodes.filter((x) => x !== n) : [...form.exclude_nodes, n],
                      );
                    }}
                    className={`px-2.5 py-1 rounded-lg text-xs font-mono border transition-colors ${
                      on
                        ? 'bg-surface-700 border-surface-600 text-surface-200'
                        : 'bg-surface-900 border-surface-800 text-surface-500 hover:text-surface-300'
                    }`}
                  >
                    {n}
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-surface-500 mt-1">
              Selected nodes are excluded from the provider network. Control planes are
              preselected because they typically have a single NIC.
            </p>
          </div>

          <details className="border border-surface-800 rounded-lg">
            <summary className="px-4 py-2.5 text-sm text-surface-300 cursor-pointer select-none">
              Object names and workarounds
            </summary>
            <div className="px-4 pb-4 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                  <label className="block text-sm text-surface-300 mb-2">ProviderNetwork</label>
                  <input
                    className={inputCls}
                    value={form.provider_network_name}
                    onChange={(e) => set('provider_network_name', e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-2">VLAN</label>
                  <input
                    className={inputCls}
                    value={form.vlan_name}
                    onChange={(e) => set('vlan_name', e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-sm text-surface-300 mb-2">Subnet</label>
                  <input
                    className={inputCls}
                    value={form.subnet_name}
                    onChange={(e) => set('subnet_name', e.target.value)}
                  />
                </div>
              </div>

              <label className="flex items-start gap-3">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={form.link_watcher}
                  onChange={(e) => set('link_watcher', e.target.checked)}
                />
                <span className="text-sm text-surface-300">
                  Deploy <span className="font-mono">provider-link-up</span>
                  <span className="block text-xs text-surface-500">
                    kube-ovn raises the provider NIC once at bridge init and never rechecks;
                    where it drops back DOWN, OVS still lists the port, pods still get
                    addresses, and every frame is silently swallowed. Turn off only where the
                    link is known to stay up.
                  </span>
                </span>
              </label>

              <label className="flex items-start gap-3">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={form.cilium_source_ip_exempt === true}
                  ref={(el) => {
                    // Unset is a third state: the server reads cilium-config
                    // and decides. Showing it as plain "off" is what let a
                    // cluster that chains Cilium be built without the
                    // workaround, reported as "not chaining Cilium".
                    if (el) el.indeterminate = form.cilium_source_ip_exempt === null;
                  }}
                  onChange={(e) => set('cilium_source_ip_exempt', e.target.checked)}
                />
                <span className="text-sm text-surface-300">
                  Deploy <span className="font-mono">cilium-gateway-exempt</span>
                  <span className="block text-xs text-surface-500">
                    {form.cilium_source_ip_exempt === null
                      ? 'Decided from the cluster: deployed when cilium-config says Cilium chains.'
                      : 'Needed when Cilium runs in chaining mode: it requires an endpoint to emit only its own source address, and an egress gateway forwards replies from the whole internet, which Cilium drops as "Invalid source ip".'}
                  </span>
                </span>
              </label>

              {form.cilium_source_ip_exempt === true && (
                <div>
                  <label className="block text-sm text-surface-300 mb-2">Cilium namespace</label>
                  <input
                    className={inputCls}
                    value={form.cilium_namespace}
                    onChange={(e) => set('cilium_namespace', e.target.value)}
                  />
                </div>
              )}
            </div>
          </details>

          <div className="flex gap-3 p-3 bg-blue-900/10 border border-blue-800/30 rounded-lg">
            <Info className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" />
            <p className="text-xs text-blue-300/80">
              A green result here means the objects exist, not that traffic flows. Confirm the
              provider NIC still shows growing tx counters some minutes later
              (<span className="font-mono">ovs-ofctl dump-ports br-external</span>) — that is
              the failure this fabric is most often lost to.
            </p>
          </div>

          <div className="flex justify-end">
            <button
              type="submit"
              disabled={!isValid || ensure.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
            >
              {ensure.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {ensure.isPending ? 'Building...' : 'Build Underlay'}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

export default Underlay;

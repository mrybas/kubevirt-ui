/**
 * CiliumRuleBuilder — visual builder for Cilium network policy rules.
 * Generates a CiliumNetworkPolicy/CiliumClusterwideNetworkPolicy spec object.
 * Used in both CiliumPolicies wizard and SecurityBaseline custom rules.
 */

import { useState } from 'react';
import { X, Info } from 'lucide-react';
import clsx from 'clsx';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CiliumRuleState {
  // Endpoint selector (which pods this applies to)
  endpointLabels: Record<string, string>;
  // Egress rules
  egressEnabled: boolean;
  egressDenyAll: boolean;
  egressAllowDns: boolean;
  egressAllowFqdns: string[];
  egressAllowCidrs: string[];
  egressAllowPorts: PortRule[];
  egressHttpEnabled: boolean;
  egressHttpMethods: string[];
  egressHttpPaths: string[];
  // Ingress rules
  ingressEnabled: boolean;
  ingressAllowNamespaces: string[];
  ingressAllowCidrs: string[];
  ingressAllowPorts: PortRule[];
}

interface PortRule {
  port: string;
  protocol: 'TCP' | 'UDP';
}

const DEFAULT_STATE: CiliumRuleState = {
  endpointLabels: {},
  egressEnabled: true,
  egressDenyAll: false,
  egressAllowDns: true,
  egressAllowFqdns: [],
  egressAllowCidrs: [],
  egressAllowPorts: [],
  egressHttpEnabled: false,
  egressHttpMethods: ['GET'],
  egressHttpPaths: [],
  ingressEnabled: false,
  ingressAllowNamespaces: [],
  ingressAllowCidrs: [],
  ingressAllowPorts: [],
};

// ---------------------------------------------------------------------------
// Spec Builder (state → CiliumNetworkPolicy spec)
// ---------------------------------------------------------------------------

export function buildCiliumSpec(state: CiliumRuleState): Record<string, unknown> {
  const spec: Record<string, unknown> = {};

  // Endpoint selector
  if (Object.keys(state.endpointLabels).length > 0) {
    spec.endpointSelector = { matchLabels: { ...state.endpointLabels } };
  } else {
    spec.endpointSelector = {};
  }

  // Egress
  if (state.egressEnabled) {
    if (state.egressDenyAll) {
      spec.egressDeny = [{ toCIDR: ['0.0.0.0/0'] }];
      // Still allow DNS if requested
      if (state.egressAllowDns) {
        spec.egress = [
          {
            toEndpoints: [{ matchLabels: { 'k8s:io.kubernetes.pod.namespace': 'kube-system' } }],
            toPorts: [{ ports: [{ port: '53', protocol: 'UDP' }, { port: '53', protocol: 'TCP' }] }],
          },
        ];
      }
    } else {
      const egress: Record<string, unknown>[] = [];

      // DNS
      if (state.egressAllowDns) {
        egress.push({
          toEndpoints: [{ matchLabels: { 'k8s:io.kubernetes.pod.namespace': 'kube-system' } }],
          toPorts: [{
            ports: [{ port: '53', protocol: 'UDP' }, { port: '53', protocol: 'TCP' }],
            rules: { dns: [{ matchPattern: '*' }] },
          }],
        });
      }

      // FQDNs
      if (state.egressAllowFqdns.length > 0) {
        egress.push({
          toFQDNs: state.egressAllowFqdns.map((d) =>
            d.startsWith('*') ? { matchPattern: d } : { matchName: d }
          ),
        });
      }

      // CIDRs
      if (state.egressAllowCidrs.length > 0) {
        egress.push({ toCIDR: state.egressAllowCidrs });
      }

      // Ports (L4)
      if (state.egressAllowPorts.length > 0) {
        egress.push({
          toPorts: [{
            ports: state.egressAllowPorts.map((p) => ({
              port: p.port,
              protocol: p.protocol,
            })),
          }],
        });
      }

      // HTTP (L7)
      if (state.egressHttpEnabled && state.egressHttpMethods.length > 0) {
        const httpRules = state.egressHttpMethods.map((method) => ({
          method,
          ...(state.egressHttpPaths.length > 0 ? { path: state.egressHttpPaths[0] } : {}),
        }));
        egress.push({
          toPorts: [{
            ports: [{ port: '80', protocol: 'TCP' }],
            rules: { http: httpRules },
          }],
        });
      }

      if (egress.length > 0) spec.egress = egress;
    }
  }

  // Ingress
  if (state.ingressEnabled) {
    const ingress: Record<string, unknown>[] = [];

    // From namespaces
    if (state.ingressAllowNamespaces.length > 0) {
      ingress.push({
        fromEndpoints: state.ingressAllowNamespaces.map((ns) => ({
          matchLabels: { 'k8s:io.kubernetes.pod.namespace': ns },
        })),
      });
    }

    // From CIDRs
    if (state.ingressAllowCidrs.length > 0) {
      ingress.push({ fromCIDR: state.ingressAllowCidrs });
    }

    // Ports
    if (state.ingressAllowPorts.length > 0) {
      ingress.push({
        toPorts: [{
          ports: state.ingressAllowPorts.map((p) => ({
            port: p.port,
            protocol: p.protocol,
          })),
        }],
      });
    }

    if (ingress.length > 0) spec.ingress = ingress;
  }

  return spec;
}

// ---------------------------------------------------------------------------
// Tag Input (reusable for FQDNs, CIDRs, namespaces)
// ---------------------------------------------------------------------------

function TagInput({
  tags,
  onAdd,
  onRemove,
  placeholder,
  validate,
}: {
  tags: string[];
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
  placeholder: string;
  validate?: (value: string) => boolean;
}) {
  const [input, setInput] = useState('');

  const add = () => {
    const v = input.trim();
    if (!v || tags.includes(v)) return;
    if (validate && !validate(v)) return;
    onAdd(v);
    setInput('');
  };

  return (
    <div>
      <div className="flex gap-2 mb-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }}
          placeholder={placeholder}
          className="flex-1 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
        />
        <button type="button" onClick={add} disabled={!input.trim()} className="px-3 py-1.5 bg-surface-700 hover:bg-surface-600 disabled:opacity-50 text-surface-300 rounded-lg text-xs transition-colors">
          Add
        </button>
      </div>
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 px-2 py-0.5 bg-surface-700 rounded text-xs font-mono text-surface-200">
              {t}
              <button type="button" onClick={() => onRemove(t)} className="text-surface-400 hover:text-red-400">
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Port Input
// ---------------------------------------------------------------------------

function PortInput({
  ports,
  onAdd,
  onRemove,
}: {
  ports: PortRule[];
  onAdd: (port: PortRule) => void;
  onRemove: (index: number) => void;
}) {
  const [port, setPort] = useState('');
  const [protocol, setProtocol] = useState<'TCP' | 'UDP'>('TCP');

  const add = () => {
    const p = port.trim();
    if (!p || isNaN(Number(p)) || Number(p) < 1 || Number(p) > 65535) return;
    onAdd({ port: p, protocol });
    setPort('');
  };

  return (
    <div>
      <div className="flex gap-2 mb-2">
        <input
          type="text"
          value={port}
          onChange={(e) => setPort(e.target.value.replace(/\D/g, ''))}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }}
          placeholder="8080"
          className="w-24 px-3 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-sm"
        />
        <div className="flex rounded-lg border border-surface-700 overflow-hidden">
          {(['TCP', 'UDP'] as const).map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setProtocol(p)}
              className={clsx(
                'px-3 py-1.5 text-xs font-mono transition-colors',
                protocol === p ? 'bg-primary-500/20 text-primary-400' : 'bg-surface-900 text-surface-400 hover:bg-surface-800',
              )}
            >
              {p}
            </button>
          ))}
        </div>
        <button type="button" onClick={add} disabled={!port.trim()} className="px-3 py-1.5 bg-surface-700 hover:bg-surface-600 disabled:opacity-50 text-surface-300 rounded-lg text-xs transition-colors">
          Add
        </button>
      </div>
      {ports.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {ports.map((p, i) => (
            <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 bg-surface-700 rounded text-xs font-mono text-surface-200">
              {p.port}/{p.protocol}
              <button type="button" onClick={() => onRemove(i)} className="text-surface-400 hover:text-red-400">
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section Toggle
// ---------------------------------------------------------------------------

function Section({
  title,
  enabled,
  onToggle,
  children,
}: {
  title: string;
  enabled: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={clsx('border rounded-lg transition-colors', enabled ? 'border-surface-600' : 'border-surface-700/50')}>
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center justify-between p-3 text-left"
      >
        <span className={clsx('text-sm font-medium', enabled ? 'text-surface-100' : 'text-surface-500')}>{title}</span>
        <div className={clsx(
          'relative inline-flex h-5 w-9 items-center rounded-full transition-colors',
          enabled ? 'bg-primary-500' : 'bg-surface-600',
        )}>
          <span className={clsx('inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform', enabled ? 'translate-x-4.5' : 'translate-x-0.5')}
            style={{ transform: enabled ? 'translateX(1rem)' : 'translateX(0.125rem)' }}
          />
        </div>
      </button>
      {enabled && <div className="px-3 pb-3 space-y-3">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

interface CiliumRuleBuilderProps {
  value: CiliumRuleState;
  onChange: (state: CiliumRuleState) => void;
  /** Hide endpoint selector (for cluster-wide policies) */
  hideEndpointSelector?: boolean;
}

export function CiliumRuleBuilder({ value: state, onChange, hideEndpointSelector }: CiliumRuleBuilderProps) {
  const set = <K extends keyof CiliumRuleState>(key: K, val: CiliumRuleState[K]) =>
    onChange({ ...state, [key]: val });

  // Label input for endpoint selector
  const [labelKey, setLabelKey] = useState('');
  const [labelValue, setLabelValue] = useState('');

  const addLabel = () => {
    if (!labelKey.trim()) return;
    set('endpointLabels', { ...state.endpointLabels, [labelKey.trim()]: labelValue.trim() });
    setLabelKey('');
    setLabelValue('');
  };

  return (
    <div className="space-y-4">
      {/* Endpoint Selector */}
      {!hideEndpointSelector && (
        <div className="space-y-2">
          <label className="block text-xs font-medium text-surface-300">
            Apply to pods with labels
            <span className="text-surface-500 font-normal ml-1">(empty = all pods)</span>
          </label>
          <div className="flex gap-2 mb-1">
            <input type="text" value={labelKey} onChange={(e) => setLabelKey(e.target.value)} placeholder="key" className="w-32 px-2 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-xs" />
            <span className="text-surface-500 self-center">=</span>
            <input type="text" value={labelValue} onChange={(e) => setLabelValue(e.target.value)} placeholder="value" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addLabel(); } }} className="w-32 px-2 py-1.5 bg-surface-900 border border-surface-700 rounded-lg text-surface-100 placeholder-surface-500 focus:outline-none focus:border-primary-500 font-mono text-xs" />
            <button type="button" onClick={addLabel} disabled={!labelKey.trim()} className="px-2 py-1.5 bg-surface-700 hover:bg-surface-600 disabled:opacity-50 text-surface-300 rounded-lg text-xs">Add</button>
          </div>
          {Object.keys(state.endpointLabels).length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(state.endpointLabels).map(([k, v]) => (
                <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary-500/10 border border-primary-500/30 rounded text-xs font-mono text-primary-300">
                  {k}={v}
                  <button type="button" onClick={() => { const next = { ...state.endpointLabels }; delete next[k]; set('endpointLabels', next); }} className="text-primary-400 hover:text-red-400">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Egress Rules */}
      <Section title="Egress Rules (outbound)" enabled={state.egressEnabled} onToggle={() => set('egressEnabled', !state.egressEnabled)}>
        {/* Deny all toggle */}
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={state.egressDenyAll} onChange={(e) => set('egressDenyAll', e.target.checked)} className="checkbox" />
          <span className="text-xs text-surface-200">Block all egress</span>
          <span className="text-xs text-surface-500">(deny 0.0.0.0/0)</span>
        </label>

        {/* Allow DNS */}
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={state.egressAllowDns} onChange={(e) => set('egressAllowDns', e.target.checked)} className="checkbox" />
          <span className="text-xs text-surface-200">Allow DNS</span>
          <span className="text-xs text-surface-500">(port 53 to kube-system)</span>
        </label>

        {!state.egressDenyAll && (
          <>
            {/* FQDNs */}
            <div>
              <label className="block text-xs text-surface-300 mb-1">Allow to domains (FQDN)</label>
              <TagInput
                tags={state.egressAllowFqdns}
                onAdd={(v) => set('egressAllowFqdns', [...state.egressAllowFqdns, v])}
                onRemove={(v) => set('egressAllowFqdns', state.egressAllowFqdns.filter((x) => x !== v))}
                placeholder="google.com, *.github.com"
              />
            </div>

            {/* CIDRs */}
            <div>
              <label className="block text-xs text-surface-300 mb-1">Allow to CIDRs</label>
              <TagInput
                tags={state.egressAllowCidrs}
                onAdd={(v) => set('egressAllowCidrs', [...state.egressAllowCidrs, v])}
                onRemove={(v) => set('egressAllowCidrs', state.egressAllowCidrs.filter((x) => x !== v))}
                placeholder="10.0.0.0/8, 192.168.0.0/16"
              />
            </div>

            {/* Ports */}
            <div>
              <label className="block text-xs text-surface-300 mb-1">Allow egress ports</label>
              <PortInput
                ports={state.egressAllowPorts}
                onAdd={(p) => set('egressAllowPorts', [...state.egressAllowPorts, p])}
                onRemove={(i) => set('egressAllowPorts', state.egressAllowPorts.filter((_, idx) => idx !== i))}
              />
            </div>

            {/* HTTP L7 */}
            <Section title="HTTP Filtering (L7)" enabled={state.egressHttpEnabled} onToggle={() => set('egressHttpEnabled', !state.egressHttpEnabled)}>
              <div className="flex items-start gap-2 p-2 bg-blue-900/10 border border-blue-800/20 rounded-lg mb-2">
                <Info className="w-3.5 h-3.5 text-blue-400 shrink-0 mt-0.5" />
                <p className="text-xs text-blue-300/80">L7 filtering uses Cilium's Envoy proxy. Only applies to port 80 traffic.</p>
              </div>
              <div>
                <label className="block text-xs text-surface-300 mb-2">HTTP Methods</label>
                <div className="flex gap-1.5 flex-wrap">
                  {['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'].map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => set('egressHttpMethods', state.egressHttpMethods.includes(m) ? state.egressHttpMethods.filter((x) => x !== m) : [...state.egressHttpMethods, m])}
                      className={clsx(
                        'px-2.5 py-1 text-xs rounded border font-mono transition-colors',
                        state.egressHttpMethods.includes(m)
                          ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                          : 'border-surface-700 text-surface-400 hover:border-surface-600',
                      )}
                    >
                      {m}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="block text-xs text-surface-300 mb-1">Path patterns (regex)</label>
                <TagInput
                  tags={state.egressHttpPaths}
                  onAdd={(v) => set('egressHttpPaths', [...state.egressHttpPaths, v])}
                  onRemove={(v) => set('egressHttpPaths', state.egressHttpPaths.filter((x) => x !== v))}
                  placeholder="/api/v1/.*, /health"
                />
              </div>
            </Section>
          </>
        )}
      </Section>

      {/* Ingress Rules */}
      <Section title="Ingress Rules (inbound)" enabled={state.ingressEnabled} onToggle={() => set('ingressEnabled', !state.ingressEnabled)}>
        <div>
          <label className="block text-xs text-surface-300 mb-1">Allow from namespaces</label>
          <TagInput
            tags={state.ingressAllowNamespaces}
            onAdd={(v) => set('ingressAllowNamespaces', [...state.ingressAllowNamespaces, v])}
            onRemove={(v) => set('ingressAllowNamespaces', state.ingressAllowNamespaces.filter((x) => x !== v))}
            placeholder="monitoring, ingress-nginx"
          />
        </div>
        <div>
          <label className="block text-xs text-surface-300 mb-1">Allow from CIDRs</label>
          <TagInput
            tags={state.ingressAllowCidrs}
            onAdd={(v) => set('ingressAllowCidrs', [...state.ingressAllowCidrs, v])}
            onRemove={(v) => set('ingressAllowCidrs', state.ingressAllowCidrs.filter((x) => x !== v))}
            placeholder="192.168.196.0/24"
          />
        </div>
        <div>
          <label className="block text-xs text-surface-300 mb-1">Allow ingress ports</label>
          <PortInput
            ports={state.ingressAllowPorts}
            onAdd={(p) => set('ingressAllowPorts', [...state.ingressAllowPorts, p])}
            onRemove={(i) => set('ingressAllowPorts', state.ingressAllowPorts.filter((_, idx) => idx !== i))}
          />
        </div>
      </Section>

      {/* Live YAML Preview */}
      <div>
        <label className="block text-xs font-medium text-surface-400 mb-1">Generated spec preview</label>
        <pre className="bg-surface-900 border border-surface-700 rounded-lg p-3 text-xs text-surface-300 font-mono overflow-auto max-h-48">
          {JSON.stringify(buildCiliumSpec(state), null, 2)}
        </pre>
      </div>
    </div>
  );
}

export { DEFAULT_STATE as DEFAULT_RULE_STATE };
export type { PortRule };

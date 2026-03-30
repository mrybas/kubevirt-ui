import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  X,
  Globe,
  Layers,
  Building2,
  ChevronRight,
  ChevronLeft,
  AlertCircle,
  CheckCircle,
  Server,
  Info,
  AlertTriangle,
} from 'lucide-react';
import {
  useCreateProviderNetwork,
  useCreateVlan,
  useCreateSubnet,
} from '../../hooks/useNetwork';
import { useCreateVpc } from '../../hooks/useVpcs';
import { listNodes } from '../../api/cluster';
import { useNamespaces } from '../../hooks/useNamespaces';
import type { ProviderNetwork, Vlan } from '../../types/network';
import { CustomSelect } from '../common/CustomSelect';
import { WizardStepIndicator } from '../common/WizardStepIndicator';

interface CreateNetworkWizardProps {
  onClose: () => void;
  existingProvider?: ProviderNetwork; // Skip provider creation step
  existingVlan?: Vlan;               // Skip provider + VLAN creation steps
}

type NetworkType = 'external' | 'overlay' | 'vpc';
type InterfaceMode = 'dedicated' | 'single-nic';

interface WizardState {
  type: NetworkType;
  interfaceMode: InterfaceMode;
  // Provider Network
  providerName: string;
  baseInterface: string; // Physical interface (e.g., eno1)
  // VLAN (required - we only support VLAN-based networks for safety)
  vlanName: string;
  vlanId: number; // Actual VLAN ID for the sub-interface (e.g., 111)
  // Subnet
  subnetName: string;
  cidrBlock: string;
  gateway: string;
  dhcpPoolStart: string;
  dhcpPoolEnd: string;
  enableDhcp: boolean;
  disableGatewayCheck: boolean; // Disable gateway ARP check
  // Subnet purpose: "vm" (for VM attachment via Multus) or "infrastructure" (for VPC NAT gateway)
  purpose: 'vm' | 'infrastructure';
  // Namespace for this subnet (one subnet = one namespace, not needed for infrastructure)
  namespace: string;
  // VPC
  vpcName: string;
  vpcSubnetCidr: string;
  vpcEnableNat: boolean;
  vpcEnablePeering: boolean;
}

const initialState: WizardState = {
  type: 'external',
  interfaceMode: 'dedicated',
  providerName: '',
  baseInterface: '',
  vlanName: '',
  vlanId: 100, // Default VLAN ID
  subnetName: '',
  cidrBlock: '',
  gateway: '',
  dhcpPoolStart: '',
  dhcpPoolEnd: '',
  enableDhcp: true,
  disableGatewayCheck: false,
  purpose: 'vm',
  namespace: '',
  vpcName: '',
  vpcSubnetCidr: '10.100.0.0/24',
  vpcEnableNat: true,
  vpcEnablePeering: true,
};

// Helper to convert IP to number for comparison
function ipToNumber(ip: string): number {
  const parts = ip.split('.').map(Number);
  return (parts[0]! << 24) + (parts[1]! << 16) + (parts[2]! << 8) + parts[3]!;
}

// Helper to convert number to IP
function numberToIp(num: number): string {
  return [
    (num >>> 24) & 255,
    (num >>> 16) & 255,
    (num >>> 8) & 255,
    num & 255,
  ].join('.');
}

// Helper to check if IP is in CIDR range
function isIpInCidr(ip: string, cidr: string): boolean {
  if (!cidr.includes('/')) return false;
  const [network, bits] = cidr.split('/') as [string, string];
  const mask = ~((1 << (32 - parseInt(bits))) - 1) >>> 0;
  const ipNum = ipToNumber(ip);
  const networkNum = ipToNumber(network!);
  return (ipNum & mask) === (networkNum & mask);
}

// Helper to get network range from CIDR
function getCidrRange(cidr: string): { start: number; end: number } | null {
  if (!cidr.includes('/')) return null;
  const [network, bits] = cidr.split('/') as [string, string];
  const mask = ~((1 << (32 - parseInt(bits))) - 1) >>> 0;
  const networkNum = ipToNumber(network!) & mask;
  const broadcast = networkNum | (~mask >>> 0);
  return { start: networkNum + 1, end: broadcast - 1 }; // Exclude network and broadcast
}

// Calculate excludeIps from DHCP pool range
function calculateExcludeIps(
  cidr: string,
  gateway: string,
  dhcpPoolStart: string,
  dhcpPoolEnd: string
): string[] {
  const excludeIps: string[] = [];
  const range = getCidrRange(cidr);
  if (!range) return excludeIps;

  const poolStart = dhcpPoolStart ? ipToNumber(dhcpPoolStart) : range.start;
  const poolEnd = dhcpPoolEnd ? ipToNumber(dhcpPoolEnd) : range.end;

  // Exclude IPs before DHCP pool
  if (poolStart > range.start) {
    const excludeStart = numberToIp(range.start);
    const excludeEnd = numberToIp(poolStart - 1);
    if (excludeStart === excludeEnd) {
      excludeIps.push(excludeStart);
    } else {
      excludeIps.push(`${excludeStart}..${excludeEnd}`);
    }
  }

  // Exclude IPs after DHCP pool
  if (poolEnd < range.end) {
    const excludeStart = numberToIp(poolEnd + 1);
    const excludeEnd = numberToIp(range.end);
    if (excludeStart === excludeEnd) {
      excludeIps.push(excludeStart);
    } else {
      excludeIps.push(`${excludeStart}..${excludeEnd}`);
    }
  }

  // Always exclude gateway if it's within the DHCP pool
  if (gateway && isIpInCidr(gateway, cidr)) {
    const gwNum = ipToNumber(gateway);
    if (gwNum >= poolStart && gwNum <= poolEnd) {
      excludeIps.push(gateway);
    }
  }

  return excludeIps;
}

// Step IDs for dynamic step list
type StepId = 'type' | 'provider' | 'vlan' | 'subnet' | 'review' | 'vpc-config' | 'vpc-peering' | 'vpc-review';

const STEP_LABELS: Record<StepId, string> = {
  type: 'Type',
  provider: 'Provider',
  vlan: 'VLAN',
  subnet: 'Subnet',
  review: 'Review',
  'vpc-config': 'VPC',
  'vpc-peering': 'Peering',
  'vpc-review': 'Review',
};

export function CreateNetworkWizard({ onClose, existingProvider, existingVlan }: CreateNetworkWizardProps) {
  const [state, setState] = useState<WizardState>(() => ({
    ...initialState,
    // Pre-fill from existing resources
    providerName: existingProvider?.name || '',
    baseInterface: existingProvider?.default_interface || '',
    vlanName: existingVlan?.name || '',
    vlanId: existingVlan?.id || 100,
  }));
  const [stepIndex, setStepIndex] = useState(0);

  // Compute which steps to show based on existing resources and network type
  const steps = useMemo<StepId[]>(() => {
    if (state.type === 'vpc') {
      return ['type', 'vpc-config', 'vpc-peering', 'vpc-review'];
    }
    if (existingVlan && existingProvider) {
      return ['subnet', 'review'];
    }
    if (existingProvider) {
      return ['vlan', 'subnet', 'review'];
    }
    return ['type', 'provider', 'vlan', 'subnet', 'review'];
  }, [existingProvider, existingVlan, state.type]);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentStep = steps[stepIndex];

  const createProviderNetwork = useCreateProviderNetwork();
  const createVlan = useCreateVlan();
  const createSubnet = useCreateSubnet();
  const createVpcMutation = useCreateVpc();

  // Fetch cluster nodes to validate CIDR doesn't overlap with node network
  const { data: nodesData } = useQuery({
    queryKey: ['nodes'],
    queryFn: listNodes,
  });

  // Fetch available namespaces for the subnet
  const { data: namespacesData } = useNamespaces();
  
  // Filter to only show "user" namespaces (exclude system ones)
  const userNamespaces = useMemo(() => {
    if (!namespacesData?.items) return [];
    const systemPrefixes = ['kube-', 'flux-', 'cilium-', 'ingress-', 'metallb-', 'piraeus-', 'victoria-', 'monitoring', 'grafana-', 'kubevirt-ui-'];
    const systemNamespaces = ['default', 'cdi', 'kubevirt', 'cluster-crds'];
    return namespacesData.items
      .filter(ns => !systemPrefixes.some(p => ns.name.startsWith(p)))
      .filter(ns => !systemNamespaces.includes(ns.name))
      .map(ns => ns.name);
  }, [namespacesData]);

  // Check if any node IPs are in the current CIDR range (dangerous!)
  const nodesInCidr = useMemo(() => {
    if (!state.cidrBlock || !nodesData?.items) return [];
    return nodesData.items.filter(
      (node) => node.internal_ip && isIpInCidr(node.internal_ip, state.cidrBlock)
    );
  }, [nodesData, state.cidrBlock]);

  // Calculate what will be excluded based on DHCP pool
  const calculatedExcludeIps = useMemo(() => {
    if (!state.cidrBlock) return [];
    return calculateExcludeIps(
      state.cidrBlock,
      state.gateway,
      state.dhcpPoolStart,
      state.dhcpPoolEnd
    );
  }, [state.cidrBlock, state.gateway, state.dhcpPoolStart, state.dhcpPoolEnd]);

  const networkTypes = [
    {
      id: 'external' as const,
      title: 'External Network',
      description: 'Connect VMs directly to your physical network',
      icon: Globe,
      color: 'emerald',
    },
    {
      id: 'overlay' as const,
      title: 'Overlay Network',
      description: 'Private isolated network for a namespace',
      icon: Layers,
      color: 'primary',
      disabled: true,
      comingSoon: true,
    },
    {
      id: 'vpc' as const,
      title: 'VPC Network',
      description: 'Isolated virtual private cloud with optional NAT gateway',
      icon: Building2,
      color: 'amber',
    },
  ];

  const commonInterfaces = ['eno1', 'eno2', 'eth0', 'eth1', 'enp0s3', 'enp3s0f0', 'bond0'];

  const handleNext = () => {
    setError(null);
    // Auto-generate VLAN name when moving from Provider step if not set
    if (currentStep === 'provider' && !state.vlanName) {
      setState((s) => ({ ...s, vlanName: `${s.providerName}-vlan${s.vlanId}` }));
    }
    setStepIndex((i) => Math.min(i + 1, steps.length - 1));
  };

  const handleBack = () => {
    setError(null);
    setStepIndex((i) => Math.max(i - 1, 0));
  };

  const validateStep = (): boolean => {
    switch (currentStep) {
      case 'provider':
        if (!state.providerName.trim()) {
          setError('Provider network name is required');
          return false;
        }
        if (state.providerName.length > 12) {
          setError('Provider network name must be 12 characters or less (Kube-OVN limit)');
          return false;
        }
        if (/^[0-9]/.test(state.providerName)) {
          setError('Name must not start with a digit (Kube-OVN requirement)');
          return false;
        }
        if (!/^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(state.providerName)) {
          setError('Name must start with a letter, contain only lowercase alphanumeric and hyphens');
          return false;
        }
        if (!state.baseInterface.trim()) {
          setError('Network interface is required');
          return false;
        }
        break;
      case 'vlan':
        if (!state.vlanName.trim()) {
          setError('VLAN name is required');
          return false;
        }
        if (/^[0-9]/.test(state.vlanName)) {
          setError('VLAN name must not start with a digit (Kube-OVN requirement)');
          return false;
        }
        if (!/^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(state.vlanName)) {
          setError('VLAN name must start with a letter, contain only lowercase alphanumeric and hyphens');
          return false;
        }
        if (state.vlanId < 1 || state.vlanId > 4094) {
          setError('VLAN ID must be between 1 and 4094');
          return false;
        }
        break;
      case 'subnet':
        if (!state.subnetName.trim()) {
          setError('Subnet name is required');
          return false;
        }
        if (/^[0-9]/.test(state.subnetName)) {
          setError('Subnet name must not start with a digit (Kube-OVN requirement)');
          return false;
        }
        if (!/^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(state.subnetName)) {
          setError('Subnet name must start with a letter, contain only lowercase alphanumeric and hyphens');
          return false;
        }
        if (!state.cidrBlock.trim()) {
          setError('CIDR block is required');
          return false;
        }
        // Check if CIDR overlaps with cluster node IPs
        if (nodesInCidr.length > 0) {
          setError(`This CIDR overlaps with cluster node IPs (${nodesInCidr.map((n: any) => n.internal_ip).join(', ')}). Use a different network or VLAN.`);
          return false;
        }
        if (!state.gateway.trim()) {
          setError('Gateway is required');
          return false;
        }
        if (state.purpose === 'vm' && !state.namespace) {
          setError('Target namespace is required for VM networks');
          return false;
        }
        break;
      case 'vpc-config':
        if (!state.vpcName.trim()) {
          setError('VPC name is required');
          return false;
        }
        if (!/^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(state.vpcName)) {
          setError('VPC name must start with a letter, contain only lowercase alphanumeric and hyphens');
          return false;
        }
        if (state.vpcSubnetCidr && !state.vpcSubnetCidr.includes('/')) {
          setError('Subnet CIDR must be in CIDR notation (e.g. 10.100.0.0/24)');
          return false;
        }
        break;
    }
    return true;
  };

  const handleCreateVpc = async () => {
    if (!validateStep()) return;

    setIsCreating(true);
    setError(null);

    try {
      await createVpcMutation.mutateAsync({
        name: state.vpcName,
        subnet_cidr: state.vpcSubnetCidr || undefined,
        enable_nat_gateway: state.vpcEnableNat,
      });
      // TODO: if peering enabled, create peering with ovn-cluster (default VPC)
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create VPC');
    } finally {
      setIsCreating(false);
    }
  };

  const handleCreate = async () => {
    if (!validateStep()) return;

    setIsCreating(true);
    setError(null);

    try {
      // 1. Create Provider Network (skip if already exists)
      if (!existingProvider) {
        const isDedicated = state.interfaceMode === 'dedicated';
        // Dedicated: bare interface (eth1); Single-NIC: sub-interface (eno1.111)
        const defaultInterface = isDedicated
          ? state.baseInterface
          : `${state.baseInterface}.${state.vlanId}`;
        await createProviderNetwork.mutateAsync({
          name: state.providerName,
          default_interface: defaultInterface,
          auto_create_vlan_subinterfaces: !isDedicated, // Only for single-NIC
          exchange_link_name: false,
        });
      }

      // 2. Create VLAN (skip if already exists)
      if (!existingVlan) {
        // Dedicated: real VLAN ID (OVN tags); Single-NIC: id=0 (sub-interface tags)
        const vlanResourceId = state.interfaceMode === 'dedicated' ? state.vlanId : 0;
        await createVlan.mutateAsync({
          name: state.vlanName,
          id: vlanResourceId,
          provider: existingProvider?.name || state.providerName,
        });
      }

      // 3. Create Subnet (+ NAD for VM subnets)
      const vlanName = existingVlan?.name || state.vlanName;
      await createSubnet.mutateAsync({
        name: state.subnetName,
        cidr_block: state.cidrBlock,
        gateway: state.gateway,
        exclude_ips: calculatedExcludeIps,
        vlan: vlanName,
        ...(state.purpose === 'vm' && state.namespace ? { namespace: state.namespace } : {}),
        purpose: state.purpose,
        enable_dhcp: state.enableDhcp,
        disable_gateway_check: state.disableGatewayCheck,
      });

      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create network');
    } finally {
      setIsCreating(false);
    }
  };

  const renderStep = () => {
    switch (currentStep) {
      case 'type': // Select network type
        return (
          <div className="space-y-4">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                Select Network Type
              </h3>
              <p className="text-surface-400 text-sm">
                Choose the type of network you want to create
              </p>
            </div>

            <div className="grid gap-4">
              {networkTypes.map((type) => {
                const Icon = type.icon;
                const isSelected = state.type === type.id;
                const colorClasses: Record<string, string> = {
                  emerald: 'bg-emerald-500/10 text-emerald-400 border-emerald-500',
                  primary: 'bg-primary-500/10 text-primary-400 border-primary-500',
                  amber: 'bg-amber-500/10 text-amber-400 border-amber-500',
                };

                return (
                  <button
                    key={type.id}
                    onClick={() => !type.disabled && setState((s) => ({ ...s, type: type.id }))}
                    disabled={type.disabled}
                    className={`p-4 rounded-lg border-2 text-left transition-all ${
                      type.disabled
                        ? 'border-surface-700 opacity-50 cursor-not-allowed'
                        : isSelected
                        ? `${colorClasses[type.color]!} border-opacity-100`
                        : 'border-surface-700 hover:border-surface-600'
                    }`}
                  >
                    <div className="flex items-start gap-4">
                      <div
                        className={`rounded-lg p-3 ${
                          type.disabled
                            ? 'bg-surface-700 text-surface-500'
                            : colorClasses[type.color]!.split(' ').slice(0, 2).join(' ')
                        }`}
                      >
                        <Icon className="h-6 w-6" />
                      </div>
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <h4 className="font-medium text-surface-100">{type.title}</h4>
                          {type.comingSoon && (
                            <span className="text-xs px-2 py-0.5 rounded-full bg-surface-700 text-surface-400">
                              Coming Soon
                            </span>
                          )}
                        </div>
                        <p className="text-sm text-surface-400 mt-1">{type.description}</p>
                      </div>
                      {isSelected && !type.disabled && (
                        <CheckCircle className="h-5 w-5 text-emerald-400" />
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        );

      case 'provider': // Provider Network config
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                Provider Network Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Connect to your physical network infrastructure
              </p>
            </div>

            {/* Interface Mode Toggle */}
            <div className="space-y-3">
              <label className="block text-sm font-medium text-surface-200">Interface Mode</label>
              <div className="grid grid-cols-2 gap-3">
                <button
                  onClick={() => setState((s) => ({ ...s, interfaceMode: 'dedicated' as InterfaceMode }))}
                  className={`p-3 rounded-lg border text-left transition-all ${
                    state.interfaceMode === 'dedicated'
                      ? 'border-primary-500 bg-primary-500/10'
                      : 'border-surface-700 bg-surface-800/50 hover:border-surface-600'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <div className={`w-3 h-3 rounded-full border-2 ${
                      state.interfaceMode === 'dedicated' ? 'border-primary-500 bg-primary-500' : 'border-surface-500'
                    }`} />
                    <span className="text-sm font-medium text-surface-100">Dedicated NIC</span>
                    <span className="px-1.5 py-0.5 text-[10px] rounded bg-emerald-500/20 text-emerald-400">recommended</span>
                  </div>
                  <p className="text-xs text-surface-400 ml-5">
                    One interface carries multiple VLANs. Best for production with a separate NIC for VMs.
                  </p>
                </button>
                <button
                  onClick={() => setState((s) => ({ ...s, interfaceMode: 'single-nic' as InterfaceMode }))}
                  className={`p-3 rounded-lg border text-left transition-all ${
                    state.interfaceMode === 'single-nic'
                      ? 'border-primary-500 bg-primary-500/10'
                      : 'border-surface-700 bg-surface-800/50 hover:border-surface-600'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <div className={`w-3 h-3 rounded-full border-2 ${
                      state.interfaceMode === 'single-nic' ? 'border-primary-500 bg-primary-500' : 'border-surface-500'
                    }`} />
                    <span className="text-sm font-medium text-surface-100">Single NIC</span>
                  </div>
                  <p className="text-xs text-surface-400 ml-5">
                    Each VLAN gets its own sub-interface. Safe for homelab / testing with one NIC.
                  </p>
                </button>
              </div>

              {state.interfaceMode === 'dedicated' && (
                <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3">
                  <div className="flex items-start gap-2">
                    <AlertTriangle className="h-4 w-4 text-amber-400 mt-0.5 shrink-0" />
                    <p className="text-xs text-amber-300">
                      This NIC will be bridged into OVS and <strong>lose its IP connectivity</strong>. 
                      Do not use the management interface.
                    </p>
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-4">
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-sm font-medium text-surface-200">
                    Provider Network Name
                  </label>
                  <span className={`text-xs ${state.providerName.length > 12 ? 'text-red-400' : 'text-surface-500'}`}>
                    {state.providerName.length}/12
                  </span>
                </div>
                <input
                  type="text"
                  value={state.providerName}
                  onChange={(e) => setState((s) => ({ ...s, providerName: e.target.value.toLowerCase() }))}
                  className={`input w-full ${state.providerName.length > 12 ? 'border-red-500 focus:ring-red-500' : ''}`}
                  placeholder="external"
                  maxLength={12}
                />
                <p className="text-xs text-surface-500 mt-1">
                  Lowercase letters, numbers, and hyphens only
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  {state.interfaceMode === 'dedicated' ? 'Dedicated Interface' : 'Physical Interface'}
                </label>
                <input
                  type="text"
                  value={state.baseInterface}
                  onChange={(e) => setState((s) => ({ ...s, baseInterface: e.target.value }))}
                  className="input w-full"
                  placeholder={state.interfaceMode === 'dedicated' ? 'eth1' : 'eno1'}
                />
                <p className="text-xs text-surface-500 mt-1">
                  {state.interfaceMode === 'dedicated' ? (
                    <>Dedicated NIC for VM traffic (must be the same on all nodes). This interface will be bridged into OVS.</>
                  ) : (
                    <>
                      Base interface name (must be the same on all nodes).
                      {state.baseInterface && state.vlanId > 0 && (
                        <> Kube-OVN will create sub-interface <span className="text-primary-400 font-mono">{state.baseInterface}.{state.vlanId}</span></>
                      )}
                    </>
                  )}
                </p>
              </div>

              {/* Common interfaces hints */}
              <div className="bg-surface-800/50 rounded-lg p-4">
                <div className="flex items-start gap-2">
                  <Info className="h-4 w-4 text-primary-400 mt-0.5" />
                  <div>
                    <p className="text-sm text-surface-300 mb-2">Common interface names:</p>
                    <div className="flex flex-wrap gap-2">
                      {commonInterfaces.map((iface) => (
                        <button
                          key={iface}
                          onClick={() => setState((s) => ({ ...s, baseInterface: iface }))}
                          className="px-2 py-1 text-xs bg-surface-700 hover:bg-surface-600 rounded font-mono text-surface-300"
                        >
                          {iface}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

            </div>
          </div>
        );

      case 'vlan': // VLAN config (required)
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                VLAN Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Configure VLAN for VM network isolation
              </p>
            </div>

            {/* Mode-specific info */}
            <div className={`border rounded-lg p-4 ${
              state.interfaceMode === 'dedicated'
                ? 'bg-primary-500/10 border-primary-500/30'
                : 'bg-amber-500/10 border-amber-500/30'
            }`}>
              <div className="flex items-start gap-3">
                <Info className="h-5 w-5 text-primary-400 mt-0.5" />
                <div>
                  {state.interfaceMode === 'dedicated' ? (
                    <>
                      <h4 className="text-sm font-medium text-primary-300">Dedicated NIC — OVN VLAN tagging</h4>
                      <p className="text-xs text-primary-400/80 mt-1">
                        OVN will tag traffic with the VLAN ID on the OVS bridge. You can add multiple VLANs to this provider later.
                      </p>
                    </>
                  ) : (
                    <>
                      <h4 className="text-sm font-medium text-amber-300">Single NIC — sub-interface tagging</h4>
                      <p className="text-xs text-amber-400/80 mt-1">
                        Kube-OVN will create sub-interface <span className="font-mono text-amber-300">{state.baseInterface}.{state.vlanId}</span>.
                        The VLAN resource uses id=0 because the sub-interface already tags at kernel level.
                        Each VLAN requires its own Provider Network.
                      </p>
                    </>
                  )}
                </div>
              </div>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  VLAN Name
                </label>
                <input
                  type="text"
                  value={state.vlanName}
                  onChange={(e) => setState((s) => ({ ...s, vlanName: e.target.value }))}
                  className="input w-full"
                  placeholder={`${state.providerName || 'external'}-vlan${state.vlanId}`}
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  VLAN ID
                </label>
                <input
                  type="number"
                  min={1}
                  max={4094}
                  value={state.vlanId}
                  onChange={(e) =>
                    setState((s) => ({ ...s, vlanId: parseInt(e.target.value) || 1 }))
                  }
                  className="input w-full"
                  placeholder="100"
                />
                <p className="text-xs text-surface-500 mt-1">
                  Must be between 1 and 4094. Ensure this VLAN is configured on your physical switch.
                  {state.interfaceMode === 'single-nic' && (
                    <> This ID is used for the sub-interface name only — OVS VLAN id will be set to 0.</>
                  )}
                </p>
              </div>
            </div>
          </div>
        );

      case 'subnet': // Subnet config
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                Subnet Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Define the IP address range for your VMs
              </p>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  Subnet Name
                </label>
                <input
                  type="text"
                  value={state.subnetName}
                  onChange={(e) => setState((s) => ({ ...s, subnetName: e.target.value }))}
                  className="input w-full"
                  placeholder="external-subnet"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-surface-200 mb-2">
                    CIDR Block
                  </label>
                  <input
                    type="text"
                    value={state.cidrBlock}
                    onChange={(e) => setState((s) => ({ ...s, cidrBlock: e.target.value }))}
                    className="input w-full font-mono"
                    placeholder="192.168.1.0/24"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-surface-200 mb-2">
                    Gateway
                  </label>
                  <input
                    type="text"
                    value={state.gateway}
                    onChange={(e) => setState((s) => ({ ...s, gateway: e.target.value }))}
                    className="input w-full font-mono"
                    placeholder="192.168.1.1"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  DHCP Pool Range
                </label>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={state.dhcpPoolStart}
                    onChange={(e) => setState((s) => ({ ...s, dhcpPoolStart: e.target.value }))}
                    className="input flex-1 font-mono"
                    placeholder={state.cidrBlock ? numberToIp(getCidrRange(state.cidrBlock)?.start || 0) : '192.168.1.100'}
                  />
                  <span className="text-surface-400">to</span>
                  <input
                    type="text"
                    value={state.dhcpPoolEnd}
                    onChange={(e) => setState((s) => ({ ...s, dhcpPoolEnd: e.target.value }))}
                    className="input flex-1 font-mono"
                    placeholder={state.cidrBlock ? numberToIp(getCidrRange(state.cidrBlock)?.end || 0) : '192.168.1.200'}
                  />
                </div>
                <p className="text-xs text-surface-500 mt-1">
                  VMs will receive IPs only from this range. Leave empty to use full subnet.
                </p>
              </div>

              {/* Error: CIDR overlaps with cluster nodes */}
              {nodesInCidr.length > 0 && (
                <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4">
                  <div className="flex items-start gap-3">
                    <AlertTriangle className="h-5 w-5 text-red-400 mt-0.5" />
                    <div className="flex-1">
                      <h4 className="text-sm font-medium text-red-300">
                        ⚠️ CIDR Conflict Detected
                      </h4>
                      <p className="text-xs text-red-400/80 mt-1">
                        This subnet overlaps with cluster node IPs. Use a different CIDR or a VLAN with separate IP space.
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {nodesInCidr.map((node) => (
                          <span
                            key={node.name}
                            className="inline-flex items-center gap-1.5 px-2 py-1 bg-red-500/20 rounded text-xs font-mono text-red-200"
                          >
                            <span className="w-2 h-2 bg-red-400 rounded-full"></span>
                            {node.name}: {node.internal_ip}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              )}

              <div className="bg-surface-800/50 rounded-lg p-4 space-y-3">
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={state.enableDhcp}
                    onChange={(e) => setState((s) => ({ ...s, enableDhcp: e.target.checked }))}
                    className="checkbox"
                  />
                  <div>
                    <span className="font-medium text-surface-100">Enable DHCP</span>
                    <p className="text-xs text-surface-400">
                      Automatically assign IP addresses to VMs
                    </p>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={state.disableGatewayCheck}
                    onChange={(e) => setState((s) => ({ ...s, disableGatewayCheck: e.target.checked }))}
                    className="checkbox"
                  />
                  <div>
                    <span className="font-medium text-surface-100">Disable Gateway Check</span>
                    <p className="text-xs text-surface-400">
                      Skip gateway ARP verification. Enable when VLAN sub-interface has no IP on nodes.
                    </p>
                  </div>
                </label>
              </div>

              {/* Purpose Selection */}
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  Subnet Purpose
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <button
                    type="button"
                    onClick={() => setState((s) => ({ ...s, purpose: 'vm' }))}
                    className={`p-3 rounded-lg border text-left transition-all ${
                      state.purpose === 'vm'
                        ? 'border-primary-500 bg-primary-500/10'
                        : 'border-surface-600 bg-surface-800/50 hover:border-surface-500'
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      {state.purpose === 'vm' && <CheckCircle className="h-4 w-4 text-primary-400" />}
                      <span className="font-medium text-surface-100">VM Network</span>
                    </div>
                    <p className="text-xs text-surface-400">
                      For connecting virtual machines. Creates NAD in a namespace.
                    </p>
                  </button>
                  <button
                    type="button"
                    onClick={() => setState((s) => ({ ...s, purpose: 'infrastructure', namespace: '' }))}
                    className={`p-3 rounded-lg border text-left transition-all ${
                      state.purpose === 'infrastructure'
                        ? 'border-amber-500 bg-amber-500/10'
                        : 'border-surface-600 bg-surface-800/50 hover:border-surface-500'
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      {state.purpose === 'infrastructure' && <CheckCircle className="h-4 w-4 text-amber-400" />}
                      <span className="font-medium text-surface-100">Infrastructure</span>
                    </div>
                    <p className="text-xs text-surface-400">
                      For VPC NAT gateway external connectivity. No NAD created.
                    </p>
                  </button>
                </div>
              </div>

              {/* Namespace Selection (only for VM purpose) */}
              {state.purpose === 'vm' && (
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  Target Namespace
                </label>
                <CustomSelect
                  value={state.namespace}
                  onChange={(v) => setState((s) => ({ ...s, namespace: v }))}
                  placeholder="Select namespace..."
                  options={[{ value: '', label: 'Select namespace...' }, ...userNamespaces.map(ns => ({ value: ns, label: ns }))]}
                />
                <p className="text-xs text-surface-500 mt-1">
                  A NetworkAttachmentDefinition will be created in this namespace. VMs in this namespace can use the network via Multus.
                </p>
              </div>
              )}
            </div>
          </div>
        );

      case 'review': // Review
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                Review Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Confirm your network settings before creating
              </p>
            </div>

            <div className="space-y-4">
              {/* Provider Network */}
              <div className={`rounded-lg p-4 ${existingProvider ? 'bg-surface-800/30 border border-surface-700' : 'bg-surface-800/50'}`}>
                <div className="flex items-center gap-2 mb-3">
                  <Globe className="h-4 w-4 text-emerald-400" />
                  <span className="font-medium text-surface-100">Provider Network</span>
                  {existingProvider && (
                    <span className="px-2 py-0.5 text-[10px] rounded bg-surface-700 text-surface-400">EXISTING</span>
                  )}
                </div>
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <dt className="text-surface-400">Name:</dt>
                  <dd className="text-surface-200">{existingProvider?.name || state.providerName}</dd>
                  <dt className="text-surface-400">Mode:</dt>
                  <dd className="text-surface-200">
                    {existingProvider
                      ? (existingProvider.default_interface.includes('.') ? 'Single NIC' : 'Dedicated NIC')
                      : (state.interfaceMode === 'dedicated' ? 'Dedicated NIC' : 'Single NIC')}
                  </dd>
                  <dt className="text-surface-400">Interface:</dt>
                  <dd className="text-surface-200 font-mono">
                    {existingProvider?.default_interface || (
                      state.interfaceMode === 'dedicated'
                        ? state.baseInterface
                        : `${state.baseInterface}.${state.vlanId}`
                    )}
                  </dd>
                </dl>
              </div>

              {/* VLAN */}
              <div className={`rounded-lg p-4 ${existingVlan ? 'bg-surface-800/30 border border-surface-700' : 'bg-surface-800/50'}`}>
                <div className="flex items-center gap-2 mb-3">
                  <Layers className="h-4 w-4 text-amber-400" />
                  <span className="font-medium text-surface-100">VLAN</span>
                  {existingVlan && (
                    <span className="px-2 py-0.5 text-[10px] rounded bg-surface-700 text-surface-400">EXISTING</span>
                  )}
                </div>
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <dt className="text-surface-400">Name:</dt>
                  <dd className="text-surface-200">
                    {existingVlan?.name || state.vlanName || `${state.providerName}-vlan${state.vlanId}`}
                  </dd>
                  <dt className="text-surface-400">VLAN ID:</dt>
                  <dd className="text-surface-200">{existingVlan?.id ?? state.vlanId}</dd>
                  {!existingVlan && (
                    <>
                      <dt className="text-surface-400">OVS VLAN ID:</dt>
                      <dd className="text-surface-200">
                        {state.interfaceMode === 'dedicated'
                          ? `${state.vlanId} (OVN tags on bridge)`
                          : '0 (sub-interface does tagging)'}
                      </dd>
                    </>
                  )}
                </dl>
              </div>

              {/* Subnet */}
              <div className="bg-surface-800/50 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Server className="h-4 w-4 text-primary-400" />
                  <span className="font-medium text-surface-100">Subnet</span>
                </div>
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <dt className="text-surface-400">Name:</dt>
                  <dd className="text-surface-200">{state.subnetName}</dd>
                  <dt className="text-surface-400">CIDR:</dt>
                  <dd className="text-surface-200 font-mono">{state.cidrBlock}</dd>
                  <dt className="text-surface-400">Gateway:</dt>
                  <dd className="text-surface-200 font-mono">{state.gateway}</dd>
                  {(state.dhcpPoolStart || state.dhcpPoolEnd) && (
                    <>
                      <dt className="text-surface-400">DHCP Pool:</dt>
                      <dd className="text-surface-200 font-mono">
                        {state.dhcpPoolStart || '(start)'}..{state.dhcpPoolEnd || '(end)'}
                      </dd>
                    </>
                  )}
                  <dt className="text-surface-400">DHCP:</dt>
                  <dd className="text-surface-200">{state.enableDhcp ? 'Enabled' : 'Disabled'}</dd>
                  <dt className="text-surface-400">Gateway Check:</dt>
                  <dd className="text-surface-200">{state.disableGatewayCheck ? 'Disabled' : 'Enabled'}</dd>
                  <dt className="text-surface-400">Purpose:</dt>
                  <dd className="text-surface-200">
                    <span className={`px-2 py-0.5 text-xs rounded ${
                      state.purpose === 'infrastructure'
                        ? 'bg-amber-500/20 text-amber-300'
                        : 'bg-primary-500/20 text-primary-300'
                    }`}>
                      {state.purpose === 'infrastructure' ? 'Infrastructure' : 'VM Network'}
                    </span>
                  </dd>
                  {state.purpose === 'vm' && (
                    <>
                      <dt className="text-surface-400">Namespace:</dt>
                      <dd className="text-surface-200 font-mono">{state.namespace || '(none)'}</dd>
                    </>
                  )}
                </dl>

                {/* Show what will be excluded */}
                {calculatedExcludeIps.length > 0 && (
                  <div className="mt-3 pt-3 border-t border-surface-700">
                    <p className="text-xs text-surface-400 mb-2">
                      IPs that will be excluded from allocation:
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {calculatedExcludeIps.map((ip, idx) => (
                        <span
                          key={idx}
                          className="px-2 py-0.5 bg-surface-700 rounded text-xs font-mono text-surface-300"
                        >
                          {ip}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

              </div>
            </div>
          </div>
        );

      case 'vpc-config':
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                VPC Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Create an isolated virtual private cloud network
              </p>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">VPC Name</label>
                <input
                  type="text"
                  value={state.vpcName}
                  onChange={(e) => setState((s) => ({
                    ...s,
                    vpcName: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''),
                  }))}
                  className="input w-full"
                  placeholder="my-vpc"
                />
                <p className="text-xs text-surface-500 mt-1">Lowercase letters, numbers, hyphens only</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-200 mb-2">
                  Subnet CIDR
                  <span className="text-surface-500 font-normal ml-1">(optional — auto-allocated if empty)</span>
                </label>
                <input
                  type="text"
                  value={state.vpcSubnetCidr}
                  onChange={(e) => setState((s) => ({ ...s, vpcSubnetCidr: e.target.value }))}
                  className="input w-full font-mono"
                  placeholder="10.100.0.0/24"
                />
              </div>

              {/* CIDR suggestions */}
              <div>
                <p className="text-xs text-surface-400 mb-2">Quick select:</p>
                <div className="flex flex-wrap gap-2">
                  {['10.100.0.0/24', '10.200.0.0/24', '172.20.0.0/16', '192.168.100.0/24'].map((cidr) => (
                    <button
                      key={cidr}
                      type="button"
                      onClick={() => setState((s) => ({ ...s, vpcSubnetCidr: cidr }))}
                      className={`px-3 py-1.5 rounded-lg border text-xs font-mono transition-colors ${
                        state.vpcSubnetCidr === cidr
                          ? 'border-primary-500 bg-primary-500/10 text-primary-400'
                          : 'border-surface-700 text-surface-400 hover:border-surface-600'
                      }`}
                    >
                      {cidr}
                    </button>
                  ))}
                </div>
              </div>

              {/* NAT Gateway toggle */}
              <div className="flex items-center justify-between p-4 bg-surface-800/50 rounded-lg border border-surface-700">
                <div>
                  <p className="text-sm font-medium text-surface-200">NAT Gateway</p>
                  <p className="text-xs text-surface-400 mt-1">
                    Allow VMs in this VPC to access external networks via NAT
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setState((s) => ({ ...s, vpcEnableNat: !s.vpcEnableNat }))}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                    state.vpcEnableNat ? 'bg-primary-500' : 'bg-surface-600'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      state.vpcEnableNat ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>
            </div>
          </div>
        );

      case 'vpc-peering':
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                VPC Peering
              </h3>
              <p className="text-surface-400 text-sm">
                Configure connectivity between this VPC and the host cluster
              </p>
            </div>

            <div className="space-y-4">
              <div className="flex items-center justify-between p-4 bg-surface-800/50 rounded-lg border border-surface-700">
                <div>
                  <p className="text-sm font-medium text-surface-200">Enable host cluster access</p>
                  <p className="text-xs text-surface-400 mt-1">
                    Allow VMs in this VPC to communicate with services in the host cluster default VPC
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setState((s) => ({ ...s, vpcEnablePeering: !s.vpcEnablePeering }))}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                    state.vpcEnablePeering ? 'bg-primary-500' : 'bg-surface-600'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      state.vpcEnablePeering ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>

              <div className="bg-surface-800/30 rounded-lg p-4 border border-surface-700">
                <div className="flex items-start gap-2">
                  <Info className="h-4 w-4 text-primary-400 mt-0.5 shrink-0" />
                  <div className="text-sm text-surface-400 space-y-2">
                    <p>
                      <strong className="text-surface-200">With peering enabled:</strong> VMs can reach host cluster services
                      (DNS, monitoring, storage). Recommended for most use cases.
                    </p>
                    <p>
                      <strong className="text-surface-200">Without peering:</strong> The VPC is fully isolated.
                      VMs can only communicate within the VPC network.
                      {state.vpcEnableNat && ' NAT gateway still provides outbound internet access.'}
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        );

      case 'vpc-review':
        return (
          <div className="space-y-6">
            <div className="text-center mb-6">
              <h3 className="text-lg font-semibold text-surface-100">
                Review VPC Configuration
              </h3>
              <p className="text-surface-400 text-sm">
                Confirm your VPC settings before creating
              </p>
            </div>

            <div className="space-y-4">
              {/* VPC */}
              <div className="bg-surface-800/50 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Building2 className="h-4 w-4 text-amber-400" />
                  <span className="font-medium text-surface-100">VPC</span>
                </div>
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <dt className="text-surface-400">Name:</dt>
                  <dd className="text-surface-200">{state.vpcName}</dd>
                  <dt className="text-surface-400">Subnet CIDR:</dt>
                  <dd className="text-surface-200 font-mono">{state.vpcSubnetCidr || '(auto-allocated)'}</dd>
                  <dt className="text-surface-400">NAT Gateway:</dt>
                  <dd className="text-surface-200">
                    {state.vpcEnableNat ? (
                      <span className="text-emerald-400">Enabled</span>
                    ) : (
                      <span className="text-surface-500">Disabled</span>
                    )}
                  </dd>
                </dl>
              </div>

              {/* Peering */}
              <div className="bg-surface-800/50 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Server className="h-4 w-4 text-emerald-400" />
                  <span className="font-medium text-surface-100">Connectivity</span>
                </div>
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <dt className="text-surface-400">Host Cluster Peering:</dt>
                  <dd className="text-surface-200">
                    {state.vpcEnablePeering ? (
                      <span className="text-emerald-400">Enabled</span>
                    ) : (
                      <span className="text-surface-500">Disabled (isolated)</span>
                    )}
                  </dd>
                </dl>
              </div>
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  const canGoNext = stepIndex < steps.length - 1;
  const canGoBack = stepIndex > 0;
  const isLastStep = stepIndex === steps.length - 1;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="card max-w-2xl w-full mx-4 max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-surface-700">
          <h2 className="text-xl font-semibold text-surface-100">
            {existingVlan
              ? `Add Subnet to ${existingVlan.name}`
              : existingProvider
              ? `Add VLAN + Subnet to ${existingProvider.name}`
              : 'Create Network'}
          </h2>
          <button
            onClick={onClose}
            className="p-2 hover:bg-surface-700 rounded-lg text-surface-400"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Progress */}
        <div className="px-6 py-4 border-b border-surface-700">
          <WizardStepIndicator
            steps={steps.map(s => STEP_LABELS[s])}
            currentStep={stepIndex}
          />
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">{renderStep()}</div>

        {/* Error */}
        {error && (
          <div className="px-6 py-3 bg-red-500/10 border-t border-red-500/20">
            <div className="flex items-center gap-2 text-red-400">
              <AlertCircle className="h-4 w-4" />
              <span className="text-sm">{error}</span>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between p-4 border-t border-surface-700">
          <button
            onClick={handleBack}
            disabled={!canGoBack}
            className="btn-secondary"
          >
            <ChevronLeft className="h-4 w-4" />
            Back
          </button>

          {isLastStep ? (
            <button
              onClick={state.type === 'vpc' ? handleCreateVpc : handleCreate}
              disabled={isCreating}
              className="btn-primary"
            >
              {isCreating ? 'Creating...' : state.type === 'vpc' ? 'Create VPC' : 'Create Network'}
            </button>
          ) : (
            <button
              onClick={() => {
                if (validateStep()) handleNext();
              }}
              disabled={!canGoNext}
              className="btn-primary"
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

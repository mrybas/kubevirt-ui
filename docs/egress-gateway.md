# Egress Gateway — Hub-and-Spoke Architecture

## Use Case

You rent a dedicated server, order an additional public IP, configure a macvlan subnet
via kube-ovn pointing at that IP, and select it in the UI — all tenants get internet
access through a single shared egress gateway. No per-tenant NAT rules, no per-tenant
external IPs. One gateway, one IP, N tenants.

## How It Works

The egress gateway uses a **hub-and-spoke** VPC peering model:

```
                        ┌──────────────────────────┐
                        │    Physical Network      │
                        │   (macvlan, e.g. eth0)   │
                        └────────────┬─────────────┘
                                     │ SNAT
                        ┌────────────▼─────────────┐
                        │   VpcEgressGateway Pods  │
                        │   (replicas: 2, BFD HA)  │
                        └────────────┬─────────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │     Gateway VPC          │
                        │   egw-shared-egress      │
                        │                          │
                        │  Internal: 10.199.0.0/24 │
                        │  Transit:  10.255.0.0/24 │
                        └──┬─────────┬──────────┬──┘
                           │         │          │
                  VpcPeering│  VpcPeering│   VpcPeering│
                           │         │          │
               ┌───────────▼┐  ┌─────▼──────┐  ┌▼───────────┐
               │ Tenant VPC │  │ Tenant VPC │  │ Tenant VPC │
               │ vpc-alpha  │  │ vpc-beta   │  │ vpc-gamma  │
               │ 10.200.0/24│  │ 10.201.0/24│  │ 10.202.0/24│
               └────────────┘  └────────────┘  └────────────┘
```

### Data Flow (tenant pod → internet)

1. Pod in `vpc-alpha` sends packet to `8.8.8.8`
2. Tenant VPC static route: `0.0.0.0/0 → 10.255.0.1` (gateway transit IP)
3. VpcPeering forwards packet through transit subnet to gateway VPC
4. VpcEgressGateway performs SNAT via macvlan interface
5. Packet exits on physical network with the macvlan IP as source

### Return path

1. Response arrives on macvlan interface
2. VpcEgressGateway reverse-NATs back to tenant pod IP
3. Gateway VPC static route: `10.200.0.0/24 → 10.255.0.2` (tenant transit IP)
4. VpcPeering forwards back to tenant VPC
5. Pod receives the response

### What Gets Created

When a gateway is created:
- **Gateway VPC** (`egw-{name}`) with two subnets:
  - Internal subnet (`egw-{name}-subnet`) — for gateway pod networking
  - Transit subnet (`egw-{name}-transit`) — for VPC peering connections
- **VpcEgressGateway** CR — manages SNAT pods with macvlan
- **ConfigMap** (`egress-transit-{name}`) — tracks transit IP allocations

When a tenant VPC is attached:
- **VpcPeering** (`{gateway}-to-{tenant-vpc}`) — connects the two VPCs
- **Static route on tenant VPC**: `0.0.0.0/0 → {gateway-transit-ip}`
- **Static route on gateway VPC**: `{tenant-cidr} → {tenant-transit-ip}`
- **VpcEgressGateway policy** updated to include tenant subnet CIDR
- **ACL rules** on tenant subnet: allow traffic to transit and gateway CIDRs

## Deployment Topologies

### Shared Gateway (recommended, cheapest)

One gateway for all tenants. Single external IP.

```
POST /api/v1/egress-gateways
{
    "name": "shared-egress",
    "macvlan_subnet": "macvlan-eth0",
    "replicas": 2
}
```

All new tenants auto-attach to this gateway (if marked as default).

### Per-VPC Gateway (maximum isolation)

Dedicated gateway per tenant. Each gets its own external IP and SNAT.

```
POST /api/v1/egress-gateways
{
    "name": "egress-for-alpha",
    "gw_vpc_cidr": "10.197.0.0/24",
    "transit_cidr": "10.253.0.0/24",
    "macvlan_subnet": "macvlan-eth0"
}

POST /api/v1/egress-gateways/egress-for-alpha/attach
{"vpc_name": "vpc-alpha", "subnet_name": "vpc-alpha-default", "cidr": "10.200.0.0/24"}
```

### Gateway Groups

Gateway A handles production tenants, Gateway B handles staging:

```
Gateway A (macvlan-prod-ip) ← vpc-prod1, vpc-prod2, vpc-prod3
Gateway B (macvlan-staging-ip) ← vpc-staging1, vpc-staging2
```

Each gateway can use a different macvlan subnet (different external IP).

## Node Selector

Gateway pods run on nodes that have physical network access for macvlan.
Use `node_selector` to pin them:

```json
{
    "node_selector": {"role": "egress"}
}
```

This maps to `nodeSelector.matchLabels` on the VpcEgressGateway CR.
Label your nodes: `kubectl label node worker-1 role=egress`.

## High Availability: Replicas + BFD

### Replicas

`replicas` controls how many egress gateway pods run. Default: 2.
Kube-OVN distributes traffic across replicas and handles failover.

### BFD (Bidirectional Forwarding Detection)

`bfd_enabled` (default: `true`) enables sub-second failure detection between
the OVN logical router and egress gateway pods. Without BFD, failover relies
on OVN's default health checking which can take 30-60 seconds.

With BFD enabled:
- Failure detection: ~1 second
- Traffic reroutes to healthy replica automatically
- No manual intervention needed

## UI Wizard Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| **name** | Yes | — | Gateway name. Used as prefix for all resources (`egw-{name}`, etc.) |
| **macvlan_subnet** | Yes | — | Existing kube-ovn Subnet with macvlan provider. This is where SNAT happens. |
| **gw_vpc_cidr** | No | `10.199.0.0/24` | Internal CIDR for gateway VPC. Change if default conflicts with your network. |
| **transit_cidr** | No | `10.255.0.0/24` | Transit CIDR for VPC peering. Each attached tenant gets an IP from this range. |
| **replicas** | No | `2` | Number of egress gateway pods. More replicas = more throughput + redundancy. |
| **bfd_enabled** | No | `true` | Enable BFD for fast failover. Disable only if your network doesn't support it. |
| **node_selector** | No | `{}` | Pin gateway pods to specific nodes (e.g. `{"role": "egress"}`). |

## Example: Manual macvlan Setup + UI Gateway

### Step 1: Create macvlan NetworkAttachmentDefinition and Subnet

This is a one-time setup on the cluster. The macvlan subnet points at a physical
interface with an external IP.

```bash
# In toolbox container (make dev-shell)

# Create provider network for macvlan
cat <<EOF | kubectl apply -f -
apiVersion: kubeovn.io/v1
kind: ProviderNetwork
metadata:
  name: macvlan-eth0
spec:
  defaultInterface: eth0
EOF

# Create VLAN (if needed)
cat <<EOF | kubectl apply -f -
apiVersion: kubeovn.io/v1
kind: Vlan
metadata:
  name: vlan0
spec:
  id: 0
  provider: macvlan-eth0
EOF

# Create subnet for macvlan
# This subnet must contain the external IP(s) you want to use for SNAT
cat <<EOF | kubectl apply -f -
apiVersion: kubeovn.io/v1
kind: Subnet
metadata:
  name: macvlan-eth0
spec:
  protocol: IPv4
  cidrBlock: 203.0.113.0/24        # Your public IP range
  gateway: 203.0.113.1             # Your gateway
  vlan: vlan0
  provider: macvlan-eth0.kube-system
  excludeIps:
    - 203.0.113.1                  # Gateway
    - 203.0.113.100                # Server's own IP
EOF
```

### Step 2: Create Egress Gateway in UI

Navigate to **Network > Egress Gateways > Create** and fill in:
- **Name**: `shared-egress`
- **Macvlan Subnet**: select `macvlan-eth0` from dropdown
- **Replicas**: `2`
- Leave other fields as defaults

### Step 3: Attach Tenants

Tenants created after the gateway is set as default will auto-attach.
For existing tenants, use **Network > Egress Gateways > shared-egress > Attach VPC**.

## Prerequisites

1. **Multus-CNI** installed in the cluster (for macvlan network attachment)
2. **Cilium** with `enableSourceIpVerification: false` (otherwise macvlan traffic gets dropped)
3. **Physical network/VLAN** accessible from at least one node for macvlan
4. **Kube-OVN** with VpcEgressGateway CRD support (v1.12+)
5. **External IP** — at least one IP address in the macvlan subnet available for SNAT

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/egress-gateways` | List all gateways with status and attached VPCs |
| `GET` | `/api/v1/egress-gateways/{name}` | Gateway details |
| `POST` | `/api/v1/egress-gateways` | Create gateway |
| `DELETE` | `/api/v1/egress-gateways/{name}` | Delete (fails if VPCs attached) |
| `POST` | `/api/v1/egress-gateways/{name}/attach` | Attach tenant VPC |
| `POST` | `/api/v1/egress-gateways/{name}/detach` | Detach tenant VPC |

## Troubleshooting

**Tenant has no internet after creation**
- Check if an egress gateway exists: `GET /api/v1/egress-gateways`
- If no gateway, create one and attach the tenant VPC

**Gateway shows not ready**
- Check VpcEgressGateway status: pods might not be scheduled (node selector mismatch)
- Verify macvlan subnet exists and has available IPs

**Attached VPC can't reach internet**
- Verify VpcPeering exists: check gateway details for the peering name
- Check static routes on both VPCs
- Verify VpcEgressGateway policies include the tenant CIDR
- Check tenant subnet ACLs allow transit CIDR traffic

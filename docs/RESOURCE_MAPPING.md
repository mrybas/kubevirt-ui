# UI Actions → Kubernetes Resources

This document describes which Kubernetes resources are created/modified for each UI operation.

## Virtual Machines

### Create VM

**UI action:** Wizard "Create Virtual Machine"

**Created resources:**

1. **VirtualMachine** (required)
2. **DataVolume** (if importing an image)
3. **Secret** (if cloud-init has passwords)

```yaml
# 1. DataVolume for the system disk
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: my-vm-root-disk
  namespace: vms
spec:
  source:
    http:
      url: "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
  storage:
    storageClassName: longhorn
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 20Gi

---
# 2. Secret for cloud-init (optional)
apiVersion: v1
kind: Secret
metadata:
  name: my-vm-cloudinit
  namespace: vms
type: Opaque
stringData:
  userdata: |
    #cloud-config
    users:
      - name: admin
        sudo: ALL=(ALL) NOPASSWD:ALL
        ssh_authorized_keys:
          - ssh-rsa AAAAB...

---
# 3. VirtualMachine
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: my-vm
  namespace: vms
  labels:
    app: my-vm
    kubevirt-ui.io/created-by: kubevirt-ui
spec:
  runStrategy: Always
  template:
    metadata:
      labels:
        app: my-vm
        kubevirt.io/vm: my-vm
    spec:
      domain:
        cpu:
          cores: 2
          threads: 1
          sockets: 1
        memory:
          guest: 4Gi
        devices:
          disks:
            - name: root-disk
              disk:
                bus: virtio
            - name: cloudinit
              disk:
                bus: virtio
          interfaces:
            - name: default
              masquerade: {}
        machine:
          type: q35
      networks:
        - name: default
          pod: {}
      volumes:
        - name: root-disk
          dataVolume:
            name: my-vm-root-disk
        - name: cloudinit
          cloudInitNoCloud:
            secretRef:
              name: my-vm-cloudinit
```

### Start VM

**UI action:** Button "Start"

**Operation:** PATCH VirtualMachine

```yaml
# PATCH /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}
spec:
  runStrategy: Always
```

**Available runStrategy values:**
- `Always` — VM always running, auto-restart on failure
- `Halted` — VM stopped
- `Manual` — manual start/stop control
- `RerunOnFailure` — restart only on failure
- `Once` — run once, do not restart

### Stop VM

**UI action:** Button "Stop"

**Operation:** PATCH VirtualMachine

```yaml
# PATCH /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}
spec:
  runStrategy: Halted
```

### Restart VM

**UI action:** Button "Restart"

**Operation:** DELETE VirtualMachineInstance (VM controller recreates it)

```bash
# DELETE /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}
```

### Live Migration

**UI action:** Button "Migrate"

**Created resource:** VirtualMachineInstanceMigration

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachineInstanceMigration
metadata:
  name: my-vm-migration-abc123
  namespace: vms
spec:
  vmiName: my-vm
```

### Edit VM

**UI action:** Edit form or YAML editor

**Operation:** PUT VirtualMachine

```yaml
# PUT /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}
# Full VM spec with modified fields
```

### Delete VM

**UI action:** Button "Delete"

**Operations:**
1. DELETE VirtualMachine
2. (Optional) DELETE associated DataVolumes/PVCs

```bash
# DELETE /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}
```

---

## Storage

### Create DataVolume (Import)

**UI action:** "Import Image" wizard

**Created resource:** DataVolume

```yaml
# HTTP import
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: ubuntu-22-04-base
  namespace: vms
  labels:
    kubevirt-ui.io/type: base-image
spec:
  source:
    http:
      url: "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
  storage:
    storageClassName: longhorn
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 10Gi

---
# Registry import
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: fedora-container-disk
  namespace: vms
spec:
  source:
    registry:
      url: "docker://quay.io/kubevirt/fedora-container-disk-demo:latest"
  storage:
    storageClassName: longhorn
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 5Gi

---
# S3 import
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: windows-server-2022
  namespace: vms
spec:
  source:
    s3:
      url: "s3://my-bucket/images/windows-2022.qcow2"
      secretRef: s3-credentials
  storage:
    storageClassName: longhorn
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 50Gi
```

### Clone Volume

**UI action:** "Clone Volume"

**Created resource:** DataVolume with source.pvc

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: my-vm-disk-clone
  namespace: vms
spec:
  source:
    pvc:
      namespace: vms
      name: my-vm-root-disk
  storage:
    storageClassName: longhorn
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 20Gi
```

### Create Snapshot

**UI action:** "Create Snapshot"

**Created resource:** VirtualMachineSnapshot

```yaml
apiVersion: snapshot.kubevirt.io/v1beta1
kind: VirtualMachineSnapshot
metadata:
  name: my-vm-snapshot-2026-01-18
  namespace: vms
spec:
  source:
    apiGroup: kubevirt.io
    kind: VirtualMachine
    name: my-vm
  failureDeadline: 5m
```

### Restore from Snapshot

**UI action:** "Restore Snapshot"

**Created resource:** VirtualMachineRestore

```yaml
apiVersion: snapshot.kubevirt.io/v1beta1
kind: VirtualMachineRestore
metadata:
  name: my-vm-restore-abc123
  namespace: vms
spec:
  target:
    apiGroup: kubevirt.io
    kind: VirtualMachine
    name: my-vm
  virtualMachineSnapshotName: my-vm-snapshot-2026-01-18
```

---

## Network (Kube-OVN)

Networking is built on Kube-OVN. Hierarchy: **ProviderNetwork → VLAN → Subnet → IP**.

### Two VLAN Modes

| Mode | defaultInterface | VLAN id | Use case |
|-------|-----------------|---------|----------------|
| **Dedicated NIC** (trunk) | `eth1` (no dot) | actual ID (111, 222) | Production, dedicated NIC for VMs |
| **Single-NIC** (sub-interface) | `eno1.111` (with dot) | 0 (OVS does not tag) | Homelab, single NIC |

**Heuristic:** if interface contains a dot → single-NIC; no dot → trunk.

### Create ProviderNetwork

**UI action:** Network wizard → Provider step

```yaml
apiVersion: kubeovn.io/v1
kind: ProviderNetwork
metadata:
  name: netprov-vlan111
spec:
  defaultInterface: eno1.111    # single-NIC mode
  # or
  defaultInterface: eth1        # dedicated/trunk mode
```

### Create VLAN

**UI action:** Network wizard → VLAN step

```yaml
apiVersion: kubeovn.io/v1
kind: Vlan
metadata:
  name: vlan111
spec:
  id: 0                          # single-NIC: 0 (no double-tagging)
  # or
  id: 111                        # trunk: actual VLAN ID
  provider: netprov-vlan111
```

### Create Subnet

**UI action:** Network wizard → Subnet step

```yaml
apiVersion: kubeovn.io/v1
kind: Subnet
metadata:
  name: subnet-vlan111
spec:
  protocol: IPv4
  cidrBlock: "10.111.0.0/24"
  gateway: "10.111.0.1"
  vlan: vlan111
  namespaces:
    - analytics-dev
    - analytics-prod
```

Subnet also automatically creates a **NetworkAttachmentDefinition** for Multus.

### Connecting a VM to an External Network

**VM spec with bridge interface:**

```yaml
spec:
  template:
    spec:
      domain:
        devices:
          interfaces:
            - name: default
              masquerade: {}
            - name: vlan111
              bridge: {}
      networks:
        - name: default
          pod: {}
        - name: vlan111
          multus:
            networkName: subnet-vlan111
```

### IP Reservation

**UI action:** "Reserve IP" in subnet detail

```yaml
apiVersion: kubeovn.io/v1
kind: IP
metadata:
  name: reserved-10.111.0.50
spec:
  subnet: subnet-vlan111
  ipAddress: "10.111.0.50"
```

---

## Projects & Environments

### Create Project

**UI action:** "Create Project" modal

**Created resources:**

1. **ConfigMap entry** in `kubevirt-ui-projects` (project metadata)
2. **Namespace** for each initial environment

```yaml
# 1. ConfigMap entry (added to existing ConfigMap)
# PATCH /api/v1/namespaces/kubevirt-ui-system/configmaps/kubevirt-ui-projects
data:
  analytics: |
    {"display_name": "Analytics Platform", "description": "Data project", "created_by": "admin@example.com"}

---
# 2. Namespace for each environment
apiVersion: v1
kind: Namespace
metadata:
  name: analytics-dev
  labels:
    kubevirt-ui.io/enabled: "true"
    kubevirt-ui.io/managed: "true"
    kubevirt-ui.io/project: analytics
    kubevirt-ui.io/environment: dev
  annotations:
    kubevirt-ui.io/display-name: analytics-dev
```

### Add Environment

**UI action:** "Add Environment" (inline text input + Add)

**Created resource:** Namespace + copy of project-level RoleBindings

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: analytics-prod
  labels:
    kubevirt-ui.io/enabled: "true"
    kubevirt-ui.io/managed: "true"
    kubevirt-ui.io/project: analytics
    kubevirt-ui.io/environment: prod
```

If project-level RoleBindings exist in sibling environments, they are automatically copied to the new namespace.

### Delete Project

**UI action:** "Delete Project" (with name confirmation)

**Operations:**
1. DELETE all environment namespaces (`analytics-dev`, `analytics-staging`, `analytics-prod`)
2. DELETE entry from ConfigMap `kubevirt-ui-projects`

### Delete Environment

**UI action:** "Remove Environment" (trash icon)

**Operation:** DELETE Namespace (cascading delete of all resources inside)

## RBAC / Access Control

### Add Project-level Access

**UI action:** "Add Team or User" in Access modal

**Created resources:** RoleBinding in **every** environment namespace of the project

```yaml
# Created in analytics-dev, analytics-staging, analytics-prod
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubevirt-ui-team-devops-editor
  namespace: analytics-dev    # (and analytics-staging, analytics-prod)
  labels:
    kubevirt-ui.io/managed: "true"
    kubevirt-ui.io/project: analytics
    kubevirt-ui.io/access-scope: project
subjects:
  - kind: Group
    name: devops
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: kubevirt-ui-editor
  apiGroup: rbac.authorization.k8s.io
```

### Add Environment-level Access

**UI action:** "Add Access" with scope=environment

**Created resource:** RoleBinding only in the specific namespace

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubevirt-ui-user-john-viewer
  namespace: analytics-prod
  labels:
    kubevirt-ui.io/managed: "true"
    kubevirt-ui.io/project: analytics
    kubevirt-ui.io/access-scope: environment
    kubevirt-ui.io/access-environment: prod
subjects:
  - kind: User
    name: john@example.com
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: kubevirt-ui-viewer
  apiGroup: rbac.authorization.k8s.io
```

## User Profile

### Update SSH Keys

**UI action:** Profile page — add/remove SSH keys

**Operation:** PATCH ConfigMap `kubevirt-ui-profiles`

```yaml
# PATCH /api/v1/namespaces/kubevirt-ui-system/configmaps/kubevirt-ui-profiles
data:
  admin@example.com: |
    {"ssh_keys": ["ssh-rsa AAAA...", "ssh-ed25519 AAAA..."]}
```

SSH keys are automatically injected into cloud-init during VM creation:

```yaml
# cloud-init userData
#cloud-config
ssh_authorized_keys:
  - ssh-rsa AAAA...     # from profile
  - ssh-ed25519 AAAA... # from profile
  - ssh-rsa BBBB...     # from VM creation form (if specified)
```

---

## Audit / Events

### Audit Event Recording

**UI action:** (automatic on every action)

**Created resource:** Event or VirtAuditEvent CRD

```yaml
# Option 1: Kubernetes Event
apiVersion: v1
kind: Event
metadata:
  name: vm-create-event-abc123
  namespace: vms
involvedObject:
  apiVersion: kubevirt.io/v1
  kind: VirtualMachine
  name: my-vm
  namespace: vms
reason: VMCreated
message: "VirtualMachine my-vm created by admin@example.com via KubeVirt UI"
type: Normal
source:
  component: kubevirt-ui
firstTimestamp: "2026-01-18T10:30:00Z"
lastTimestamp: "2026-01-18T10:30:00Z"

---
# Option 2: Custom CRD
apiVersion: kubevirt-ui.io/v1alpha1
kind: VirtAuditEvent
metadata:
  name: audit-2026-01-18-abc123
  namespace: vms
spec:
  user: admin@example.com
  action: create
  resourceType: VirtualMachine
  resourceName: my-vm
  resourceNamespace: vms
  timestamp: "2026-01-18T10:30:00Z"
  sourceIP: "192.168.1.100"
  userAgent: "KubeVirt-UI/1.0"
  requestBody:
    cpu: 2
    memory: 4Gi
  outcome: success
```

---

## VM Console

### VNC Console

**UI action:** Click "Open Console"

**Operation:** Proxy WebSocket to virt-handler

```
# 1. Get VMI to find virt-launcher pod
GET /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}

# 2. Proxy to VNC endpoint
WebSocket /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/vnc
```

### Serial Console

**UI action:** Click "Serial Console"

**Operation:** Proxy WebSocket to serial console

```
WebSocket /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/console
```

---

## Instance Types & Preferences

### Select Instance Type

**UI action:** Dropdown "Instance Type" in wizard

**Read-only resources:**

```yaml
# Cluster-wide instance types
GET /apis/instancetype.kubevirt.io/v1beta1/virtualmachineclusterinstancetypes

# Namespace-specific instance types  
GET /apis/instancetype.kubevirt.io/v1beta1/namespaces/{ns}/virtualmachineinstancetypes
```

**VM spec with instancetype:**

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: my-vm
spec:
  instancetype:
    kind: VirtualMachineClusterInstancetype
    name: small
  preference:
    kind: VirtualMachineClusterPreference  
    name: ubuntu
  # ... rest of spec
```

---

## Quick Reference Table

| UI Action | K8s Resource | Verb | API Group |
|-----------|--------------|------|-----------|
| Create VM | VirtualMachine + DataVolume | CREATE | kubevirt.io, cdi.kubevirt.io |
| Start VM | VirtualMachine | PATCH | kubevirt.io |
| Stop VM | VirtualMachine subresource | PUT | subresources.kubevirt.io |
| Restart VM | VirtualMachine subresource | PUT | subresources.kubevirt.io |
| Delete VM | VirtualMachine | DELETE | kubevirt.io |
| VNC Console | VMI Subresource | GET (proxy) | subresources.kubevirt.io |
| Serial Console | VMI Subresource | GET (proxy) | subresources.kubevirt.io |
| Resize Disk | PVC | PATCH | "" (core) |
| Create Image from VM | DataVolume (clone) | CREATE | cdi.kubevirt.io |
| Import Image | DataVolume | CREATE | cdi.kubevirt.io |
| Clone Volume | DataVolume | CREATE | cdi.kubevirt.io |
| Create Snapshot | VirtualMachineSnapshot | CREATE | snapshot.kubevirt.io |
| Restore Snapshot | VirtualMachineRestore | CREATE | snapshot.kubevirt.io |
| Create ProviderNetwork | ProviderNetwork | CREATE | kubeovn.io |
| Create VLAN | Vlan | CREATE | kubeovn.io |
| Create Subnet | Subnet | CREATE | kubeovn.io |
| Reserve IP | IP | CREATE | kubeovn.io |
| Create Project | ConfigMap (patch) | PATCH | "" (core) |
| Add Environment | Namespace | CREATE | "" (core) |
| Delete Environment | Namespace | DELETE | "" (core) |
| Add Access (project) | RoleBinding × N envs | CREATE | rbac.authorization.k8s.io |
| Add Access (env) | RoleBinding | CREATE | rbac.authorization.k8s.io |
| Update SSH Keys | ConfigMap (patch) | PATCH | "" (core) |

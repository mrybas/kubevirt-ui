"""Virtual Machine Pydantic models."""

from typing import Any

from pydantic import BaseModel, Field


class VMDiskConfig(BaseModel):
    """Disk configuration for VM creation."""

    name: str
    size: str = "20Gi"
    storage_class: str | None = None
    source_type: str = "blank"  # blank, http, registry, pvc
    source_url: str | None = None
    source_pvc: str | None = None
    source_pvc_namespace: str | None = None
    bus: str = "virtio"


class VMNetworkConfig(BaseModel):
    """Network configuration for VM creation."""

    name: str = "default"
    network_type: str = "pod"  # pod, multus
    multus_network: str | None = None
    interface_type: str = "masquerade"  # masquerade, bridge


class VMCloudInitConfig(BaseModel):
    """Cloud-init configuration."""

    user_data: str | None = None
    network_data: str | None = None


class VMCreateRequest(BaseModel):
    """Request model for creating a VM."""

    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    cpu_cores: int = Field(default=2, ge=1, le=128)
    memory: str = Field(default="2Gi", pattern=r"^\d+[KMGT]i?$")
    run_strategy: str = Field(default="Always", pattern=r"^(Always|Halted|Manual|RerunOnFailure|Once)$")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    # Instance type (optional - overrides cpu/memory)
    instance_type: str | None = None
    preference: str | None = None

    # Disks
    disks: list[VMDiskConfig] = Field(default_factory=list)

    # Container disk (quick start)
    container_disk_image: str | None = None

    # Networks
    networks: list[VMNetworkConfig] = Field(
        default_factory=lambda: [VMNetworkConfig(name="default", network_type="pod")]
    )

    # Cloud-init
    cloud_init: VMCloudInitConfig | None = None

    # Placement
    node_selector: dict[str, str] = Field(default_factory=dict)

    def to_k8s_manifest(self, namespace: str) -> dict[str, Any]:
        """Convert to Kubernetes VirtualMachine manifest."""
        # Build volumes and disks
        volumes: list[dict[str, Any]] = []
        disk_specs: list[dict[str, Any]] = []

        # Container disk (for quick start)
        if self.container_disk_image:
            volumes.append(
                {
                    "name": "containerdisk",
                    "containerDisk": {"image": self.container_disk_image},
                }
            )
            disk_specs.append(
                {"name": "containerdisk", "disk": {"bus": "virtio"}}
            )

        # Custom disks
        for disk in self.disks:
            if disk.source_type == "blank":
                # Will need DataVolume creation separately
                volumes.append(
                    {
                        "name": disk.name,
                        "dataVolume": {"name": f"{self.name}-{disk.name}"},
                    }
                )
            elif disk.source_type == "pvc":
                volumes.append(
                    {
                        "name": disk.name,
                        "persistentVolumeClaim": {"claimName": disk.source_pvc},
                    }
                )

            disk_specs.append({"name": disk.name, "disk": {"bus": disk.bus}})

        # Cloud-init
        if self.cloud_init and self.cloud_init.user_data:
            volumes.append(
                {
                    "name": "cloudinit",
                    "cloudInitNoCloud": {"userData": self.cloud_init.user_data},
                }
            )
            disk_specs.append({"name": "cloudinit", "disk": {"bus": "virtio"}})

        # Build networks and interfaces
        network_specs: list[dict[str, Any]] = []
        interface_specs: list[dict[str, Any]] = []

        for net in self.networks:
            if net.network_type == "pod":
                network_specs.append({"name": net.name, "pod": {}})
            elif net.network_type == "multus" and net.multus_network:
                network_specs.append(
                    {"name": net.name, "multus": {"networkName": net.multus_network}}
                )

            if net.interface_type == "masquerade":
                interface_specs.append({"name": net.name, "masquerade": {}})
            elif net.interface_type == "bridge":
                interface_specs.append({"name": net.name, "bridge": {}})

        # Build domain spec
        domain: dict[str, Any] = {
            "devices": {
                "disks": disk_specs,
                "interfaces": interface_specs,
            },
            "machine": {"type": "q35"},
        }

        # Add resources if not using instance type
        if not self.instance_type:
            domain["cpu"] = {"cores": self.cpu_cores}
            domain["resources"] = {"requests": {"memory": self.memory}}

        # Build template spec
        template_spec: dict[str, Any] = {
            "domain": domain,
            "networks": network_specs,
            "volumes": volumes,
        }

        if self.node_selector:
            template_spec["nodeSelector"] = self.node_selector

        # Build VM spec
        vm_spec: dict[str, Any] = {
            "runStrategy": self.run_strategy,
            "template": {
                "metadata": {
                    "labels": {
                        "kubevirt.io/vm": self.name,
                        **self.labels,
                    }
                },
                "spec": template_spec,
            },
        }

        if self.instance_type:
            vm_spec["instancetype"] = {
                "kind": "VirtualMachineClusterInstancetype",
                "name": self.instance_type,
            }

        if self.preference:
            vm_spec["preference"] = {
                "kind": "VirtualMachineClusterPreference",
                "name": self.preference,
            }

        # Build full manifest
        manifest: dict[str, Any] = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": self.name,
                "namespace": namespace,
                "labels": {
                    "app": self.name,
                    "kubevirt-ui.io/created-by": "kubevirt-ui",
                    **self.labels,
                },
                "annotations": self.annotations,
            },
            "spec": vm_spec,
        }

        return manifest


class VMConsoleConfig(BaseModel):
    """Console configuration for VM."""
    
    vnc_enabled: bool = True
    serial_console_enabled: bool = False


class VMUpdateRequest(BaseModel):
    """Request model for updating a VM."""

    cpu_cores: int | None = Field(default=None, ge=1, le=128)
    memory: str | None = Field(default=None, pattern=r"^\d+[KMGT]i?$")
    run_strategy: str | None = Field(default=None, pattern=r"^(Always|Halted|Manual|RerunOnFailure|Once)$")
    
    # Console settings
    console: VMConsoleConfig | None = None
    
    # Labels and annotations
    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None


class VMDiskInfo(BaseModel):
    """Detailed disk information."""

    name: str
    type: str  # dataVolume, persistentVolumeClaim, cloudInitNoCloud, containerDisk
    source_name: str | None = None  # PVC/DV name
    size: str | None = None
    storage_class: str | None = None
    bus: str = "virtio"
    boot_order: int | None = None
    is_cloudinit: bool = False


class VMCondition(BaseModel):
    """VM condition."""

    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class GuestOSInfo(BaseModel):
    """Guest OS information from QEMU Guest Agent."""
    id: str | None = None
    name: str | None = None
    pretty_name: str | None = None
    version: str | None = None
    version_id: str | None = None
    kernel_release: str | None = None
    kernel_version: str | None = None
    machine: str | None = None


class GuestAgentInfo(BaseModel):
    """Guest Agent info from VMI status."""
    agent_connected: bool = False
    hostname: str | None = None
    os_info: GuestOSInfo | None = None
    timezone: str | None = None
    timezone_offset: int | None = None
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    filesystem: list[dict[str, Any]] = Field(default_factory=list)
    users: list[dict[str, Any]] = Field(default_factory=list)


class VMResponse(BaseModel):
    """Response model for a VirtualMachine."""

    name: str
    namespace: str
    status: str
    ready: bool
    created: str | None = None

    # Spec info
    cpu_cores: int | None = None
    memory: str | None = None
    run_strategy: str | None = None
    
    # Console settings
    console: VMConsoleConfig = Field(default_factory=VMConsoleConfig)

    # Runtime info (from VMI)
    phase: str | None = None
    ip_address: str | None = None
    node: str | None = None

    # Guest Agent
    guest_agent: GuestAgentInfo | None = None

    # Metadata
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    # RBAC context
    project: str | None = None
    environment: str | None = None
    owner: str | None = None

    # Conditions
    conditions: list[VMCondition] = Field(default_factory=list)

    # Volume info (legacy - just names)
    volumes: list[str] = Field(default_factory=list)
    
    # Detailed disk info
    disks: list[VMDiskInfo] = Field(default_factory=list)


class VMListResponse(BaseModel):
    """Response model for listing VMs."""

    items: list[VMResponse]
    total: int
    page: int = 1
    per_page: int = 50
    pages: int = 1


class VMStatusResponse(BaseModel):
    """Response model for VM action status."""

    name: str
    namespace: str
    action: str
    success: bool
    message: str | None = None


def vm_from_k8s(vm: dict[str, Any], vmi: dict[str, Any] | None, pod_ip: str | None = None) -> VMResponse:
    """Convert Kubernetes VM object to VMResponse."""
    metadata = vm.get("metadata", {})
    spec = vm.get("spec", {})
    status = vm.get("status", {})
    template_spec = spec.get("template", {}).get("spec", {})
    domain = template_spec.get("domain", {})

    # Get CPU/Memory from domain or instance type
    cpu_cores = None
    memory = None

    if "cpu" in domain:
        cpu_cores = domain["cpu"].get("cores")
    if "resources" in domain:
        memory = domain["resources"].get("requests", {}).get("memory")
    
    # Get console settings from devices
    devices = domain.get("devices", {})
    vnc_enabled = devices.get("autoattachGraphicsDevice", True)  # Default true in KubeVirt
    serial_console_enabled = devices.get("autoattachSerialConsole", False)  # Default false
    console_config = VMConsoleConfig(
        vnc_enabled=vnc_enabled,
        serial_console_enabled=serial_console_enabled,
    )

    # Get conditions
    conditions = [
        VMCondition(
            type=c.get("type", ""),
            status=c.get("status", ""),
            reason=c.get("reason"),
            message=c.get("message"),
        )
        for c in status.get("conditions", [])
    ]

    # Get volumes (legacy - just names)
    volumes = [v.get("name", "") for v in template_spec.get("volumes", [])]
    
    # Get detailed disk info
    volume_specs = template_spec.get("volumes", [])
    disk_specs = domain.get("devices", {}).get("disks", [])
    
    # Build a map of disk names to their specs
    disk_spec_map = {d.get("name"): d for d in disk_specs}
    
    disks: list[VMDiskInfo] = []
    for vol in volume_specs:
        vol_name = vol.get("name", "")
        disk_spec = disk_spec_map.get(vol_name, {})
        
        # Determine type and source
        disk_type = "unknown"
        source_name = None
        is_cloudinit = False
        
        if "dataVolume" in vol:
            disk_type = "dataVolume"
            source_name = vol["dataVolume"].get("name")
        elif "persistentVolumeClaim" in vol:
            disk_type = "persistentVolumeClaim"
            source_name = vol["persistentVolumeClaim"].get("claimName")
        elif "cloudInitNoCloud" in vol or "cloudInitConfigDrive" in vol:
            disk_type = "cloudInit"
            is_cloudinit = True
        elif "containerDisk" in vol:
            disk_type = "containerDisk"
            source_name = vol["containerDisk"].get("image")
        
        # Get bus type
        bus = "virtio"
        if "disk" in disk_spec:
            bus = disk_spec["disk"].get("bus", "virtio")
        elif "cdrom" in disk_spec:
            bus = "sata"
        
        # Get boot order
        boot_order = disk_spec.get("bootOrder")
        
        disks.append(VMDiskInfo(
            name=vol_name,
            type=disk_type,
            source_name=source_name,
            bus=bus,
            boot_order=boot_order,
            is_cloudinit=is_cloudinit,
        ))

    # VMI runtime info
    phase = None
    ip_address = None
    node = None
    guest_agent = None

    if vmi:
        vmi_status = vmi.get("status", {})
        phase = vmi_status.get("phase")
        node = vmi_status.get("nodeName")

        # Get IP from interfaces (guest agent) or fall back to pod-level IP (Kube-OVN IPAM)
        interfaces = vmi_status.get("interfaces", [])
        if interfaces:
            ip_address = interfaces[0].get("ipAddress")
        if not ip_address and pod_ip:
            ip_address = pod_ip

        # Guest Agent info
        guest_os_raw = vmi_status.get("guestOSInfo", {})
        agent_conditions = [c for c in vmi_status.get("conditions", []) if c.get("type") == "AgentConnected"]
        agent_connected = any(c.get("status") == "True" for c in agent_conditions)

        if agent_connected or guest_os_raw:
            os_info = None
            if guest_os_raw:
                os_info = GuestOSInfo(
                    id=guest_os_raw.get("id"),
                    name=guest_os_raw.get("name"),
                    pretty_name=guest_os_raw.get("prettyName"),
                    version=guest_os_raw.get("version"),
                    version_id=guest_os_raw.get("versionId"),
                    kernel_release=guest_os_raw.get("kernelRelease"),
                    kernel_version=guest_os_raw.get("kernelVersion"),
                    machine=guest_os_raw.get("machine"),
                )

            ga_interfaces = []
            for iface in interfaces:
                ga_interfaces.append({
                    "name": iface.get("name"),
                    "mac": iface.get("mac"),
                    "ip_address": iface.get("ipAddress"),
                    "ip_addresses": iface.get("ipAddresses", []),
                    "interface_name": iface.get("interfaceName"),
                })

            guest_agent = GuestAgentInfo(
                agent_connected=agent_connected,
                hostname=vmi_status.get("guestOSInfo", {}).get("hostname") or vmi_status.get("nodeName"),
                os_info=os_info,
                interfaces=ga_interfaces,
                filesystem=vmi_status.get("volumeStatus", []),
                users=vmi_status.get("guestOSInfo", {}).get("users", []),
            )

    # RBAC context from namespace labels (injected by list_vms) or VM labels/annotations
    vm_labels = metadata.get("labels", {})
    vm_annotations = metadata.get("annotations", {})
    project = vm_labels.get("kubevirt-ui.io/project") or None
    environment = vm_labels.get("kubevirt-ui.io/environment") or None
    owner = vm_annotations.get("kubevirt-ui.io/owner") or vm_annotations.get("kubevirt-ui.io/created-by") or None

    return VMResponse(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        status=status.get("printableStatus", "Unknown"),
        ready=status.get("ready", False),
        created=metadata.get("creationTimestamp"),
        cpu_cores=cpu_cores,
        memory=memory,
        run_strategy=status.get("runStrategy") or spec.get("runStrategy"),
        console=console_config,
        phase=phase,
        ip_address=ip_address,
        node=node,
        guest_agent=guest_agent,
        labels=vm_labels,
        disks=disks,
        annotations=vm_annotations,
        project=project,
        environment=environment,
        owner=owner,
        conditions=conditions,
        volumes=volumes,
    )

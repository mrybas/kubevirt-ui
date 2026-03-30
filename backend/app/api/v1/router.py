"""API v1 router aggregation."""

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.api.v1.auth import router as auth_router
from app.api.v1.cluster import router as cluster_router
from app.api.v1.users import users_router, groups_router
from app.api.v1.disks import router as disks_router, snapshots_router
from app.api.v1.namespaces import router as namespaces_router
from app.api.v1.network import router as network_router
from app.api.v1.profile import router as profile_router
from app.api.v1.folders import router as folders_router
from app.api.v1.projects import router as projects_router, teams_router
from app.api.v1.storage import router as storage_router
from app.api.v1.templates import router as templates_router, images_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.schedules import router as schedules_router
from app.api.v1.tenants_crud import router as tenants_router
from app.api.v1.vpcs import router as vpcs_router
from app.api.v1.egress_gateway import router as egress_gateway_router
from app.api.v1.ovn_gateway import router as ovn_gateway_router
from app.api.v1.security_groups import router as security_groups_router
from app.api.v1.subnet_acls import router as subnet_acls_router
from app.api.v1.hubble import router as hubble_router
from app.api.v1.cilium_policy import router as cilium_policy_router
from app.api.v1.bgp import router as bgp_router
from app.api.v1.security_baseline import router as security_baseline_router
from app.api.v1.vms import router as vms_router
from app.api.v1.vm_actions import router as vm_actions_router
from app.api.v1.vm_console import router as vm_console_router
from app.api.v1.vm_disks import router as vm_disks_router
from app.api.v1.vm_network import router as vm_network_router
from app.api.v1.velero_backups import router as velero_backups_router
from app.api.v1.vm_snapshots import router as vm_snapshots_router

router = APIRouter()

settings = get_settings()


@router.get("/features", tags=["Features"])
async def get_features():
    return {"enableTenants": settings.enable_tenants}


# Include all v1 routers
router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
router.include_router(metrics_router, prefix="/metrics", tags=["Metrics"])
router.include_router(profile_router, prefix="/profile", tags=["Profile"])
router.include_router(folders_router, prefix="/folders", tags=["Folders"])
router.include_router(projects_router, prefix="/projects", tags=["Projects"])

if settings.enable_tenants:
    router.include_router(tenants_router, prefix="/tenants", tags=["Tenants"])
else:

    _tenants_fallback = APIRouter()

    @_tenants_fallback.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def tenants_disabled(path: str):
        raise HTTPException(status_code=403, detail="Tenants feature is disabled")

    router.include_router(_tenants_fallback, prefix="/tenants", tags=["Tenants"])
router.include_router(teams_router, prefix="/teams", tags=["Teams"])
router.include_router(users_router, prefix="/users", tags=["Users"])
router.include_router(groups_router, prefix="/groups", tags=["Groups"])

# VM Templates and Images
router.include_router(templates_router, prefix="/templates", tags=["VM Templates"])
router.include_router(images_router, prefix="/images", tags=["Images"])

# Network Management (Kube-OVN)
router.include_router(network_router, prefix="/network", tags=["Network"])
router.include_router(vpcs_router, prefix="/vpcs", tags=["VPCs"])
router.include_router(security_groups_router, prefix="/security-groups", tags=["Security Groups"])
router.include_router(egress_gateway_router, prefix="/egress-gateways", tags=["Egress Gateways"])
router.include_router(ovn_gateway_router, prefix="/ovn-gateways", tags=["OVN Gateways"])
router.include_router(subnet_acls_router, prefix="/subnets", tags=["Subnet ACLs"])
router.include_router(hubble_router, prefix="/hubble", tags=["Hubble"])
router.include_router(cilium_policy_router, prefix="/cilium-policies", tags=["Cilium Policies"])
router.include_router(security_baseline_router, prefix="/security-baseline", tags=["Security Baseline"])
router.include_router(bgp_router, prefix="/bgp", tags=["BGP"])

# Cluster-wide VMs endpoint
router.include_router(vms_router, prefix="/vms", tags=["Virtual Machines"])

# Namespaced resources — VM CRUD + sub-modules share the same prefix
router.include_router(vms_router, prefix="/namespaces/{namespace}/vms", tags=["Virtual Machines"])
router.include_router(vm_actions_router, prefix="/namespaces/{namespace}/vms", tags=["VM Actions"])
router.include_router(vm_disks_router, prefix="/namespaces/{namespace}/vms", tags=["VM Disks"])
router.include_router(vm_console_router, prefix="/namespaces/{namespace}/vms", tags=["VM Console"])
router.include_router(vm_snapshots_router, prefix="/namespaces/{namespace}/vms", tags=["VM Snapshots"])
router.include_router(vm_network_router, prefix="/namespaces/{namespace}/vms", tags=["VM Network"])
router.include_router(storage_router, prefix="/namespaces/{namespace}/storage", tags=["Storage"])
router.include_router(disks_router, prefix="/namespaces/{namespace}/disks", tags=["Persistent Disks"])
router.include_router(snapshots_router, prefix="/namespaces/{namespace}/snapshots", tags=["Volume Snapshots"])
router.include_router(schedules_router, prefix="/namespaces/{namespace}/schedules", tags=["Scheduled Actions"])
router.include_router(namespaces_router, prefix="/namespaces", tags=["Namespaces"])
router.include_router(cluster_router, prefix="/cluster", tags=["Cluster"])
router.include_router(velero_backups_router, prefix="/velero", tags=["Velero Backups"])

# Cluster-wide storage classes endpoint
router.include_router(storage_router, prefix="/storage", tags=["Storage"])

"""API v1 router aggregation."""

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.core.auth import require_auth
from app.api.v1.auth import router as auth_router
from app.api.v1.cluster import router as cluster_router
from app.api.v1.users import users_router, groups_router
from app.api.v1.disks import router as disks_router, snapshots_router
from app.api.v1.namespaces import router as namespaces_router
from app.api.v1.network import router as network_router
from app.api.v1.vpc_underlay import router as vpc_underlay_router
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
from app.api.v1.ldap import router as ldap_router

router = APIRouter()

settings = get_settings()


@router.get("/features", tags=["Features"])
async def get_features():
    return {"enableTenants": settings.enable_tenants}


# Everything except /auth sits behind authentication.
#
# Per-route dependencies were the convention here, and 18 routes across
# projects, storage and metrics simply never got one — anonymous callers could
# delete namespaces, delete DataVolumes and run arbitrary PromQL. Worse,
# `create_project` read `request.state.user`, which nothing in the app ever
# sets, so the code *looked* guarded. A router-level dependency is the only
# form that cannot be forgotten by the next endpoint.
#
# With AUTH_TYPE=none `require_auth` resolves to the anonymous admin, so a lab
# deployment behaves exactly as before; this bites only where auth is on.
protected = APIRouter(dependencies=[Depends(require_auth)])

# Include all v1 routers
router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
protected.include_router(metrics_router, prefix="/metrics", tags=["Metrics"])
protected.include_router(profile_router, prefix="/profile", tags=["Profile"])
protected.include_router(folders_router, prefix="/folders", tags=["Folders"])
protected.include_router(projects_router, prefix="/projects", tags=["Projects"])

if settings.enable_tenants:
    protected.include_router(tenants_router, prefix="/tenants", tags=["Tenants"])
else:

    _tenants_fallback = APIRouter()

    @_tenants_fallback.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def tenants_disabled(path: str):
        raise HTTPException(status_code=403, detail="Tenants feature is disabled")

    protected.include_router(_tenants_fallback, prefix="/tenants", tags=["Tenants"])
protected.include_router(teams_router, prefix="/teams", tags=["Teams"])
protected.include_router(users_router, prefix="/users", tags=["Users"])
protected.include_router(groups_router, prefix="/groups", tags=["Groups"])
protected.include_router(ldap_router, prefix="/ldap", tags=["LDAP"])

# VM Templates and Images
protected.include_router(templates_router, prefix="/templates", tags=["VM Templates"])
protected.include_router(images_router, prefix="/images", tags=["Images"])

# Network Management (Kube-OVN)
protected.include_router(network_router, prefix="/network", tags=["Network"])
protected.include_router(vpc_underlay_router, prefix="/network", tags=["Network"])
protected.include_router(vpcs_router, prefix="/vpcs", tags=["VPCs"])
protected.include_router(security_groups_router, prefix="/security-groups", tags=["Security Groups"])
protected.include_router(egress_gateway_router, prefix="/egress-gateways", tags=["Egress Gateways"])
protected.include_router(ovn_gateway_router, prefix="/ovn-gateways", tags=["OVN Gateways"])
protected.include_router(subnet_acls_router, prefix="/subnets", tags=["Subnet ACLs"])
protected.include_router(hubble_router, prefix="/hubble", tags=["Hubble"])
protected.include_router(cilium_policy_router, prefix="/cilium-policies", tags=["Cilium Policies"])
protected.include_router(security_baseline_router, prefix="/security-baseline", tags=["Security Baseline"])
protected.include_router(bgp_router, prefix="/bgp", tags=["BGP"])

# Cluster-wide VMs endpoint
protected.include_router(vms_router, prefix="/vms", tags=["Virtual Machines"])

# Namespaced resources — VM CRUD + sub-modules share the same prefix
protected.include_router(vms_router, prefix="/namespaces/{namespace}/vms", tags=["Virtual Machines"])
protected.include_router(vm_actions_router, prefix="/namespaces/{namespace}/vms", tags=["VM Actions"])
protected.include_router(vm_disks_router, prefix="/namespaces/{namespace}/vms", tags=["VM Disks"])
# The consoles are WebSockets and authenticate themselves, in
# `vm_console._ws_authenticate`, from a `token` query parameter.
#
# They must NOT sit behind the router-level dependency. It resolves through
# `HTTPBearer`, which takes an HTTP Request and is handed a WebSocket instead:
#
#   TypeError: HTTPBearer.__call__() missing 1 required positional argument
#
# — raised before the handler runs, so every console closed with 1006 and the
# UI sat on "Connecting to console..." forever. A browser cannot set an
# Authorization header on a WebSocket at all, which is why the token travels
# in the query string; there is no header for `HTTPBearer` to have read.
#
# Exempt from the *dependency*, not from authentication — see
# `test_route_auth_contract.py`, which walks the WebSocket routes separately
# and fails if one of them stops authenticating.
router.include_router(vm_console_router, prefix="/namespaces/{namespace}/vms", tags=["VM Console"])
protected.include_router(vm_snapshots_router, prefix="/namespaces/{namespace}/vms", tags=["VM Snapshots"])
protected.include_router(vm_network_router, prefix="/namespaces/{namespace}/vms", tags=["VM Network"])
protected.include_router(storage_router, prefix="/namespaces/{namespace}/storage", tags=["Storage"])
protected.include_router(disks_router, prefix="/namespaces/{namespace}/disks", tags=["Persistent Disks"])
protected.include_router(snapshots_router, prefix="/namespaces/{namespace}/snapshots", tags=["Volume Snapshots"])
protected.include_router(schedules_router, prefix="/namespaces/{namespace}/schedules", tags=["Scheduled Actions"])
protected.include_router(namespaces_router, prefix="/namespaces", tags=["Namespaces"])
protected.include_router(cluster_router, prefix="/cluster", tags=["Cluster"])
protected.include_router(velero_backups_router, prefix="/velero", tags=["Velero Backups"])

# Cluster-wide storage classes endpoint
protected.include_router(storage_router, prefix="/storage", tags=["Storage"])

router.include_router(protected)

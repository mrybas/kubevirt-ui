"""Tenant management — decomposed into submodules.

This file is a backwards-compatibility shim. All code has been moved to:
  - tenants_common.py  (constants, helpers)
  - tenants_vpc.py     (VPC lifecycle)
  - tenants_capi.py    (CAPI resource builders)
  - tenants_addons.py  (Flux HelmRelease / addon management)
  - tenants_crud.py    (router + REST endpoints)
"""

from app.api.v1.tenants_crud import router  # noqa: F401

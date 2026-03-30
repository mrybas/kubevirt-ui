"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.v1.router import router as api_v1_router
from app.config import get_settings
from app.core.k8s_client import K8sClient

# Configure logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    logger.info("Starting KubeVirt UI Backend...")

    # Initialize Kubernetes client
    k8s_client = K8sClient()
    await k8s_client.initialize()
    app.state.k8s_client = k8s_client

    # Ensure system namespace exists (needed for SA tokens, settings, templates)
    SYSTEM_NAMESPACE = "kubevirt-ui-system"
    try:
        await k8s_client.core_api.read_namespace(SYSTEM_NAMESPACE)
        logger.info(f"Namespace {SYSTEM_NAMESPACE} exists")
    except Exception:
        from kubernetes_asyncio.client import V1Namespace, V1ObjectMeta
        await k8s_client.core_api.create_namespace(
            V1Namespace(metadata=V1ObjectMeta(
                name=SYSTEM_NAMESPACE,
                labels={"kubevirt-ui.io/managed": "true"},
            ))
        )
        logger.info(f"Created namespace {SYSTEM_NAMESPACE}")

    # Seed LLDAP with default groups/admin if enabled
    from app.core.lldap_client import LLDAP_ENABLED, get_lldap_client
    if LLDAP_ENABLED:
        try:
            lldap = get_lldap_client()
            await lldap.seed_initial_data()
        except Exception as e:
            logger.warning(f"LLDAP seeding failed (will retry on first request): {e}")

    # Start tenant addon reconciler (background task) — only if tenants enabled
    from app.config import get_settings
    reconciler_task = None
    if get_settings().enable_tenants:
        from app.core.tenant_reconciler import reconcile_loop
        reconciler_task = asyncio.create_task(reconcile_loop(k8s_client))
    else:
        logger.info("Tenants feature disabled, skipping tenant reconciler")

    logger.info("KubeVirt UI Backend started successfully")
    yield

    # Cleanup
    logger.info("Shutting down KubeVirt UI Backend...")
    if reconciler_task:
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass
    await k8s_client.close()
    logger.info("KubeVirt UI Backend shut down")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # Log validation errors (field names only — never log request body, it may contain secrets)
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: FastAPIRequest, exc: RequestValidationError):
        failed_fields = [".".join(str(l) for l in e.get("loc", ())) for e in exc.errors()]
        logger.warning(f"Validation error on {request.method} {request.url.path}: fields={failed_fields}")
        # Serialize errors safely — ctx may contain non-JSON-serializable objects like ValueError
        safe_errors = []
        for err in exc.errors():
            safe_err = {k: (str(v) if k == "ctx" else v) for k, v in err.items()}
            safe_errors.append(safe_err)
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    # CORS middleware — wildcard origin with credentials is a browser security violation.
    # When origins=["*"], disable credentials so browsers reject credentialed requests.
    origins = settings.cors_origins_list
    allow_creds = "*" not in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_creds,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(health_router)
    app.include_router(api_v1_router, prefix=settings.api_prefix)

    return app


app = create_app()

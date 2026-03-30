"""Health check endpoints."""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_class=JSONResponse)
async def health() -> dict[str, str]:
    """Liveness probe endpoint."""
    return {"status": "ok"}


@router.get("/health/ready", response_class=JSONResponse)
async def ready(request: Request) -> JSONResponse:
    """Readiness probe endpoint - checks Kubernetes API connectivity."""
    k8s_client = request.app.state.k8s_client

    try:
        is_connected = await k8s_client.check_connectivity()
        if is_connected:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "ready", "kubernetes": "connected"},
            )
        else:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not ready", "kubernetes": "disconnected"},
            )
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not ready", "error": str(e)},
        )

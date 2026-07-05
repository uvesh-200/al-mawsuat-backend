import json
import logging
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("api")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        tenant_id = None
        if hasattr(request.state, "user") and request.state.user:
            tenant_id = getattr(request.state.user, "tenant_id", None)
        if not tenant_id:
            tenant_id = request.headers.get("X-Tenant-ID", "unknown")

        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "level": "INFO" if response.status_code < 500 else "ERROR",
            "endpoint": str(request.url.path),
            "method": request.method,
            "tenant_id": tenant_id,
            "duration_ms": duration_ms,
            "status_code": response.status_code,
        }
        logger.info(json.dumps(record))
        return response

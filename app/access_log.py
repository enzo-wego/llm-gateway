"""Replacement for the server access log: never include the query string."""

import logging
import time

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint

# Keep request lines out of the application logger's handlers.
log = logging.getLogger("gw-access")


async def path_access_log(request: Request, call_next: RequestResponseEndpoint) -> Response:
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        log.info("%s %s %s %dms", request.method, request.url.path, status,
                 int((time.monotonic() - started) * 1000))

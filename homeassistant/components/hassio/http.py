"""HTTP Support for Hass.io."""
from __future__ import annotations

import logging
import os
import re

import aiohttp
from aiohttp import web
from aiohttp.client import ClientError, ClientTimeout
from aiohttp.hdrs import CONTENT_TYPE
from aiohttp.web_exceptions import HTTPBadGateway

from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.components.onboarding import async_is_onboarded
from homeassistant.const import HTTP_UNAUTHORIZED

from .const import X_HASS_IS_ADMIN, X_HASS_USER_ID, X_HASSIO

_LOGGER = logging.getLogger(__name__)

NO_TIMEOUT = re.compile(
    r"^(?:"
    r"|homeassistant/update"
    r"|hassos/update"
    r"|hassos/update/cli"
    r"|supervisor/update"
    r"|addons/[^/]+/(?:update|install|rebuild)"
    r"|backups/.+/full"
    r"|backups/.+/partial"
    r"|backups/[^/]+/(?:upload|download)"
    r"|snapshots/.+/full"
    r"|snapshots/.+/partial"
    r"|snapshots/[^/]+/(?:upload|download)"
    r")$"
)

NO_AUTH_ONBOARDING = re.compile(
    r"^(?:" r"|supervisor/logs" r"|backups/[^/]+/.+" r"|snapshots/[^/]+/.+" r")$"
)

NO_AUTH = re.compile(
    r"^(?:" r"|app/.*" r"|addons/[^/]+/logo" r"|addons/[^/]+/icon" r")$"
)


class HassIOView(HomeAssistantView):
    """Hass.io view to handle base part."""

    name = "api:hassio"
    url = "/api/hassio/{path:.+}"
    requires_auth = False

    def __init__(self, host: str, websession: aiohttp.ClientSession) -> None:
        """Initialize a Hass.io base view."""
        self._host = host
        self._websession = websession

    async def _handle(
        self, request: web.Request, path: str
    ) -> web.Response | web.StreamResponse:
        """Route data to Hass.io."""
        hass = request.app["hass"]
        if _need_auth(hass, path) and not request[KEY_AUTHENTICATED]:
            return web.Response(status=HTTP_UNAUTHORIZED)

        return await self._command_proxy(path, request)

    delete = _handle
    get = _handle
    post = _handle

    async def _command_proxy(
        self, path: str, request: web.Request
    ) -> web.StreamResponse:
        """Return a client request with proxy origin for Hass.io supervisor.

        This method is a coroutine.
        """
        headers = _init_header(request)
        if path in ("snapshots/new/upload", "backups/new/upload"):
            # We need to reuse the full content type that includes the boundary
            headers[
                "Content-Type"
            ] = request._stored_content_type  # pylint: disable=protected-access
        try:
            # Stream the request to the supervisor
            client = await self._websession.request(
                method=request.method,
                url=f"http://{self._host}/{path}",
                headers=headers,
                data=request.content,
                timeout=_get_timeout(path),
            )

            # Stream the supervisor response back
            response = web.StreamResponse(status=client.status, headers=client.headers)
            response.content_type = client.content_type

            await response.prepare(request)
            async for data in client.content.iter_chunked(4096):
                await response.write(data)

            return response

        except ClientError as err:
            _LOGGER.error("Client error on api %s request %s", path, err)

        raise HTTPBadGateway()


def _init_header(request: web.Request) -> dict[str, str]:
    """Create initial header."""
    headers = {
        X_HASSIO: os.environ.get("HASSIO_TOKEN", ""),
        CONTENT_TYPE: request.content_type,
    }

    # Add user data
    user = request.get("hass_user")
    if user is not None:
        headers[X_HASS_USER_ID] = request["hass_user"].id
        headers[X_HASS_IS_ADMIN] = str(int(request["hass_user"].is_admin))

    return headers


def _get_timeout(path: str) -> ClientTimeout:
    """Return timeout for a URL path."""
    if NO_TIMEOUT.match(path):
        return ClientTimeout(connect=10)
    return ClientTimeout(connect=10, total=300)


def _need_auth(hass, path: str) -> bool:
    """Return if a path need authentication."""
    if not async_is_onboarded(hass) and NO_AUTH_ONBOARDING.match(path):
        return False
    if NO_AUTH.match(path):
        return False
    return True

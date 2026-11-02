"""MCP bridge — exposes the danger_zone agent API to OpenCode / MCP clients.

The FastAPI app in this repo is a plain REST service. OpenCode (and most
agentic clients) speak MCP, so this module wraps the REST surface in a small
Streamable-HTTP MCP server. It is a *client* of the local API, not a second
implementation: every tool is a thin HTTP call, so behaviour stays identical.

Run it (after ``uv sync --extra mcp``)::

    python -m app.mcp_server

Environment:
    DZ_API_URL    base URL of the danger_zone API (default http://127.0.0.1:8001)
    DZ_API_KEY    X-API-Key sent to the API *and* required from MCP clients
    DZ_MCP_HOST   bind address (default 127.0.0.1)
    DZ_MCP_PORT   bind port (default 8010)
    DZ_PUBLIC_URL public base URL used in snapshot links (default DZ_API_URL)

Exposed endpoint: ``POST /mcp`` (Streamable HTTP).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.config import get_settings

API_URL = os.getenv("DZ_API_URL", "http://127.0.0.1:8001").rstrip("/")
PUBLIC_URL = os.getenv("DZ_PUBLIC_URL", API_URL).rstrip("/")
API_KEY = os.getenv("DZ_API_KEY") or get_settings().api_key
MCP_HOST = os.getenv("DZ_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("DZ_MCP_PORT", "8010"))

server = MCPServer(
    name="danger-zone",
    version="0.1.0",
    instructions=(
        "Tools for the danger_zone safety system running on this machine. "
        "Use list_incidents to see detections, get_agent_modes/set_agent_mode "
        "to control the VLM agents, and set_mission to change in plain English "
        "what PPE the compliance agent checks for. Cameras can be Meraki, a "
        "looped video file, an RTSP stream, or a synthetic scene."
    ),
)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API_URL,
            timeout=httpx.Timeout(120.0, connect=5.0),
            headers={"X-API-Key": API_KEY},
        )
    return _client


async def _get(path: str, **params: Any) -> Any:
    cleaned = {k: v for k, v in params.items() if v is not None}
    response = await _http().get(path, params=cleaned)
    response.raise_for_status()
    return response.json()


async def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    response = await _http().post(path, json=body or {})
    response.raise_for_status()
    return response.json()


async def _delete(path: str) -> Any:
    response = await _http().delete(path)
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {"status": "ok"}
    return response.json()


def _fail(exc: Exception) -> dict[str, str]:
    if isinstance(exc, httpx.HTTPStatusError):
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"}
    return {"error": str(exc)}


# ---- incidents -------------------------------------------------------------


@server.tool()
async def list_incidents(
    limit: int = 20,
    incident_type: str | None = None,
    status: str | None = None,
    camera_id: str | None = None,
) -> Any:
    """List recent incidents, newest first.

    Args:
        limit: Maximum rows to return (1-200).
        incident_type: Filter by type, e.g. PPE_VIOLATION, PPE_OK, INTRUSION.
        status: Filter by status, e.g. OPEN, ACKNOWLEDGED, RESOLVED.
        camera_id: Filter by camera serial.
    """
    try:
        return await _get(
            "/incidents/",
            limit=limit,
            type=incident_type,
            status=status,
            camera_id=camera_id,
        )
    except Exception as exc:  # noqa: BLE001 - surface as tool error text
        return _fail(exc)


@server.tool()
async def get_incident(incident_id: int) -> Any:
    """Get one incident, including the VLM summary and structured reasoning."""
    try:
        return await _get(f"/incidents/{incident_id}")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def get_incident_snapshot_url(incident_id: int) -> dict[str, str]:
    """Return the URL of the JPEG snapshot attached to an incident."""
    return {"url": f"{PUBLIC_URL}/incidents/{incident_id}/snapshot"}


@server.tool()
async def get_agent_traces(limit: int = 20, agent: str | None = None) -> Any:
    """Recent agent reasoning traces (what the agent thought and why).

    Args:
        limit: Maximum rows to return.
        agent: Filter by agent name, e.g. ppe_compliance, zone_intrusion.
    """
    try:
        return await _get("/agents/traces", limit=limit, agent=agent)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---- agents ----------------------------------------------------------------


@server.tool()
async def get_agent_modes() -> Any:
    """Get active/passive mode for the gated VLM agents."""
    try:
        return await _get("/agents/modes")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def set_agent_mode(agent: str, mode: str) -> Any:
    """Turn a VLM agent on (active) or off (passive).

    Args:
        agent: zone_intrusion or ppe_compliance.
        mode: "active" or "passive".
    """
    if mode not in {"active", "passive"}:
        return {"error": "mode must be 'active' or 'passive'"}
    try:
        return await _post(f"/agents/{agent}/mode", {"mode": mode})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def invoke_agent(agent: str, payload: dict[str, Any] | None = None) -> Any:
    """Manually trigger an agent by publishing an invoke event.

    Args:
        agent: zone_intrusion, ppe_compliance, rogue_device, heartbeat or demo.
        payload: Optional event payload (camera serial, path, ...).
    """
    try:
        return await _post(f"/agents/{agent}/invoke", {"payload": payload or {}})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---- mission context -------------------------------------------------------


@server.tool()
async def get_mission() -> Any:
    """Get the active natural-language mission context, if any."""
    try:
        return await _get("/agents/context")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def set_mission(mission: str) -> Any:
    """Set the PPE mission in plain English; the VLM parses it once.

    Example: "this is an electronics lab — workers need safety glasses and
    anti-static wrist straps". Subsequent snapshots are checked against the
    parsed item list until the mission is cleared.
    """
    try:
        return await _post("/agents/context", {"mission": mission})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def clear_mission() -> Any:
    """Clear the mission context; the PPE agent reverts to cameras.yaml."""
    try:
        return await _delete("/agents/context")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---- cameras / ingest ------------------------------------------------------


@server.tool()
async def list_cameras() -> Any:
    """List configured cameras (Meraki and ingest-backed)."""
    try:
        return await _get("/cameras/")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def get_ingest_status() -> Any:
    """Report running ingest sources, buffer depth and last frame sequence."""
    try:
        return await _get("/cameras/ingest")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def get_zones(serial: str) -> Any:
    """Get the exclusion / PPE polygons for a camera (normalised 0..1)."""
    try:
        return await _get(f"/cameras/{serial}/zones")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def capture_snapshot(serial: str) -> Any:
    """Force a snapshot now and let the agents analyse it.

    Works for ingest-backed cameras (newest buffered frame) and Meraki.
    """
    try:
        return await _post(f"/cameras/{serial}/snapshot")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@server.tool()
async def get_latest_snapshot_url(serial: str) -> dict[str, str]:
    """Return the URL of the newest snapshot for a camera."""
    return {"url": f"{PUBLIC_URL}/cameras/{serial}/snapshot/latest"}


# ---- transport -------------------------------------------------------------


class ApiKeyMiddleware:
    """Pure-ASGI header check; safe for the streaming MCP response."""

    def __init__(self, app: Any, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.api_key:
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"x-api-key", b"").decode()
            if not provided:
                auth = headers.get(b"authorization", b"").decode()
                if auth.lower().startswith("bearer "):
                    provided = auth[7:]
            if not hmac.compare_digest(provided, self.api_key):
                response = JSONResponse({"detail": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app() -> Any:
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        host=MCP_HOST,
        # Tailnet traffic is already WireGuard-encrypted and we check an API
        # key, so Host-header rebinding protection would only add fragility
        # when fronted by `tailscale serve` (different Host name).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.add_middleware(ApiKeyMiddleware, api_key=API_KEY)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()

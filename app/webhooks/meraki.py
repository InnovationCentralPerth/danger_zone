from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.runtime.bus import bus

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Meraki may send "sha256=<hex>" or just "<hex>"; use removeprefix (not lstrip).
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


@router.post("/meraki", summary="Receive Meraki webhook events")
async def receive_meraki_webhook(
    request: Request,
    x_cisco_meraki_signature: str = Header(default=""),
) -> dict[str, str]:
    settings = get_settings()
    body = await request.body()

    if settings.meraki_webhook_secret and not _verify_signature(
        body, x_cisco_meraki_signature, settings.meraki_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON") from e

    alert_type = payload.get("alertType", "unknown")
    logger.info("Meraki webhook received: %s", alert_type)

    # Route to appropriate topic on the internal bus
    topic_map = {
        "motion_alert": "camera.motion",
        "settings_changed": "network.settings",
        "rogue_ap_detected": "network.rogue_ap",
    }
    # Client connectivity events
    if "client" in alert_type.lower() or "connectivity" in alert_type.lower():
        await bus.publish("network.client", payload)
    else:
        topic = topic_map.get(alert_type, f"meraki.{alert_type}")
        await bus.publish(topic, payload)

    return {"status": "accepted"}

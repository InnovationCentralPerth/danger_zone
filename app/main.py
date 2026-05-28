from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel

from app.agents.demo import fire_demo_incident, heartbeat_status
from app.config import get_settings
from app.models.db import AgentTrace, Device, Incident, Setting  # noqa: F401 — register tables
from app.runtime.scheduler import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _init_db() -> None:
    from sqlmodel import create_engine

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.snapshots_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{settings.db_path}", echo=settings.debug)
    SQLModel.metadata.create_all(engine)
    # Store engine on app state via a module-level singleton
    from app import db as db_module

    db_module._engine = engine


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    logger.info("Starting danger_zone…")
    _init_db()

    settings = get_settings()

    # Verify Meraki API key is read-only at the org level — hard fail if not.
    # Skipped when MERAKI_API_KEY is empty (e.g. local UI-only development).
    if settings.meraki_api_key and settings.meraki_org_id:
        from app.integrations.meraki import MerakiError, WriteScopedKeyError, get_client

        try:
            await get_client().assert_read_only()
        except WriteScopedKeyError as e:
            if settings.meraki_allow_write_key:
                logger.warning("MERAKI_ALLOW_WRITE_KEY=true — bypassing safety check: %s", e)
            else:
                logger.error("Meraki key check failed: %s", e)
                raise
        except MerakiError as e:
            logger.error("Meraki key check failed: %s", e)
            raise
    else:
        logger.warning(
            "MERAKI_API_KEY / MERAKI_ORG_ID not set — Meraki integration disabled. "
            "Webhooks and snapshot fetches will not work."
        )

    # Wire real webhook handlers to the bus
    from app.agents.meraki_handlers import start_subscribers

    start_subscribers()

    # Start the zone intrusion agent (subscribes to camera.snapshot)
    from app.agents import zone_intrusion

    zone_intrusion.start()

    # Schedule demo agents (milestone 1 — replace with real agents later)
    if settings.debug:
        scheduler.register(15, fire_demo_incident)
    scheduler.register(60, heartbeat_status)
    await scheduler.start()

    yield

    scheduler.stop()
    logger.info("danger_zone stopped.")


app = FastAPI(
    title="danger_zone",
    description="Agentic AI security demonstrator — Meraki cameras + network",
    version="0.1.0",
    lifespan=lifespan,
)

# Routers (imported here to avoid circular imports)
from app.api.agents import router as agents_router  # noqa: E402
from app.api.cameras import router as cameras_router  # noqa: E402
from app.api.incidents import router as incidents_router  # noqa: E402
from app.api.models import router as models_router  # noqa: E402
from app.api.network import router as network_router  # noqa: E402
from app.dashboard.routes import router as dashboard_router  # noqa: E402
from app.webhooks.meraki import router as webhook_router  # noqa: E402

app.include_router(incidents_router, prefix="/incidents", tags=["incidents"])
app.include_router(agents_router, prefix="/agents", tags=["agents"])
app.include_router(network_router, prefix="/network", tags=["network"])
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"])
app.include_router(models_router, prefix="/models", tags=["models"])
app.include_router(dashboard_router, tags=["dashboard"])
app.include_router(webhook_router, prefix="/webhooks", tags=["webhooks"])

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    return {"status": "ok"}

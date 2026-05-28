# AGENTS.md

## Project
`danger_zone` — Agentic AI security demonstrator using Meraki cameras and network. FastAPI backend, HTMX dashboard, SQLite persistence. Proactive (event-triggered) agents detect zone intrusions, PPE violations, and rogue network devices.

## Language / Runtime
- Python 3.12 (pinned via `.python-version` and `requires-python = ">=3.12,<3.13"`; do not use 3.13/3.14 — some deps lack wheels or behave differently)
- `uv` for env and lockfile management

## Key Commands

| Task | Command |
|------|---------|
| First-time setup | `uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e ".[dev]"` |
| Run dev server | `DEBUG=true uvicorn app.main:app --reload --port 8001` |
| Run tests | `pytest -x` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Typecheck | `mypy app` |
| Docker (build+run) | `docker compose -f docker/compose.yaml up --build` |

## Architecture

```
app/
  main.py            — FastAPI app; lifespan starts scheduler and agents
  config.py          — pydantic-settings; reads .env
  db.py              — SQLite engine singleton (set during lifespan)
  api/               — REST routers: incidents, agents, cameras, network
  dashboard/         — HTMX + Jinja2 templates + SSE endpoint (/events)
  agents/            — One module per agent; demo.py fires synthetic events,
                       meraki_handlers.py subscribes to camera/network bus events
  integrations/      — meraki.py (real, milestone 2), vlm.py (stub until M3)
  services/          — config_loader.py reads config/*.yaml
  models/db.py       — SQLModel tables: Incident, AgentTrace, Device, Setting
  runtime/
    bus.py           — In-process pub/sub (asyncio.Queue); singleton `bus`
    scheduler.py     — Fixed-interval task runner; singleton `scheduler`
    trace.py         — Decorator for recording AgentTrace rows
  webhooks/meraki.py — HMAC-verified webhook receiver; dispatches onto bus
config/
  cameras.yaml       — Single camera serial, exclusion polygon, PPE area polygon
  whitelist.yaml     — MAC whitelist + blacklist
  models.yaml        — VLM model catalogue with vision flag
```

## Event bus topics
| Topic | Published by | Consumed by |
|---|---|---|
| `incident.new` | agents | dashboard SSE, DB writer |
| `heartbeat` | heartbeat agent | dashboard SSE |
| `agent.trace` | `AgentRun` ctx manager | dashboard SSE, DB writer |
| `camera.motion` | meraki webhook | (logged; snapshot pipeline handles intrusion) |
| `camera.snapshot` | `/cameras/{serial}/snapshot` + motion handler | zone_intrusion agent |
| `network.client` | meraki webhook | rogue_device agent |
| `agent.invoke.*` | POST /agents/{name}/invoke | named agent |

## VLM / LLM Provider
- GitHub Models API (OpenAI-compatible): `https://models.github.ai/inference`
- Auth: `GITHUB_TOKEN` (PAT) as Bearer token
- Default model: `openai/gpt-4o-mini` (vision-capable)
- Only models with `vision: true` in `config/models.yaml` can process snapshots
- Provider abstraction lives in `app/integrations/vlm.py` — `GitHubModelsProvider.analyse_image()` sends image as base64 data URL and forces `response_format=json_object`
- Active model switchable at runtime via `POST /models/active` or the dashboard dropdown

## External Agent API
- OpenAPI at `/docs`; all routes are auth-guarded via `X-API-Key` header
- Key groups: `incidents`, `agents`, `cameras`, `network`, `models`, `ops`
- `POST /agents/{name}/invoke` to trigger any named agent on demand

## Webhook setup (Meraki → app)
- Endpoint: `POST /webhooks/meraki`
- Signature verification: HMAC-SHA256; set `MERAKI_WEBHOOK_SECRET` to the Meraki shared secret
- Header may be `sha256=<hex>` or just `<hex>` — use `removeprefix` not `lstrip`
- For local dev: `tailscale funnel 8001` to expose a public HTTPS URL

## Required env vars (see `.env.example`)
- `MERAKI_API_KEY`, `MERAKI_ORG_ID`, `MERAKI_WEBHOOK_SECRET`
- `MERAKI_ALLOW_WRITE_KEY=true` to bypass the read-only-key safety check (dev only)
- `GITHUB_TOKEN`, `MODEL_ID` (default: `openai/gpt-4o-mini`)
- `API_KEY` (for external agent auth)
- `DEBUG=true` enables demo agent (fires synthetic incidents every 15s)

## Config files
- `config/cameras.yaml` — **gitignored**; copy from `config/cameras.example.yaml`. Camera serial, network_id, exclusion zone polygon (normalised 0–1 coords), PPE area polygon, cooldown_seconds. Loader falls back to the `.example.yaml` if the real file is missing.
- `config/whitelist.yaml` — **gitignored**; copy from `config/whitelist.example.yaml`. MAC entries with label/vendor; separate `blacklist` section.
- `config/models.yaml` — checked in; model catalogue, `default` key sets startup default.

## Meraki integration notes
- API rate limit: 10 req/s per org; all calls need backoff + retry
- Snapshot URLs from MV API are short-lived (minutes); fetch and persist to `data/snapshots/` immediately on incident creation
- Meraki API key must be **read-only** at org level; app should assert this on startup (milestone 2)
- Network client events arrive via webhooks (`alertType` containing "client" or "connectivity")

## Milestones / Branch Workflow
Each milestone is built on its own branch, PR'd into `main` on completion:
1. `feat/skeleton` — FastAPI + bus + scheduler + demo agent + HTMX dashboard (done)
2. `feat/meraki-client` — Meraki SDK client, snapshot fetch+persist, camera tile, webhook→bus wiring (done)
3. `feat/vlm-zone-intrusion` — VLM adapter + Agent A (zone intrusion) (done)
4. `feat/ppe-compliance` — Agent B (PPE / hard hat) (done)
5. `feat/rogue-device` — Agent C + whitelist UI
6. `feat/polish` — heartbeat narrator, external-agent docs page, face-blur option

## Testing
- `pytest -x` — fast fail on first error
- Meraki and VLM calls should use `pytest-httpx` record-replay fixtures; do not hit live APIs in tests
- `asyncio_mode = "auto"` is set; no need for `@pytest.mark.asyncio` on test functions

## Non-obvious gotchas
- `app/db.py::_engine` is `None` until lifespan runs; tests that need DB must call `_init_db()` or use a fixture that does
- `bus` is a module-level singleton; tests that publish events must import the same singleton, not instantiate a new `EventBus`
- Normalised polygon coords (0–1) in `cameras.yaml` are relative to the **snapshot resolution**, not the camera's native resolution
- GitHub Models vision: image must be sent as base64 `data:image/jpeg;base64,...` in the `image_url` message part

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
| `camera.snapshot` | `/cameras/{serial}/snapshot` + motion handler + auto-capture loop | zone_intrusion agent, ppe_compliance agent |
| `network.client` | meraki webhook | rogue_device agent |
| `agent.invoke.*` | POST /agents/{name}/invoke | named agent |
| `auto_capture.tick` | auto-capture loop | dashboard SSE (status dot) |

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

## Agent active/passive mode
- VLM-backed agents (`zone_intrusion`, `ppe_compliance`) honour a per-agent mode flag persisted in the `Setting` table
- **Default on first boot is `passive`** — no VLM calls are made until you flip them. Prevents accidental token burn
- API: `GET /agents/modes`, `GET/POST /agents/{name}/mode` (body: `{"mode": "active"|"passive"}`)
- Dashboard has clickable pills in the nav; green = active, grey = passive
- Lives in `app/services/agent_mode.py`; gated set is `GATED_AGENTS`

## PPE compliance config
- `cameras.yaml` → `ppe_area.required_item` controls what Agent B checks for
- Currently supported: `"hard_hat"` (default) and `"hi_vis_vest"`
- Add more by extending `_ITEMS` in `app/agents/ppe_compliance.py` (label, missing-count JSON key, definition string for the prompt)
- **Three-way verdict** in `app/agents/ppe_compliance.py::run`:
  - `violation && conf>=0.5 && missing>=1` → `PPE_VIOLATION` (HIGH if conf>=0.85 else MEDIUM)
  - `in_area>=1 && missing==0 && conf>=0.5` → `PPE_OK` (INFO) compliance note
  - otherwise (nobody in area, or low confidence) → silent (trace only, no incident)
- INFO incidents do NOT bump the dashboard's red live-incident counter; rendered as muted grey cards. `NON_ALERT_SEVERITIES = {'INFO'}` in `static/js/main.js`.

## Dashboard zone overlay
- Endpoint `GET /cameras/{serial}/zones` returns `{serial, zones: [{id, label, polygon, colour, kind, required_item?}]}`. 404 on unknown serial. Polygons are normalised 0..1 coords; loader falls back to `config/cameras.example.yaml` if real config missing.
- `static/js/main.js::loadZones()` builds SVG polygons inside `#zone-overlay` (viewBox 0 0 100 100, coords * 100) and renders pills in `#zone-legend`. Overlay revealed only after a snapshot loads.
- Exclusion zone = red `#ff3c3c` (`kind: "intrusion"`); PPE area = yellow `#ffd200` (`kind: "ppe"`).

## Auto-capture loop
- `app/services/auto_capture.py` owns one asyncio.Task per camera that polls `fetch_and_persist_snapshot()` on an interval, then publishes `camera.snapshot` with `trigger: "auto"` so the existing agents pick it up. Replaces the need to click *Capture now* — the demo feels agentic without human intervention.
- State persisted in `Setting` table: `auto_capture.<serial>.enabled` (`"true"`/`"false"`), `auto_capture.<serial>.interval_seconds`.
- API (all under `/cameras`):
  - `GET /auto-capture` — list state for every configured camera
  - `GET /{serial}/auto-capture` — single-camera state (404 unknown serial)
  - `POST /{serial}/auto-capture` body `{"enabled": bool, "interval_seconds"?: int}` — idempotent; 422 if interval < `MIN_INTERVAL_SECONDS` (2)
- Defaults: `DEFAULT_INTERVAL_SECONDS = 5`, `MIN_INTERVAL_SECONDS = 2`, `MAX_BACKOFF_SECONDS = 60`, `MAX_RUNTIME_SECONDS = 30*60` (auto-shutoff guard).
- Safety rails:
  - Default off on first boot.
  - **Idle skip**: if every gated agent (`zone_intrusion`, `ppe_compliance`) is passive, the loop publishes `auto_capture.tick` with `status="idle"` and skips the Meraki fetch — no quota burned for snapshots nobody analyses.
  - **Back-off**: consecutive `MerakiError` doubles the sleep up to 60s; resets on first success.
  - **Auto-shutoff** at 30 min runtime — persists `enabled=false`, publishes `status="auto-shutoff"`, exits.
- Lifespan: `auto_capture.restore_on_startup()` re-spawns loops for serials persisted as enabled (interval/clock reset).
- Dashboard pill in the camera tile header — coloured dot + interval input + Start/Stop button. Pulses green on each successful tick; turns red on error, yellow on auto-shutoff.

## Webhook setup (Meraki → app)
- Endpoint: `POST /webhooks/meraki`
- Signature verification: HMAC-SHA256; set `MERAKI_WEBHOOK_SECRET` to the Meraki shared secret
- Header may be `sha256=<hex>` or just `<hex>` — use `removeprefix` not `lstrip`
- For local dev: `tailscale funnel 8001` to expose a public HTTPS URL

## Required env vars (see `.env.example`)
- `MERAKI_API_KEY`, `MERAKI_ORG_ID`, `MERAKI_WEBHOOK_SECRET`
- `MERAKI_ALLOW_WRITE_KEY=true` to bypass the read-only-key safety check (dev only)
- `GITHUB_TOKEN`, `MODEL_ID` (default: `openai/gpt-4o-mini`)
  - Get a token via the device flow helper: `python scripts/gh_device_login.py` — runs the standard GitHub OAuth 2.0 device flow, prints a user code, polls for the access token, and writes it into `.env`. Default client_id is the public gh CLI one; override with `--client-id` or `GITHUB_OAUTH_CLIENT_ID` env var. Restart the server afterwards.
- `API_KEY` (for external agent auth)
- `DEBUG=true` enables verbose logging / FastAPI debug
- `DEMO_AGENT=true` runs the synthetic demo agent (fires fake incidents every 15s). Decoupled from `DEBUG` so you can run verbose logs against the real camera without synthetic noise.

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

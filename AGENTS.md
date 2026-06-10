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
| Run dev server | `DEBUG=true uvicorn app.main:app --reload --reload-dir app --reload-dir static --reload-dir config --port 8001` |
| Run tests | `pytest -x` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Typecheck | `mypy app` |
| Docker — prod (detached) | `make up` (or `docker compose -f docker/compose.yaml up -d`) |
| Docker — dev (live reload) | `make up-dev` (bind-mounts source; `--reload` watches `app/`, `static/`, `config/`) |
| Docker — logs / shell / down | `make logs` / `make shell` / `make down` |
| Docker — tests inside container | `make test` |
| Makefile help | `make help` |

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
  cameras.yaml       — Optional per-camera overrides (zone polygons, cooldown). Cameras are discovered live from Meraki; YAML entries override the full-frame defaults.
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
| `camera.snapshot` | `/cameras/{serial}/snapshot` + motion handler + analytics watcher | zone_intrusion agent, ppe_compliance agent |
| `network.client` | meraki webhook | rogue_device agent |
| `agent.invoke.*` | POST /agents/{name}/invoke | named agent |
| `analytics_watcher.tick` | analytics watcher loop | dashboard SSE (status dot) |
| `mission_context.changed` | `set_context` / `clear_context` | dashboard SSE (live-refresh mission card) |

## VLM / LLM Provider
- GitHub Models API (OpenAI-compatible): `https://models.github.ai/inference`
- Auth: `GITHUB_TOKEN` (PAT) as Bearer token
- Default model: `openai/gpt-4o-mini` (vision-capable)
- Only models with `vision: true` in `config/models.yaml` can process snapshots
- Provider abstraction lives in `app/integrations/vlm.py` — `GitHubModelsProvider.analyse_image()` sends image as base64 data URL and forces `response_format=json_object`. `GitHubModelsProvider.complete()` is the text-only sibling used by the mission-context parser (system + user prompt, optional `json_response`).
- Active model switchable at runtime via `POST /models/active` or the dashboard dropdown

## External Agent API
- OpenAPI at `/docs`; all routes are auth-guarded via `X-API-Key` header
- Key groups: `incidents`, `agents`, `cameras`, `network`, `models`, `ops`, `mission-context`
- `GET /incidents/{id}/snapshot` returns the JPEG attached to an incident (404 if no snapshot or file missing); dashboard renders this as a 120×80 thumbnail per card.
- `POST /agents/{name}/invoke` to trigger any named agent on demand
- `GET/POST/DELETE /agents/context` — read, set, or clear the active mission context (PPE requirements in plain English). POST body: `{"mission": "..."}`. Triggers one cheap LLM call to extract structured items; subsequent snapshots reuse the persisted context with no extra parse cost.

## Agent active/passive mode
- VLM-backed agents (`zone_intrusion`, `ppe_compliance`) honour a per-agent mode flag persisted in the `Setting` table
- **Default on first boot is `passive`** — no VLM calls are made until you flip them. Prevents accidental token burn
- API: `GET /agents/modes`, `GET/POST /agents/{name}/mode` (body: `{"mode": "active"|"passive"}`)
- Dashboard has clickable pills in the nav; green = active, grey = passive
- Lives in `app/services/agent_mode.py`; gated set is `GATED_AGENTS`

## PPE compliance config
- `cameras.yaml` → `ppe_area.required_item` controls what Agent B checks for when no mission context is set (the legacy single-item fallback).
- Currently supported single-item values: `"hard_hat"` (default) and `"hi_vis_vest"`
- Add more to the fallback by extending `_ITEMS` in `app/agents/ppe_compliance.py` (label, missing-count JSON key, definition string for the prompt). For mission-context (multi-item) flow you don't need to touch `_ITEMS` — the LLM-parsed item names go straight into the VLM prompt.
- **Three-way verdict** in `app/agents/ppe_compliance.py::run`:
  - `violation && conf>=0.5 && missing>=1` → `PPE_VIOLATION` (HIGH if conf>=0.85 else MEDIUM)
  - `in_area>=1 && missing==0 && conf>=0.5` → `PPE_OK` (INFO) compliance note
  - otherwise (nobody in area, or low confidence) → silent (trace only, no incident)
- INFO incidents do NOT bump the dashboard's red live-incident counter; rendered as muted grey cards. `NON_ALERT_SEVERITIES = {'INFO'}` in `static/js/main.js`.

## Mission context (dynamic PPE requirements)
- The **differentiator** vs YOLO-style detectors: detection target set at run time in natural language. No code change, no model retraining, no YAML edit.
- A user (or external agent) describes the required PPE in plain English; one cheap text-only LLM call extracts a structured `AgentContext { required_items, context_note, severity_hint }` which is persisted in the `Setting` table under `mission_context.active` as a JSON blob.
- Lives in `app/services/mission_context.py`; REST surface in `app/api/mission.py` (`/agents/context` GET/POST/DELETE).
- **Scope intentionally narrow**: PPE compliance agent only (zone_intrusion untouched); one global context (not per-camera); 1–5 items per mission; max 500 chars of input.
- Flow when a snapshot arrives:
  1. `ppe_compliance.run()` calls `get_context()`.
  2. If a context exists → build multi-item VLM prompt with the parsed `required_items` + `context_note` injected; expect VLM JSON `{people_in_area, items_checked: [{item, missing_count, confidence}], overall_confidence, summary}`.
  3. If no context → fall back to the legacy single-item path driven by `cameras.yaml`'s `ppe_area.required_item`.
- **Cost discipline**: parsing happens exactly once (on `POST /agents/context`). Per-snapshot evaluation reuses the persisted blob; no extra LLM call.
- Severity for multi-item violations: `severity_hint=high` + overall conf ≥0.85 → HIGH; `high` + lower conf → MEDIUM; `medium` → MEDIUM; `low` → LOW. PPE_OK stays INFO.
- Dashboard "Active Mission" card (top of page): textarea + Set Mission button (with spinner during the LLM call), required-item chips colour-coded by severity, model + parse-time metadata, Clear button to revert to YAML default. Live-refreshes via `mission_context_changed` SSE event so multiple browsers / external agents stay in sync.
- Demo script: type "this is an electronics lab — workers need safety glasses and anti-static wrist straps" → click Set Mission → next person-in-frame snapshot triggers a PPE check against those two items instead of the YAML default.

## Multi-camera support
- Cameras are discovered live from Meraki via `GET /cameras/devices` (org-level `getOrganizationDevices` filtered to MV models). `config/cameras.yaml` is now an **optional per-camera override** file — entries only matter when you want non-default zone polygons, cooldowns, or PPE items for a specific serial.
- Cameras without a YAML entry use `_default_camera_config()` in `app/services/config_loader.py:get_camera_config()`: full-frame polygon `[[0,0],[1,0],[1,1],[0,1]]` for both exclusion and PPE zones, `required_item: "hard_hat"`, `cooldown_seconds: 10`. This means a freshly added Meraki camera works end-to-end with zero config edits.
- `GET /cameras/analytics-watcher` returns the **union** of YAML cameras + Meraki-discovered cameras (YAML order first, then Meraki-only). Silent fallback to YAML-only if Meraki is unreachable so the dashboard still works offline.
- Watchers persist their enabled state in the `Setting` table; `analytics_watcher.restore_on_startup()` now scans the DB (not YAML) so watchers enabled on Meraki-only cameras survive restarts.
- Dashboard `#camera-select` dropdown switches the active camera tile. Selection persists in `localStorage` (`dz.cameraSerial`) so a reload keeps you on the same camera. The watcher and agents run on **all** discovered cameras simultaneously — the dropdown only changes what you're looking at, incidents from every camera continue to land in the feed.

## Dashboard zone overlay
- Endpoint `GET /cameras/{serial}/zones` returns `{serial, zones: [{id, label, polygon, colour, kind, required_item?}]}`. Cameras with no `cameras.yaml` entry fall back to full-frame polygons (0,0 → 1,1) and `required_item: "hard_hat"` so Meraki-discovered cameras work without a YAML edit. Polygons are normalised 0..1 coords; loader falls back to `config/cameras.example.yaml` if real config missing.
- `static/js/main.js::loadZones()` builds SVG polygons inside `#zone-overlay` (viewBox 0 0 100 100, coords * 100) and renders pills in `#zone-legend`. Overlay revealed only after a snapshot loads.
- Exclusion zone = red `#ff3c3c` (`kind: "intrusion"`); PPE area = yellow `#ffd200` (`kind: "ppe"`).

## Analytics-gated capture watcher
- `app/services/analytics_watcher.py` owns one asyncio.Task per camera that polls Meraki's on-device analytics (`/devices/{serial}/camera/analytics/recent`) on an interval. A snapshot is only fetched (`fetch_and_persist_snapshot`) and published onto `camera.snapshot` (with `trigger: "watcher"`) when a person is detected in the Full-Frame zone. Replaces the dumb auto-capture loop — the demo still feels agentic but VLM calls only happen when something interesting is in frame, drastically cutting GitHub Models token spend.
- **Gate rule** (per poll, per camera): for zone `"0"` (Full Frame), if `endTs` advanced **AND** (`averageCount > 0` **OR** `entrances > last_entrances`) → trigger snapshot. Otherwise publish a quiet tick and skip. Catches both "person currently in frame" and "person just walked in" rising edges.
- State persisted in `Setting` table: `analytics_watcher.<serial>.enabled` (`"true"`/`"false"`), `analytics_watcher.<serial>.interval_seconds`.
- API (all under `/cameras`):
  - `GET /analytics-watcher` — list state for every configured camera
  - `GET /{serial}/analytics-watcher` — single-camera state (404 unknown serial)
  - `POST /{serial}/analytics-watcher` body `{"enabled": bool, "interval_seconds"?: int}` — idempotent; 422 if interval outside `[MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS]` (2–60)
  - `GET /{serial}/analytics/recent` — debug passthrough of Meraki's raw analytics JSON for external agents
- Defaults: `DEFAULT_INTERVAL_SECONDS = 5`, `MIN_INTERVAL_SECONDS = 2`, `MAX_INTERVAL_SECONDS = 60`, `MAX_BACKOFF_SECONDS = 60`, `MAX_RUNTIME_SECONDS = 30*60` (auto-shutoff guard).
- Safety rails:
  - Default off on first boot.
  - **Idle skip**: if every gated agent (`zone_intrusion`, `ppe_compliance`) is passive, the loop publishes `analytics_watcher.tick` with `status="idle"` and skips the Meraki analytics call entirely — zero requests in steady state.
  - **Back-off**: consecutive `MerakiError` (on either the analytics poll or the snapshot fetch) doubles the sleep up to 60s; resets on first success.
  - **Auto-shutoff** at 30 min runtime — persists `enabled=false`, publishes `status="auto-shutoff"`, exits.
- Tick statuses (`analytics_watcher.tick` payload `status`): `person` (snapshot fired), `quiet` (analytics polled, no person), `idle` (gated agents all passive), `error` (Meraki failure), `auto-shutoff`.
- Lifespan: `analytics_watcher.restore_on_startup()` re-spawns loops for serials persisted as enabled (interval/clock reset).
- Dashboard pill in the camera tile header — coloured dot + interval input + Start/Stop button. Green pulse on `person`, steady blue on `quiet`, grey on `idle`/`off`, red on `error`, yellow on `auto-shutoff`.

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
- `config/cameras.yaml` — **gitignored**; copy from `config/cameras.example.yaml`. Optional per-camera overrides (zone polygons, cooldown, PPE item). Cameras are discovered live via Meraki `GET /organizations/{id}/devices`; YAML entries only override the full-frame defaults for the listed serials. Loader falls back to the `.example.yaml` if the real file is missing.
- `config/whitelist.yaml` — **gitignored**; copy from `config/whitelist.example.yaml`. MAC entries with label/vendor; separate `blacklist` section.
- `config/models.yaml` — checked in; model catalogue, `default` key sets startup default.

## Docker stack
- `docker/Dockerfile` — multi-stage. Builder stage uses `ghcr.io/astral-sh/uv` and `uv sync --frozen --no-dev` against `uv.lock` for reproducible installs. Runtime stage is `python:3.12-slim` + `tini` (clean SIGTERM → asyncio task cancellation) + `curl` (for HEALTHCHECK). Runs as non-root `appuser` (UID/GID default `1000:1000`, override via `--build-arg UID=$(id -u) GID=$(id -g)`).
- `docker/compose.yaml` — prod stack. Image tag `danger-zone:latest`, container name `danger_zone`. Ports `8001:8001`. Bind volumes: `../data:/app/data` (SQLite + snapshots), `../logs:/app/logs`, `../config:/app/config:ro`. `env_file: ../.env`. `restart: unless-stopped`. Healthcheck hits `/healthz` every 30s.
- `docker/compose.dev.yaml` — override layered with `-f docker/compose.yaml -f docker/compose.dev.yaml`. Bind-mounts `../app` and `../static` for live `--reload`. Sets `DEBUG=true`. `--reload` is scoped to `app/`, `static/`, `config/` only (see gotcha re: reload-death-loop). Drops `restart: unless-stopped` so Ctrl+C cleanly stops.
- `Makefile` — wrappers around both compose invocations. Common targets: `make build`, `make up`, `make up-dev`, `make down`, `make logs`, `make shell`, `make test` (pytest in a one-off container), `make rebuild` (`--no-cache`), `make ps`, `make clean`. `make help` lists everything.
- `.dockerignore` — excludes `data/`, `logs/`, `.venv/`, `tests/`, `*.md`, `.env`, `config/cameras.yaml`, `config/whitelist.yaml`, and all build/cache dirs. The image only carries `.example.yaml` configs; real configs are mounted at runtime.
- `uv.lock` is **tracked in git** so Docker builds are reproducible. Refresh with `uv lock` after editing `pyproject.toml`.
- Meraki webhooks still need a public HTTPS URL even in Docker — `tailscale funnel 8001` on the host works (the container's port is bound to `0.0.0.0:8001` on the host).

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
- VLM-backed agent subscribers (`zone_intrusion`, `ppe_compliance`) coalesce queued `camera.snapshot` events via `_coalesce_latest_by_serial` before running — when VLM latency > watcher interval, this keeps the queue from growing unboundedly and avoids back-to-back redundant VLM calls. Only the most-recent snapshot per serial is processed.
- Dashboard hydrates the incident list on page load via `GET /incidents/?limit=20` (see `static/js/main.js::loadIncidents`). Without this, the list stays empty until a new SSE event arrives even though DB has rows. SSE handler deduplicates by `data-incident-id` to avoid double-rendering during a race.
- **Uvicorn `--reload` MUST be scoped to source dirs only** (`--reload-dir app --reload-dir static --reload-dir config`). Without this it watches the whole cwd, including `logs/` and `data/snapshots/` — every log write or new snapshot triggers a worker restart, which kills background asyncio tasks (analytics watcher, agent subscribers) before they can do useful work. Symptom: clicking Start on the Watcher pill "does nothing", `logs/server.log` fills with thousands of `watchfiles.main INFO 1 change detected` lines spaced ~350ms apart.

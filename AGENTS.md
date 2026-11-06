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
  ingest/            — source-agnostic video ingest (file / rtsp / synthetic)
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
| `camera.frame` | ingest manager (one per decoded frame) | (metadata only; future detector service) |
| `camera.snapshot` | `/cameras/{serial}/snapshot` + motion handler + analytics watcher + ingest | zone_intrusion agent, ppe_compliance agent |
| `network.client` | meraki webhook | rogue_device agent |
| `agent.invoke.*` | POST /agents/{name}/invoke | named agent |
| `analytics_watcher.tick` | analytics watcher loop | dashboard SSE (status dot) |
| `mission_context.changed` | `set_context` / `clear_context` | dashboard SSE (live-refresh mission card) |

## VLM / LLM Provider
- **Local-first** for the Cisco/Curtin expo. The default is a self-hosted vLLM
  server (`VLM_BASE_URL=http://127.0.0.1:8000/v1`) serving
  `qwen3-vl-235b-awq` on 2×H100. The provider class is still named
  `GitHubModelsProvider` because it speaks the same OpenAI-compatible protocol;
  it can point at any compatible endpoint.
- Cloud models (GitHub Models / OpenAI / Anthropic) are **hidden from
  `/models/` and rejected by `set_active_model` unless
  `ALLOW_CLOUD_MODELS=true`**. This is a data-sovereignty guard: an operator
  cannot accidentally send a frame to a hosted provider.
- `config/models.yaml` marks local entries with `local: true` and sets
  `default: qwen3-vl-235b-awq`. `list_available_models()` applies the filter;
  `list_models()` still returns the raw catalogue for diagnostics.
- Only models with `vision: true` can process snapshots.
- Provider abstraction lives in `app/integrations/vlm.py` —
  `analyse_image()` sends the image as a base64 data URL and forces
  `response_format=json_object`; `complete()` is the text-only sibling used by
  the mission-context parser.
- Active model switchable at runtime via `POST /models/active` or the dashboard
  dropdown, but only among currently selectable (local) models.

## External Agent API
- OpenAPI at `/docs`. **The REST API currently has no auth dependency** — `API_KEY` is only enforced by the MCP bridge below, so anyone who can reach port 8001 can call it. Keep it on localhost or a trusted tailnet/ACL.
- Historical intent was `X-API-Key` on every route; do not assume it is active.
- Key groups: `incidents`, `agents`, `cameras`, `network`, `models`, `ops`, `mission-context`
- `GET /incidents/{id}/snapshot` returns the JPEG attached to an incident (404 if no snapshot or file missing); dashboard renders this as a 120×80 thumbnail per card.
- `POST /agents/{name}/invoke` to trigger any named agent on demand
- `GET/POST/DELETE /agents/context` — read, set, or clear the active mission context (PPE requirements in plain English). POST body: `{"mission": "..."}`. Triggers one cheap LLM call to extract structured items; subsequent snapshots reuse the persisted context with no extra parse cost.

## MCP bridge (OpenCode / external clients)
- `app/mcp_server.py` wraps the REST API as a Streamable-HTTP MCP server so OpenCode and other MCP clients can use the agents as tools. It is a thin `httpx` client of `DZ_API_URL` — never re-implements agent logic.
- 15 tools: `list_incidents`, `get_incident`, `get_incident_snapshot_url`, `get_agent_traces`, `get_agent_modes`, `set_agent_mode`, `invoke_agent`, `get_mission`, `set_mission`, `clear_mission`, `list_cameras`, `get_ingest_status`, `get_zones`, `capture_snapshot`, `get_latest_snapshot_url`.
- Run with `python -m app.mcp_server` after `uv sync --extra mcp` (optional dependency group; the app itself does not import it). Deployed as `danger-zone-mcp.service` on `127.0.0.1:8010`, exposed tailnet-only via `sudo tailscale serve --bg 8010` → `https://xenterm-wl180.tailc0c1b.ts.net/mcp`.
- Auth is a pure-ASGI `ApiKeyMiddleware` checking `X-API-Key` (or `Authorization: Bearer`) against `DZ_API_KEY`, falling back to `API_KEY` from settings. This is the only place `API_KEY` is currently enforced.
- Env: `DZ_API_URL`, `DZ_API_KEY`, `DZ_PUBLIC_URL` (snapshot links), `DZ_MCP_HOST`, `DZ_MCP_PORT`.
- mcp 2.x API: `from mcp.server.mcpserver import MCPServer` (FastMCP was renamed); client tests use `httpx2.AsyncClient` + `mcp.client.streamable_http.streamable_http_client`.
- OpenCode remote MCP defaults to OAuth; a header-authenticated server needs `"oauth": false` in `mcp.servers.<name>`. `{env:VAR}` is not expanded in MCP headers, so the key is literal in the client config.

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

## Mission context (dynamic PPE requirements and zone rules)
- The **differentiator** vs YOLO-style detectors: detection target set at run time in natural language. No code change, no model retraining, no YAML edit.
- A user (or external agent) describes the required PPE in plain English; one cheap text-only LLM call extracts a structured `AgentContext { required_items, context_note, severity_hint }` which is persisted in the `Setting` table under `mission_context.active` as a JSON blob.
- Lives in `app/services/mission_context.py`; REST surface in `app/api/mission.py` (`/agents/context` GET/POST/DELETE).
- **Scope intentionally narrow**: one global context (not per-camera); 1–5 PPE items and up to 3 natural-language `zone_rules` (140 chars each) per mission; max 500 chars of input. A mission needs at least one item or one zone rule.
- **Natural-language zones**: `AgentContext.zone_rules` keeps the user's own wording (no coordinates). When rules exist, `zone_intrusion.run()` takes the `_run_rules` path: clean frame + numbered rules → VLM JSON `{people_visible, rules: [{rule_index, violated, confidence, people_involved, reasoning}]}` → one `INTRUSION` incident per rule with `violated && conf>=0.5` (CRITICAL ≥0.85 else HIGH; `extra.zone_rule` holds the rule). With no rules the legacy polygon path is unchanged. Works without an `exclusion_zone` in YAML. The prompt tells the model to ignore people on screens/posters.
- A zone-only mission (no PPE items) makes `ppe_compliance` skip instead of falling back to the YAML default.
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

## Dashboard camera scope
- The tile shows one camera, but agents run on **every** ingest camera and the incident/trace feeds are global. Each incident card and trace line carries `data-camera-id` and the camera's friendly name; a **This camera / All cameras** toggle (persisted in `localStorage` `dz.incidentScope`, default *this camera*) hides the rest and shows "N from other cameras hidden". `agent.trace` events include `serial`.

## Branding, layout and status bar
- **Layout** (`app/dashboard/templates/index.html`, CSS "design system v2" at the end of `static/css/main.css`): a fixed one-screen app frame on ≥1200px (`body.app{height:100vh}`; below that it scrolls normally). Header = ICP tile + shield wordmark (`PRODUCT_NAME`, default **Safety Sentry**) on the left, **Powered by** Meraki / Splunk / NIIN tiles on the right, then the ops bar. Main = left: camera panel (hero video) + Agent reasoning; right: Mission + Live incidents. Footer is only the trademark line. Every element ID used by `main.js` must survive template edits (`tests/test_smoke.py` guards the key ones).
- **Self-contained**: Bootstrap and the Inter variable font are served from `static/vendor/` and `static/fonts/` (no CDN; the page works with no internet). htmx was unused and has been removed. `<html data-bs-theme="dark">` is required for Bootstrap's muted/form colours to be legible.
- **Colour roles**: navy surfaces; Cisco-blue accent for the one interactive colour; amber = replay/mission; red = violation/zone; green = healthy/compliant. Tokens are the `--bg/--panel/--accent/--ok/--warn/--bad` CSS variables.
- **Video sizing** uses container-query units (`.video-stage{container-type:size}`, image `max-width:100cqw;max-height:100cqh`) so the feed fills whatever space the layout gives it. Camera-mode badge, caption and an **AI chip** sit over the video; the chip lights for ~2 s with the agent name and model latency on every `agent_trace` for the camera on screen, and a red ring flashes (`flashAlert`) when an alert incident lands on it. The status badge never says "live" for a replay (it follows `cameraModeFor`). Watcher / Capture-now are `.cloud-only` and hidden unless the camera is a Meraki cloud snapshot.
- **Reasoning feed** shows plain English via `prettyDecision()` (raw agent text stays in the tooltip); incident cards drop the serial from the sentence and use the camera's friendly name (`relabelCameras()` fixes cards rendered before the camera list loaded).
- **Ops bar**: SYSTEM (SSE connected/offline), SITE (`SITE_NAME`), CAMERAS (`running/total streaming · mode`), AGENTS (active/passive pills), AI (model + "NIIN Sovereign Cloud · no external AI"), Perth/Melbourne clocks, and a gear menu holding the vision-model picker and API docs link.
- **Partner logos** resolve from `static/img/partners/<key>.{svg,png,webp,jpg}` (`meraki`, `splunk`, `niin`; `icp` is fixed in the header) in `app/dashboard/routes.py::_PARTNERS`; each sits on a solid white tile (partner guidelines want a plain background and an unaltered mark). A missing file falls back to the plain product name, never a broken image. `meraki.png` and `splunk.png` are cleaned display copies of `MERAKI_LOGO.webp` (checkerboard removed) and `SPLUNK_LOGO.png` (margin trimmed); replace them with the official partner-portal files when available. `niin.png` and `icp.png` are the supplied logos trimmed of margin. The Splunk and Cisco/Meraki trademark attribution is the footer.
- Reviewing the UI: headless Chromium (Playwright) works on this VM for real screenshots; see how `/tmp/opencode/shot.py` was used (install with `uv pip install --target /tmp/opencode/pw playwright` and `PLAYWRIGHT_BROWSERS_PATH=... python -m playwright install chromium`).

## Backup and restore
- `scripts/backup_demo_assets.sh [OUT|-] [--no-secrets] [--with-db]` archives what git does not hold: `data/demo` (clips, joined reels, `trims.txt`), non-example `config/*.yaml`, `.env` + `.meraki_key.env` (secrets; skip with `--no-secrets`), optionally a consistent SQLite copy (`--with-db`, uses the sqlite backup API). Snapshots are excluded (GBs). Writes `MANIFEST.txt` (git commit, sha256 of every file) and `RESTORE.txt`. `OUT=-` streams to stdout so a laptop can run `ssh vm 'danger_zone/scripts/backup_demo_assets.sh -' > backup.tgz`.
- `scripts/restore_demo_assets.sh BACKUP.tgz` restores into the repo and `DATA_DIR` (override with `REPO_DIR` / `DATA_DIR`) and verifies every file against the manifest (non-zero exit on mismatch).

## Ask Safety Sentry (natural-language assistant)
- `POST /assistant/ask {question, feed_cleared_after_id?}` → `{answer, source: llm|template|error, plan, facts, evidence[], actions[], took_ms}`. Code: `app/services/assistant.py` (+ `app/services/events.py`, `app/api/assistant.py`). UI: ask bar under the incident panel (bottom-right); typing opens a sheet over the incident list (over the whole right column on screens ≤840px tall); `/` focuses it, Esc closes it.
- **The model never writes SQL or does arithmetic.** Step 1 (`complete()` in JSON mode, few-shot) turns the question into a `QueryPlan` {intent count|list|status|retask|unknown, event_type, item, camera, window mission|today|hour|all, group_by, keyword}; `validate_plan` coerces anything odd to safe defaults. Step 2 runs fixed read-only queries. Step 3 asks the model to phrase 1–2 sentences from the facts only. `answer_is_grounded` rejects a sentence containing any number not in the facts, or saying "none/no one" when events > 0; then the deterministic `template_answer` is used instead (`source: "template"`). No native tool-calling is used (the vLLM server is not launched with tool-call flags).
- **Events, not rows**: an agent writes an incident every few seconds, so `events.group_events` merges consecutive incidents (same camera, type and zone rule, gap ≤ 20 s) into one event. Item names go through `canonical_item` (helmet/hardhat → hard hat). Default window is **since the later of the last mission change and the last Clear feed** (`mission` window); only cameras in the current camera config are counted (not leftovers from old demos).
- **"Was it even checking?"**: for item questions `watching.checked_in_window` comes from `agenttrace.inputs_json` (the PPE agent logs the items it checked). If it wasn't, the answer says so and an action `{type: set_mission, label, mission}` is offered (the UI POSTs it to `/agents/context`; existing mission items and zone rules are preserved).
- **Evidence and jump links**: up to 3 events, each with the best incident's thumbnail (`/incidents/{id}/snapshot`, opens the existing lightbox) and, for replay cameras, `replay_position`. Positions come from `app/ingest/positions.py`: the ingest manager registers `snapshot path → reel position`, and `create_incident` copies it into `extra.replay_position`. The UI's **▶ Jump** switches the camera and POSTs `/cameras/{serial}/playback {position - 3 s}`. Incidents recorded before this existed have no position (no jump button).
- Times quoted use `SITE_TIMEZONE` (default Australia/Perth). The query layer is deliberately one module so Splunk (SPL) can sit behind the same plan later.

## Dashboard feed controls
- Incident cards show a **Mission: "…"** tag (from `extra.mission`, truncated, full text in the tooltip) so verdicts from different missions on the same footage are distinguishable. `mission_context_changed` SSE events add a **▶ New mission: "…"** marker line to the top of the feed (client-side only, not persisted).
- **Clear feed** empties the incident and reasoning lists without deleting anything: it stores the newest incident id in `localStorage` (`dz.feedClearedAfterId`) and hydrate/SSE only show newer ids. `parseExtra` accepts both `extra` (live SSE object) and `extra_json` (history string).

## Dashboard zone overlay
- Endpoint `GET /cameras/{serial}/zones` returns `{serial, zones: [{id, label, polygon, colour, kind, required_item?}]}`. Cameras with no `cameras.yaml` entry fall back to full-frame polygons (0,0 → 1,1) and `required_item: "hard_hat"` so Meraki-discovered cameras work without a YAML edit. Polygons are normalised 0..1 coords; loader falls back to `config/cameras.example.yaml` if real config missing.
- `static/js/main.js::loadZones()` builds SVG polygons inside `#zone-overlay` (viewBox 0 0 100 100, coords * 100) and renders pills in `#zone-legend`. Overlay revealed only after a snapshot loads.
- Exclusion zone = red `#ff3c3c` (`kind: "intrusion"`); PPE area = yellow `#ffd200` (`kind: "ppe"`).

## Dashboard source labels and mission presets
- `static/js/main.js::cameraModeFor()` labels each tile from `/cameras/`:
  `LIVE` (`source_type: rtsp`), `CLOUD SNAPSHOT` (Meraki snapshot, no `source:`),
  `REPLAY` (`source_type: file` or explicit `demo_label`), `SYNTHETIC`
  (`source_type: synthetic`). The badge appears in the tile header and as an
  overlay on the image (`#camera-mode-overlay`) so screenshots are unambiguous.
- Ingest-backed cameras use the MJPEG stream; Meraki-cloud cameras keep the
  snapshot-polling path. `refreshSnapshot()` must not reset an active MJPEG
  stream on every `camera.snapshot` SSE event.
- The mission card has one-click presets (Hard hat, Hard hat + vest, All three,
  Goggles only) that fill the textarea and submit immediately. The mission
  parser preserves “safety goggles” rather than normalising it to “glasses”.

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

## Video ingest (source-agnostic)
- `app/ingest/` decouples the app from Meraki. Every backend implements one `FrameSource` protocol (`stream() -> AsyncIterator[Frame]`, `aclose()`), and everything downstream only sees bus events. Swapping a recorded clip for a live Meraki RTSP stream is a config edit, not a code change.
- Backends: `synthetic` (Pillow-generated scripted workshop scene — no camera or footage needed), `file` (any ffmpeg-readable clip, looped in real time), `rtsp` (Meraki external RTSP or any IP camera, TCP + reconnect/backoff).
- `app/ingest/ffmpeg_source.py` runs one ffmpeg subprocess per camera (`-f image2pipe -vcodec mjpeg`), splits the concatenated JPEG stream with `JpegStreamParser`, and reads dimensions from the JPEG header. `file`/`rtsp` differ only in input args (`-readrate`, `-stream_loop`, `-rtsp_transport tcp`).
- `app/ingest/manager.py` starts one task per camera that declares a `source:` block in `config/cameras.yaml`. Per frame it pushes into a bounded drop-oldest `FrameRing` and publishes `camera.frame`; at `vlm_fps` it also persists a JPEG and publishes `camera.snapshot` (the topic the VLM agents already consume). Rate-limiting snapshots is deliberate — a slow VLM must never stall ingest.
- Cameras without a `source:` block are ignored, so Meraki-only installs are unchanged. Ingest is off unless `INGEST_ENABLED=true`.
- `GET /cameras/ingest` reports running sources, buffer depth and last sequence. `POST /cameras/{serial}/snapshot` returns the newest buffered frame for ingest-backed cameras instead of calling Meraki.
- `GET /cameras/{serial}/stream.mjpg` serves a multipart MJPEG stream from the frame ring (only new sequence numbers are sent; the generator ends after 10 s with no new frames). Returns 404 for Meraki-cloud-only cameras. The dashboard uses this for ingest-backed tiles so file/RTSP/synthetic sources look like video instead of a once-per-second still.
- **Replay scrubbing**: `FileSource` is `seekable` (duration via ffprobe). `GET /cameras/{serial}/playback` → `{supported, position, duration, paused}`; `POST` body `{position?, paused?}` seeks/pauses/resumes (404 unknown camera, 409 not seekable, 422 negative position). Seek restarts ffmpeg with `-ss`; pause stops it and holds one decoded frame, re-emitted ~2/s so the MJPEG stream stays alive. A paused frame is published to the VLM agents **once** per hold (`hold_id`), not every tick. Position is anchored to the first decoded frame. `-readrate` + `-ss` decodes forward in real time from the previous keyframe, so reels **must** have dense keyframes: `scripts/build_demo_reel.sh` encodes `-g 10` (1 s). Dashboard shows a pause button + slider under file-source tiles.
- **Section loop (A–B)**: `POST /cameras/{serial}/playback` also takes `loop_start` + `loop_end` (together; 1–180 s) and `clear_loop`. ffmpeg cannot loop a sub-range, so `FileSource.set_loop` cuts the section into a temp clip under `data/loops/` (~0.5 s) and loops that with `-stream_loop` (seamless). Positions stay in whole-reel time (`_pos_base`); a seek inside the range stays looped, outside it clears the loop. `GET /playback` returns `loop: {start,end}|null`. Temp clips are deleted on clear/replace and at startup. Dashboard: **A** / **B** buttons (B alone loops the last 10 s), a highlighted band on the slider, **⟲ ✕** to clear.
- `GET /cameras/` now exposes `source_type`, `demo_label`, and `vlm_fps` so the dashboard can label a tile `LIVE`, `CLOUD SNAPSHOT`, `REPLAY`, or `SYNTHETIC`.
- Demo config: a synthetic camera lives in the (gitignored) `config/cameras.yaml`. The default script cycles empty → compliant worker → worker without a hard hat, so a PPE demo always lands on cue.
- `DEMO_PROFILE=replay` loads `config/cameras.replay.yaml`; see `docs/EXPO_RUNBOOK.md` for the cue sheet and recording guide.

## Webhook setup (Meraki → app)
- Endpoint: `POST /webhooks/meraki`
- Signature verification: HMAC-SHA256; set `MERAKI_WEBHOOK_SECRET` to the Meraki shared secret
- Header may be `sha256=<hex>` or just `<hex>` — use `removeprefix` not `lstrip`
- For local dev: `tailscale funnel 8001` to expose a public HTTPS URL

## Required env vars (see `.env.example`)
- `MERAKI_API_KEY`, `MERAKI_ORG_ID`, `MERAKI_WEBHOOK_SECRET`
- `MERAKI_ALLOW_WRITE_KEY=true` to bypass the read-only-key safety check (dev only)
- `VLM_BASE_URL` / `VLM_API_KEY` — point the VLM layer at a self-hosted
  OpenAI-compatible server (e.g. `http://127.0.0.1:8000/v1`).
- `MODEL_ID` — default `qwen3-vl-235b-awq` (local). If it names a cloud model
  while `ALLOW_CLOUD_MODELS=false`, it is ignored with a warning and the first
  local model is used.
- `ALLOW_CLOUD_MODELS` — default `false`. When false, cloud entries are hidden
  and cannot be selected.
- `GITHUB_TOKEN` — legacy cloud fallback, only used when `VLM_BASE_URL` is
  empty. The device-flow helper `python scripts/gh_device_login.py` still works.
- `API_KEY` (for external agent auth)
- `INGEST_ENABLED` — start ingest tasks for cameras with a `source:` block (default `false`)
- `DEMO_PROFILE` — optional camera-config profile. `DEMO_PROFILE=replay` loads
  `config/cameras.replay.yaml` instead of `config/cameras.yaml`; missing profile
  files fall back to the default with a warning.
- `DEBUG=true` enables verbose logging / FastAPI debug
- `DEMO_AGENT=true` runs the synthetic demo agent (fires fake incidents every 15s). Decoupled from `DEBUG` so you can run verbose logs against the real camera without synthetic noise.

## Config files
- `config/cameras.yaml` — **gitignored**; copy from `config/cameras.example.yaml`. Optional per-camera overrides (zone polygons, cooldown, PPE item). Cameras are discovered live via Meraki `GET /organizations/{id}/devices`; YAML entries only override the full-frame defaults for the listed serials. Loader falls back to the `.example.yaml` if the real file is missing.
- `config/cameras.<profile>.yaml` — **gitignored** site-specific profiles. `DEMO_PROFILE=<profile>` selects one; `replay` is the expo profile.
- `config/cameras.replay.example.yaml` — checked-in template for recorded-footage demos. Points at `data/demo/*.mp4`, sets `demo_label: "REPLAY"` and `vlm_fps: 1.0`.
- `config/whitelist.yaml` — **gitignored**; copy from `config/whitelist.example.yaml`. MAC entries with label/vendor; separate `blacklist` section.
- `config/models.yaml` — checked in; model catalogue, `local: true` marks self-hosted models, `default` sets the startup default. Cloud entries are hidden unless `ALLOW_CLOUD_MODELS=true`.
- `.meraki_key.env` — **gitignored**; never commit it.

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

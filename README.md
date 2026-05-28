# danger_zone

Agentic AI security demonstrator using Meraki cameras and network infrastructure.

## What it does

Three proactive AI agents watch your environment and log incidents automatically:

- **Zone Intrusion** — Detects people entering a configured exclusion zone via camera
- **PPE Compliance** — Checks for yellow hard hat in a work area using a VLM
- **Rogue Device** — Flags non-whitelisted MAC addresses joining the network

A live HTMX dashboard shows the agent reasoning traces, incidents, and status in real time. A documented REST API lets other AI agents query state and trigger agents on demand.

## Quick start

```bash
cp .env.example .env
# Fill in MERAKI_API_KEY, MERAKI_ORG_ID, GITHUB_TOKEN, etc.

uv pip install -e ".[dev]"
DEBUG=true uvicorn app.main:app --reload
```

Open http://localhost:8000 for the dashboard, http://localhost:8000/docs for the API.

With `DEBUG=true` the demo agent fires synthetic incidents every 15 seconds so the dashboard works without real Meraki hardware.

## Webhook setup (local dev)

Meraki webhooks need a public HTTPS URL to reach your local instance:

```bash
tailscale funnel 8000
```

Then set the resulting URL as your webhook receiver in the Meraki Dashboard:  
`https://<your-tailscale-host>/webhooks/meraki`

Set `MERAKI_WEBHOOK_SECRET` in `.env` to the shared secret configured in Meraki.

## Docker

```bash
docker compose -f docker/compose.yaml up --build
```

## Branch workflow

Features are developed on `feat/<name>` branches and PR'd into `main`:

| Branch | Milestone |
|--------|-----------|
| `feat/skeleton` | FastAPI + dashboard + demo agent |
| `feat/meraki-client` | Meraki API client + webhook receiver |
| `feat/vlm-zone-intrusion` | VLM adapter + zone intrusion agent |
| `feat/ppe-compliance` | PPE agent |
| `feat/rogue-device` | Rogue device agent + whitelist UI |
| `feat/polish` | Heartbeat narrator + external-agent docs |

## Configuration

- `config/cameras.yaml` — camera serial, exclusion zone + PPE area polygons (normalised 0–1 coords)
- `config/whitelist.yaml` — MAC whitelist and blacklist
- `config/models.yaml` — VLM model catalogue; set `default` to choose startup model

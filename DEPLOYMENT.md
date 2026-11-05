# Deployment — danger_zone on 2×H100

This box runs a production-shaped deployment of the `danger_zone` PoC with two
changes that matter for a live demo:

1. **Video ingest is source-agnostic.** The app no longer assumes Meraki. A
   camera entry declares a `source:` block (`synthetic`, `file`, `rtsp`), and
   everything downstream — agents, dashboard, incidents — only sees
   `camera.frame` / `camera.snapshot` bus events.
2. **The VLM is local.** Qwen3-VL-235B-A22B-Instruct-AWQ (4-bit) is served by
   vLLM across the two H100s, so there are no hosted-API tokens, rate limits,
   or network dependency in the demo path. The AWQ checkpoint is used because
   the 238 GB FP8 version no longer fits the 192 GB box; the FP8 weights are
   still on disk for a future 4-card machine (`~/vlm/run-235b.sh`).

## What runs where

| Process | Address | Start |
|---|---|---|
| vLLM (Qwen3-VL-235B AWQ, TP2) | `127.0.0.1:8000/v1` | `~/vlm/run-235b-awq.sh` (container restarts automatically) |
| danger_zone API + dashboard | `0.0.0.0:8001` | `systemctl start danger-zone` (enabled at boot) |
| MCP bridge (OpenCode) | `127.0.0.1:8010/mcp`, proxied by `tailscale serve` | `systemctl start danger-zone-mcp` (enabled at boot) |
| Ingest tasks | in-process | started by the app lifespan when `INGEST_ENABLED=true` |
| Storage | `/models` | 1 TB volume |

`~/vlm/README.md` covers the model server in detail. The app is a systemd
unit (`/etc/systemd/system/danger-zone.service`) so it survives reboots and
restarts on failure:

```bash
sudo systemctl status danger-zone danger-zone-mcp
sudo systemctl restart danger-zone
journalctl -u danger-zone -f
```

The checked-in unit files live in `deploy/`. To install them on a fresh box:

```bash
sudo cp deploy/danger-zone.service deploy/danger-zone-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now danger-zone danger-zone-mcp
```

## First run / cold boot

```bash
# 1. Model server (cold start ~3-6 min: 125 GB of AWQ weights + torch.compile)
~/vlm/run-235b-awq.sh
sudo docker logs -f vllm-qwen3vl     # wait for "Application startup complete"

# 2. App + MCP bridge (the bridge Requires=danger-zone)
sudo systemctl start danger-zone danger-zone-mcp
```

On reboot everything comes back on its own: the vLLM container has
`--restart unless-stopped`, and `danger-zone.service` / `danger-zone-mcp.service`
are enabled. The app does not wait for vLLM at startup — it connects lazily on
the first VLM call. The MCP bridge is optional for the demo; install its extra
once with `uv sync --extra dev --extra mcp` (a plain `uv sync` removes it).

Open <http://localhost:8001>. The camera dropdown lists the Meraki cameras
discovered from the org (e.g. `MRWA MV12W ICP WORKSHOP`) and the tile shows the
latest real snapshot.

### Verified on this machine

| Check | Result |
|---|---|
| Synthetic compliant frame | `violation=false`, confidence 0.95, ~1.7 s |
| Synthetic no-hard-hat frame | `violation=true`, confidence 0.95, ~1.0 s |
| Mission parse (`hard hat` + `hi-vis vest`) | both items extracted by the local 235B |
| Live cycle | `PPE_OK` INFO + `PPE_VIOLATION` HIGH incidents with annotated 1280×720 snapshots |
| Real Meraki snapshot (`Q2GV-XPS8-82TX`) | 1920×1080 JPEG, ~14 s cold; empty frame → both agents silent/cleared |

## Config: choosing the video source

Everything is driven by `config/cameras.yaml` (gitignored). There are two
independent paths:

- **Meraki cloud snapshots** — omit the `source:` block. Cameras are discovered
  live from Meraki and the app uses the cloud snapshot + analytics APIs. This
  works from a cloud VM with no route to the camera LAN, and is what the current
  deployment uses.
- **Local video ingest** — add a `source:` block (`synthetic`, `file`, or
  `rtsp`). This is the source-agnostic path and needs a network route for RTSP.

Switch paths by editing the `source:` block — no code changes.

You can also keep a whole alternative camera file and select it with an env
var, which is safer than editing the live config in front of an audience:

```bash
cp config/cameras.replay.example.yaml config/cameras.replay.yaml
# .env
DEMO_PROFILE=replay
```

`DEMO_PROFILE=replay` loads `config/cameras.replay.yaml`; unset it to return to
`config/cameras.yaml`. A missing profile file falls back to the default with a
warning. See `docs/EXPO_RUNBOOK.md` for the expo recording guide and cue sheet.

Cloud-hosted VLM entries are hidden from the model picker and rejected by the
API unless `ALLOW_CLOUD_MODELS=true`. Keep it false for data-sovereign demos.

### Synthetic (fallback — no camera, no footage)

The previous local config is saved at `/home/marrabld/cameras.synthetic.yaml`;
copy it back to `config/cameras.yaml` to run without cameras.

```yaml
cameras:
  - serial: "DEMO-SYNTH-0001"
    name: "Synthetic Workshop"
    source:
      type: synthetic
      fps: 5
      width: 1280
      height: 720
      script:
        - {duration: 6,  person: false}
        - {duration: 10, person: true, hard_hat: true,  hi_vis: true}
        - {duration: 14, person: true, hard_hat: false, hi_vis: true}
        - {duration: 6,  person: false}
    vlm_fps: 0.2
```

The script cycles empty → compliant worker → worker without a hard hat, so the
PPE agent always has a violation to find on cue.

### Recorded clip

```yaml
    demo_label: "REPLAY"          # dashboard tile badge
    source:
      type: file
      uri: "data/demo/ppe_clip.mp4"
      loop: true
      speed: 1.0
      fps: 5
      width: 1280
    vlm_fps: 1.0                  # 1 VLM snapshot/sec; agents add their own cooldown
```

Drop the clip in `/models/danger_zone/data/demo/` and use an absolute path, or
a path relative to the repo root. The dashboard serves ingest-backed cameras as
an MJPEG stream (`/cameras/{serial}/stream.mjpg`), so a recorded clip looks like
video instead of a once-per-second still. The `REPLAY` badge stays visible on
the tile and over the image.

### Meraki cloud snapshots (no RTSP route needed)

Omit the `source:` block entirely. The app discovers MV cameras via the Meraki
API and uses:

- `POST /cameras/{serial}/snapshot` — real 1920×1080 JPEG. The signed URL 307s
  to a regional streaming host (`streaming.apac.meraki.com`), then returns 400
  until the camera uploads; cold fetch is ~10–15 s. The client follows redirects
  and polls for up to 25 s.
- `POST /cameras/{serial}/analytics-watcher` — polls on-device analytics and
  captures only when a person is present, so VLM calls happen on interesting
  frames. Auto-shuts off after 30 minutes; re-enable from the dashboard pill.

The three ICP cameras (`Q2GV-MGCS-QRM5`, `Q2GV-52D5-YTLK`, `Q2GV-XPS8-82TX`)
are configured this way. `MERAKI_ALLOW_WRITE_KEY=true` is only needed because
the current key has `orgAccess='full'`; a read-only key is recommended.

### Live camera (Meraki external RTSP)

```yaml
    source:
      type: rtsp
      uri: "rtsp://admin:password@10.0.0.42:9000/live"
      fps: 5
      width: 1280
```

Meraki: Dashboard → Camera → Settings → External RTSP. TCP port 9000,
unencrypted, one stream per camera, LAN access required.

Meraki RTSP is LAN-only. A cloud VM needs a route to the camera subnet — e.g. a
Tailscale subnet router on-site advertising `192.168.6.0/24`, approved in the
admin console. Test with `ffprobe -rtsp_transport tcp ...` before enabling the
`source:` block; the current VM cannot reach `192.168.6.x` directly.

### Keys that control ingest

| Key | Meaning |
|---|---|
| `source.fps` | frames decoded per second (detector rate) |
| `source.width` | output width; height keeps aspect |
| `vlm_fps` | snapshots persisted/published per second — i.e. how often the VLM agents may run |
| `ring_size` | buffered frames per camera (default 16, drop-oldest) |
| `cooldown_seconds` | per-agent cooldown on top of `vlm_fps` |

`INGEST_ENABLED=true` is set in `.env`. Cameras without a `source:` block are
ignored by the ingest manager; they are still discovered from Meraki and use the
snapshot/analytics path above, so a Meraki-only install works without RTSP.

## Demo runbook

1. Confirm the model is up:

   ```bash
   curl -s http://127.0.0.1:8000/v1/models -H 'Authorization: Bearer <vllm-api-key>'
   ```

2. Flip both VLM agents to active (they default to passive; both are already
   active on this box):

   ```bash
   curl -s -X POST localhost:8001/agents/zone_intrusion/mode \
     -H 'X-API-Key: <API_KEY from .env>' -H 'Content-Type: application/json' \
     -d '{"mode":"active"}'
   curl -s -X POST localhost:8001/agents/ppe_compliance/mode \
     -H 'X-API-Key: <API_KEY from .env>' -H 'Content-Type: application/json' \
     -d '{"mode":"active"}'
   ```

3. Optional — set a natural-language mission. This is the differentiator:
   the detection target changes without code or model changes.

   ```bash
   curl -s -X POST localhost:8001/agents/context \
     -H 'X-API-Key: <API_KEY from .env>' -H 'Content-Type: application/json' \
     -d '{"mission":"this is a workshop — everyone inside needs a hard hat and a hi-vis vest"}'
   ```

4. Watch the dashboard. Within one script cycle (~36 s) the synthetic worker
   loses their hard hat and a `PPE_VIOLATION` incident appears with the
   annotated snapshot.

5. To prove the source is irrelevant, stop the app, swap the `source:` block
   to a file or RTSP camera, and restart. Nothing else changes.

## API surface added for ingest

| Endpoint | Purpose |
|---|---|
| `GET /cameras/ingest` | running sources, buffer depth, last sequence |
| `POST /cameras/{serial}/snapshot` | newest buffered frame for ingest cameras (falls back to Meraki otherwise) |
| `GET /cameras/{serial}/snapshot/latest` | newest JPEG on disk (dashboard tile) |
| `GET /cameras/{serial}/stream.mjpg` | MJPEG stream from the ingest frame ring (404 for Meraki-cloud-only cameras) |
| `GET /cameras/{serial}/zones` | polygons for the dashboard overlay |

## Connect OpenCode from your laptop

OpenCode speaks MCP, not the app's REST API, so this box runs a small bridge
(`app/mcp_server.py`) that exposes the agents as MCP tools. It is a thin HTTP
client of the local API — no second implementation, no extra state.

| Piece | Value |
|---|---|
| MCP endpoint | `https://xenterm-wl180.tailc0c1b.ts.net/mcp` (Streamable HTTP) |
| Local bind | `127.0.0.1:8010` — only reachable through `tailscale serve` |
| Auth | `X-API-Key: <API_KEY from .env>` |
| Service | `danger-zone-mcp.service` (enabled at boot, requires `danger-zone`) |

Tools: incidents (`list_incidents`, `get_incident`, snapshot links), agents
(`get_agent_modes`, `set_agent_mode`, `invoke_agent`, `get_agent_traces`),
mission context (`get_mission`, `set_mission`, `clear_mission`), cameras/ingest
(`list_cameras`, `get_ingest_status`, `get_zones`, `capture_snapshot`).

### On the laptop (same tailnet)

1. Read the key on this box, then export it on the laptop:

```bash
# server
grep '^API_KEY=' ~/danger_zone/.env
# laptop
export DZ_API_KEY=<paste>
```

2. Add the server to the global config:

```bash
opencode mcp add danger-zone --global \
  --url https://xenterm-wl180.tailc0c1b.ts.net/mcp \
  --header "X-API-Key=$DZ_API_KEY"
```

3. OpenCode defaults remote servers to OAuth. Add `"oauth": false` next to the
   URL in the global config the command reported (usually
   `~/.config/opencode/opencode.json`); the CLI does not set it:

```json
{
  "mcp": {
    "servers": {
      "danger-zone": {
        "type": "remote",
        "url": "https://xenterm-wl180.tailc0c1b.ts.net/mcp",
        "oauth": false,
        "headers": { "X-API-Key": "<paste the key>" }
      }
    }
  }
}
```

   `{env:DZ_API_KEY}` is **not** expanded inside MCP headers (verified with
   OpenCode 2.1.3), so the key has to be literal. `chmod 600` the file.

4. Verify:

```bash
opencode mcp list          # → ✓ danger-zone  connected
```

Then ask things like:

- "List the last five incidents and tell me which camera fired most."
- "Set the mission to: this is an electronics lab, workers need safety glasses and anti-static wrist straps."
- "Turn the PPE agent passive, capture a snapshot from DEMO-SYNTH-0001, then turn it active again."

### Notes

- `tailscale serve` must stay configured; `tailscale serve status` should show
  `/ → http://127.0.0.1:8010`. Re-create with `sudo tailscale serve --bg 8010`.
  This is Serve (tailnet only), not Funnel.
- The app's REST API on `http://100.107.106.43:8001` is **not** API-key
  protected (the dashboard calls it without headers). Anyone on the tailnet can
  reach it — use Tailscale ACLs if that matters. The dashboard lives there too.
- Snapshot links returned by MCP tools point at `http://100.107.106.43:8001`.
  Override with `DZ_PUBLIC_URL` in `danger-zone-mcp.service` if you front the
  app with `tailscale serve` as well.

## Storage and retention

`DATA_DIR=/models/danger_zone/data` — the SQLite DB and snapshots live on the
1 TB volume, not the 30 GB root disk.

At `vlm_fps: 0.2` a camera writes one JPEG every 5 s (~50 KB) ≈ 0.9 GB/day.
An hourly scheduler job (`app/services/retention.py`) deletes snapshots older
than `SNAPSHOT_MAX_AGE_DAYS` (7 on this box) and removes empty day folders.

Incidents keep accumulating while the agents are active — every cooldown
window with a person in frame produces one. Flip both agents back to
`{"mode":"passive"}` when you are done demoing.

## Troubleshooting

| Symptom | Check |
|---|---|
| Tile stays empty | `GET /cameras/ingest` — is the source running? `docker logs`/uvicorn log for ffmpeg errors |
| `No snapshots on disk` | `DATA_DIR` must be writable and `INGEST_ENABLED=true` |
| VLM errors | `curl /v1/models` on the vLLM container; `VLM_BASE_URL`/`VLM_API_KEY`/`MODEL_ID` in `.env` |
| Agents never fire | both agents default to passive; flip via the API or dashboard pills |
| ffmpeg missing | `sudo apt install ffmpeg` (needed for `file`/`rtsp`, not `synthetic`) |
| Root disk filling | ensure `DATA_DIR` points at `/models`, and Docker/containerd roots are on the volume |
| `/models` empty after a VM resize | reattach the 1 TB volume, `systemctl stop docker containerd`, `mount /models`, then start Docker — starting Docker first makes it use a root-disk `/models/docker` |
| vLLM OOM after losing GPUs | use the AWQ launcher (`run-235b-awq.sh`, TP2); lower `--gpu-memory-utilization` to 0.85 or `--max-model-len` to 65536 |

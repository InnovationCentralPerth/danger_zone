# Deployment — danger_zone on 4×H100

This box runs a production-shaped deployment of the `danger_zone` PoC with two
changes that matter for a live demo:

1. **Video ingest is source-agnostic.** The app no longer assumes Meraki. A
   camera entry declares a `source:` block (`synthetic`, `file`, `rtsp`), and
   everything downstream — agents, dashboard, incidents — only sees
   `camera.frame` / `camera.snapshot` bus events.
2. **The VLM is local.** Qwen3-VL-235B-A22B-Instruct-FP8 is served by vLLM on
   all four GPUs, so there are no hosted-API tokens, rate limits, or network
   dependency in the demo path.

## What runs where

| Process | Address | Start |
|---|---|---|
| vLLM (Qwen3-VL-235B) | `127.0.0.1:8000/v1` | `~/vlm/run-235b.sh` (container restarts automatically) |
| danger_zone API + dashboard | `0.0.0.0:8001` | `systemctl start danger-zone` (enabled at boot) |
| Ingest tasks | in-process | started by the app lifespan when `INGEST_ENABLED=true` |
| Storage | `/models` | 1 TB volume |

`~/vlm/README.md` covers the model server in detail. The app is a systemd
unit (`/etc/systemd/system/danger-zone.service`) so it survives reboots and
restarts on failure:

```bash
sudo systemctl status danger-zone
sudo systemctl restart danger-zone
journalctl -u danger-zone -f
```

## First run / cold boot

```bash
# 1. Model server (cold start ~10-15 min: 238 GB of weights + torch.compile)
~/vlm/run-235b.sh
sudo docker logs -f vllm-qwen3vl     # wait for "Application startup complete"

# 2. App
sudo systemctl start danger-zone
```

On reboot both come back on their own: the vLLM container has
`--restart unless-stopped`, and `danger-zone.service` is enabled. The app does
not wait for vLLM at startup — it connects lazily on the first VLM call.

Open <http://localhost:8001>. The demo camera appears in the camera dropdown
as `Synthetic Workshop (DEMO-SYNTH-0001)` and the tile refreshes every 5 s.

### Verified on this machine

| Check | Result |
|---|---|
| Synthetic compliant frame | `violation=false`, confidence 0.95, ~1.9 s |
| Synthetic no-hard-hat frame | `violation=true`, confidence 0.95, ~1.2 s |
| Mission parse (`hard hat` + `hi-vis vest`) | both items extracted by the local 235B |
| Live cycle | `PPE_OK` INFO + `PPE_VIOLATION` HIGH incidents with annotated 1280×720 snapshots |

## Config: choosing the video source

Everything is driven by `config/cameras.yaml` (gitignored; local copy already
has the synthetic demo camera). Switch sources by editing the `source:` block —
no code changes.

### Synthetic (default — no camera, no footage)

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
    source:
      type: file
      uri: "data/demo/ppe_clip.mp4"
      loop: true
      speed: 1.0
      fps: 5
      width: 1280
```

Drop the clip in `/models/danger_zone/data/demo/` and use an absolute path, or
a path relative to the repo root.

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

### Keys that control ingest

| Key | Meaning |
|---|---|
| `source.fps` | frames decoded per second (detector rate) |
| `source.width` | output width; height keeps aspect |
| `vlm_fps` | snapshots persisted/published per second — i.e. how often the VLM agents may run |
| `ring_size` | buffered frames per camera (default 16, drop-oldest) |
| `cooldown_seconds` | per-agent cooldown on top of `vlm_fps` |

`INGEST_ENABLED=true` is set in `.env`. Cameras without a `source:` block are
ignored, so a Meraki-only install behaves exactly as before.

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
| `GET /cameras/{serial}/zones` | polygons for the dashboard overlay |

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

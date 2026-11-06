# Expo runbook — danger_zone, Monday 9 November

## The claim

The audience should leave believing two things:

1. **The AI is live.** A 235B vision-language model is analysing frames on the
   local H100s, and the detection target can be changed in plain English while
   the demo is running.
2. **The data stays local.** No frame is sent to a cloud AI provider. The only
   cloud dependency is Meraki's own camera platform, which is the vendor the
   cameras already belong to.

Do **not** claim the video is live when it is a replay. The dashboard now shows
a mode badge (`LIVE`, `CLOUD SNAPSHOT`, `REPLAY`, `SYNTHETIC`) on every tile and
an overlay on the image, so the audience can see which mode is running.

## Architecture reality

| Thing | Where it is | Implication |
|---|---|---|
| H100 VM | Melbourne, provisioned by Cisco | Local vLLM inference |
| Meraki cameras | ICP Perth, `192.168.6.0/24` | Not directly reachable from the VM |
| Laptop | Omarchy Linux, presenter | Drives the dashboard/MCP |
| Network | Internal, not air-gapped | Meraki cloud + Tailscale are allowed |

The current Meraki cloud snapshot path works from Melbourne, but a cold
snapshot takes roughly 9–14 seconds. That is fine for proving the cameras are
real, but it is not the main "near-real-time" experience.

Live RTSP requires a device on the Perth camera VLAN to act as a relay. That is
the stretch goal, not the critical path.

## Mode A — Replay (primary)

This is the reliable main stage. The footage is recorded, the analysis is live.

1. Record or export one clip per camera into
   `/models/danger_zone/data/demo/` (the 1 TB volume — not the repo's 30 GB
   root disk). See the cue sheet.
2. The profile is already created locally as `config/cameras.replay.yaml`
   (gitignored). If it is missing, copy the template:
   ```bash
   cp config/cameras.replay.example.yaml config/cameras.replay.yaml
   ```
3. Edit the `uri:` paths if your filenames differ.
4. Set the profile and restart:
   ```bash
   # .env
   DEMO_PROFILE=replay
   INGEST_ENABLED=true
   ```
5. Check the labels:
   ```bash
   curl -s localhost:8001/cameras/ | python3 -m json.tool
   ```
   Every replay camera should have `"source_type": "file"` and
   `"demo_label": "REPLAY"`.
6. Open the dashboard. Each tile should show the `REPLAY` overlay and an MJPEG
   stream (not a once-per-second still).

### Cue sheet

Record the clip so the mission change is falsifiable. If the footage is all
compliant, changing the mission proves nothing.

| Clip time | What is on screen | Mission to set | Expected verdict |
|---|---|---|---|
| 0:00–2:00 | Worker, no hard hat | “hard hat” | PPE_VIOLATION (hard hat) |
| 2:00–4:00 | Worker with hard hat, no vest | “hard hat + vest” | PPE_VIOLATION (vest) |
| 4:00–6:00 | Worker with hard hat + vest, no goggles | “all three” | PPE_VIOLATION (goggles) |
| 6:00–8:00 | Worker fully compliant | “all three” | PPE_OK (INFO) |
| 8:00–10:00 | Empty workshop / walk-through | “all three” | silent |

The dashboard has one-click presets for **Hard hat**, **Hard hat + vest**,
**All three**, and **Goggles only**.

### Recording guide for the students

- 1920×1080, 25–30 fps, H.264 MP4. 8–15 minutes per camera.
- Static camera, good light, same framing as the live view so the zone
  polygons transfer.
- Walk in and out slowly. Hold each pose for 5–10 seconds.
- Record each missing item as a separate, obvious pass. Do not rely on one
  continuous walk-through.
- Prefer backs and side profiles, or get consent. Face blur is not implemented.
- Keep the clip free of unrelated people and screens with sensitive content.

Normalise an exported clip to a predictable H.264 MP4:

```bash
ffmpeg -i raw.mov -vf "scale=1280:-2,fps=25" \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart \
  /models/danger_zone/data/demo/workshop.mp4
```

Check all three clips with:

```bash
./scripts/check_demo_assets.sh config/cameras.replay.yaml
```

## Mode B — Live cloud snapshots (proof the cameras are real)

Use the normal `config/cameras.yaml` with no `source:` block. The dashboard
tile refreshes from Meraki cloud snapshots every few seconds. Label it
`CLOUD SNAPSHOT` and say: “this is the real camera; Meraki snapshots are
stills, not video, and they are slower than the replay path.”

- Start the analytics watcher on the workshop camera at 5 s.
- Expect 9–14 s cold snapshots and occasional 400s while Meraki uploads.
- Do not use this as the main real-time claim.

## Mode C — Live RTSP relay (stretch)

Only attempt this if a Perth device is on `192.168.6.0/24`.

On the Perth device:

```bash
# 1. Confirm the camera is reachable.
ffprobe -v error -rtsp_transport tcp \
  -show_entries stream=codec_name,width,height,avg_frame_rate \
  rtsp://192.168.6.59:9000/live

# 2. Install go2rtc (single binary) and run it.
#    go2rtc.yaml:
#    streams:
#      workshop: rtsp://192.168.6.59:9000/live
#      demo-area: rtsp://192.168.6.58:9000/live
#      coffee: rtsp://192.168.6.40:9000/live
go2rtc -config go2rtc.yaml
```

Make sure Tailscale is running on the relay. On the VM:

```bash
ffprobe -v error -rtsp_transport tcp \
  rtsp://<relay-tailscale-ip>:8554/workshop
```

If that succeeds, point the camera config at the relay:

```yaml
source:
  type: rtsp
  uri: "rtsp://<relay-tailscale-ip>:8554/workshop"
  fps: 5
  width: 1280
```

Keep the replay profile ready. If Tailscale falls back to a DERP relay, latency
and jitter will rise; switch back without apologising.

## Mode D — Laptop fallback (Omarchy Linux)

Use this only if the VM is unreachable. It runs the same app with a small local
VLM. No cloud AI.

```bash
# App
git clone git@github.com:InnovationCentralPerth/danger_zone.git
cd danger_zone
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"

# Local VLM
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b        # use :3b if the laptop is CPU-only

# .env
VLM_BASE_URL=http://127.0.0.1:11434/v1
VLM_API_KEY=ollama
MODEL_ID=qwen2.5vl:7b
ALLOW_CLOUD_MODELS=false
DEMO_PROFILE=replay
INGEST_ENABLED=true
```

Add the laptop model to `config/models.yaml` with `local: true`, then start the
app. Test the three-item mission before relying on it — small models are much
weaker on goggles.

If Ollama is not ready, use a direct Ethernet or Tailscale link to the VM
instead. Do not pretend an untested fallback exists.

## Mode E — Synthetic (guaranteed)

`config/cameras.synthetic.yaml` runs a scripted workshop scene with no footage
and no camera. It is the last-resort fallback. It still exercises the real
agents and the real VLM.

## Pre-flight checklist (Sunday)

- [ ] `pytest -x` passes and `ruff check .` is clean.
- [ ] Local vLLM responds: `curl -H "Authorization: Bearer $VLM_API_KEY" localhost:8000/v1/models`.
- [ ] App responds: `curl localhost:8001/healthz`.
- [ ] `/cameras/` shows the expected profile and `demo_label` values.
- [ ] Every replay clip plays and the MJPEG tile updates; no frozen frames.
- [ ] Each mission preset parses in under 3 seconds and returns all items.
- [ ] One deliberate violation per PPE item on the cue sheet.
- [ ] `zone_intrusion` is passive, or its polygons are tightened. Full-frame
      exclusion zones fire on every person and create noise.
- [ ] Old incidents cleared (back up `data/app.db` first).
- [ ] Meraki key is read-only and `MERAKI_ALLOW_WRITE_KEY=false`.
- [ ] Laptop can reach the dashboard and MCP; `X-API-Key` works.
- [ ] Synthetic profile starts.
- [ ] Laptop fallback tested, or explicitly dropped from the plan.

## Ten-minute demo click path

1. Open the dashboard. Point out the mode badge.
2. Set mission **Hard hat**. Show the parsed chips.
3. Wait for the violation. Open the snapshot and the reasoning trace.
4. Change to **Hard hat + vest** on the same footage. Show the verdict change.
5. Change to **All three**. Show the goggles violation.
6. Zones in plain English: click **Keep off the bench**, or type your own
   ("nobody may be in the left half of the picture"). Show the red zone chip
   and the incident reasoning. Then type a rule nobody breaks ("nobody at the far
   right-hand end of the bench") and show it stay quiet.
7. Clear the mission. Show the YAML default.
8. Optional: switch to the live cloud camera. Show the real timestamp.
9. Close: local 235B VLM, frames never leave the network, detection target set
   in plain English at run time.

## Zone rules (natural language)

A mission can mix PPE and zone rules in one sentence. The parser splits them;
the zone agent gives the VLM the clean frame and the rule text and files one
INTRUSION incident per violated rule. Zone-only missions silence the PPE agent.
Rules are capped at 3 and 140 characters each. Judgement is by the VLM, so
phrase rules around things visible in the picture ("near the bench", "left half
of the picture"), not coordinates. Setting a new mission replaces the whole
previous one (PPE and zones).

Footage notes: `demo-area.mp4` is two Meraki dashboard exports joined and
cropped to the picture (the export has a dark band on the right). The monitors
in shot show the safety-sentry STOP/GO banner; the zone prompt tells the model
to ignore people on screens.

## Honesty rules

- Keep the `REPLAY` badge visible at all times.
- Say “live analysis of recorded lab footage,” not “live video.”
- Say “no cloud AI,” not “no cloud.” Meraki snapshots are a cloud path.
- If the live path fails, switch to replay. It is the designed fallback, not a
  failure.

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
- [ ] Backup taken to the laptop (see *Backup and restore*) and the archive opens.
- [ ] Poster file copied to the tablet and opens offline (both orientations).
- [ ] AI egress rule in place: `sudo scripts/ai_egress.sh test` says *egress blocked* and the AI answers
      (the rule does not survive a reboot; re-run `sudo scripts/ai_egress.sh apply` if it says NOT ACTIVE).

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

## Scrubbing and the incident scope

- Replay tiles have a pause button and a slider. Drag to jump; a seek shows the
  new picture in ~3 s while playing, faster while paused. **Pause on a violation**
  and the AI analyses that frozen frame once, so you can talk through it without
  the feed filling with duplicates. Scrub while paused to find the moment, then
  press play.
- **Loop a section to retask on the same moment.** Scrub to the start of the
  situation, press **A**, play to the end, press **B**. (Press **B** alone to loop
  the last 10 s.) The section repeats seamlessly with an amber band on the slider.
  Now change the mission: the same footage is re-judged, so the verdict changes
  because the mission changed, not the video. Press **⟲ ✕** to carry on.
  Dragging the slider outside the loop ends it.
- Each incident card is tagged with the mission that produced it, and a
  **▶ New mission** line appears in the feed when you retask. **Clear feed**
  empties the incident and reasoning lists for a clean slate (nothing is deleted;
  earlier incidents stay in the database).
- The incident and reasoning feeds default to **This camera**. All cameras are
  analysed all the time; "All cameras" shows everything, with the camera name on
  each card. Say this out loud if someone asks why a tile that is not on screen
  has incidents.

## The stand: poster for the story, big screen for the demo

Two copies of the **same poster file** do different jobs, and it works out which by where
it was opened:

| Copy | Where | What the last box (09) does |
|---|---|---|
| **Handheld** | Saved file on the roaming tablet (`file://...`), or any copy opened with `#handheld` | "See it live on the big screen": opens a short panel, no network needed. Nothing here can change the live demo. |
| **Big screen** | One tab in the screen's tab cycle, opened from `http://xenterm-wl180:8001/static/poster/safety-sentry-poster.html` | "Live demo": opens the running dashboard in the same tab. The dashboard has a **← Poster** button to come back. After 60 s with nobody touching it the poster returns to the overview. |

Open a copy with `#notes` on the end (once; the browser remembers) and a **Notes** button
appears bottom-right: *Say / Point at / Careful* for whichever tile is open, so colleagues
give the same answers. It is hidden from visitors by default; `#nonotes` turns it off.
Overrides if it guesses wrong: `#handheld` or `#screen`.

**How a walk-through goes** (about 5 minutes; tile order is the story): 01 problem, 02 the
idea, **03 same moment, different mission** (switch Scene to *Woodshop* and tap *All
three* on each frame), 04 how it works, 05 Ask, 06 numbers, 07 lessons, 08 QR. Then:
"the real thing is on the big screen, let me walk you over", and run the cue sheet there.
If time is short: 01, 03, then the big screen.

### Reset the demo (one minute, before each group and whenever it has drifted)

Several people and the presenter's laptop share one live mission, and whoever sets it last
wins. On the big screen:

- [ ] Poster tab is on the overview (it returns by itself after 60 s idle).
- [ ] Dashboard: set the intended mission (e.g. *Hard hat + vest*, or *Woodshop kit* for the
      woodshop cameras), then press **Clear feed** so counts start from now.
- [ ] Camera tile says REPLAY and is playing; scrub bar near the start of the moment you
      want to show.
- [ ] Agents both *active*; status bar AI says *Open-weight VLM · 235B (local, 2×H100)*.
- [ ] Tell the team who is allowed to change the mission right now.

### If a screen freezes or crawls

The server is almost never the cause: check `curl localhost:8001/healthz` and `top` on the VM. If
it answers instantly and the app is near 0% CPU, the *browser tab* is stuck (a tab left open
for many hours can degrade, and one that outlives a server restart runs old page code). Close
the browser completely and reopen it. Dashboard tabs now reload themselves after a server
restart and after 30 idle minutes, but still start the day (and before VIPs arrive) with a
fresh load of each screen: Ctrl-Shift-R.

### Say / don't say (everyone, every time)

- **Say:** "Recorded footage, live analysis." Replay is always badged REPLAY.
- **Say:** Splunk is *next*, not built. Live video from the lab is *next*: the camera video
  stream isn't reachable from the server yet.
- **Say:** the AI runs on our own GPUs in NIIN Sovereign Cloud (not "on-premise"). Camera
  frames and questions don't leave the server (measured in a 13-minute capture on 6 Nov: the
  only outbound traffic was a small anonymous usage report from the serving software, under
  1 KB every 10 minutes).
- **Say** "the AI server is blocked from making outside connections" **only if** today's
  pre-flight `sudo scripts/ai_egress.sh test` passed (see *Data leaving the building*).
  **Don't say** "no outbound connections at all": the rest of the VM does talk out (Meraki
  API, Tailscale), and the serving software tries to send its usage report until it is
  restarted with telemetry off.
- **Which model?** Answer directly, see below. Never deflect.
- **Accuracy:** never say it is perfect. Woodshop PPE: 78 of 81 checks right; clear glasses
  and earmuffs with the head turned away are the misses. Goggles need the person close.
- **Faces** in the footage are consented. No other people's footage is used.

## The poster on the tablet

`poster/dist/safety-sentry-poster.html` is a standalone interactive poster (1.8 MB, works
offline). Copy it to the tablet (USB, Nearby Share, Drive, or open
`http://xenterm-wl180:8001/static/poster/safety-sentry-poster.html#handheld` on the tablet
and save the page) and open it in Chrome or Fully Kiosk. Both orientations work. Tiles to
lead with: **03 Same moment, different mission**, then **05 Ask it anything**. The LinkedIn
QR is in **08**. Everything on it is recorded from the live system; rebuild with
`python poster/record_results.py && python poster/build_poster.py` (see `poster/README.md`).

## "Which model is that?"

On screen the model is described by what matters: **open weights, running on-site**
(status bar, model picker, mission card, poster). The exact identity is one tap away
and never hidden: gear menu → *Model card*, the poster's *Vision AI → Model card*
button, the API (`GET /models/`), and the repo and docs name it plainly.

If someone asks, answer in one calm sentence and don't hedge:

> "It's Qwen3-VL 235B from Alibaba: open weights under Apache 2.0, running on our own
> GPUs in NIIN Sovereign Cloud. The weights are a file of numbers; they can't make a
> network connection, and the AI server has no route to the internet."

Only say the last clause (reword it as "the AI server is blocked from making outside
connections") while `sudo scripts/ai_egress.sh test` passes (see *Data leaving the
building* below). Do not claim anything about the model's
origin that isn't true, and do not deflect. Open weights are the point, and a calm,
direct answer is the best response to the concern.

## Data leaving the building

What was measured, on the VM that hosts the AI (NIIN Sovereign Cloud):

- **Before any change (6 Nov, 13-minute capture on the container's network bridge):** exactly
  one outbound event, at the serving software's 10-minute usage report: a DNS lookup then
  one HTTPS connection to `stats.vllm.ai` (Cloudflare), 919 bytes in total. The local record of
  what it prepares is hardware, CUDA version, a few settings and the model's architecture
  name. No prompts, no images; and 919 bytes is far too small to hold a camera frame.
- **After the firewall rule (7 Nov):** two watches (8 and 9 minutes). Each caught one report
  attempt: 16 DNS packets to 1.1.1.1 / 8.8.8.8, all rejected at the host, so the software
  never got an answer and never opened a connection. A control (rule removed) showed the
  test connections do get out when the rule is off.

Commands (all on the VM, need sudo): `scripts/ai_egress.sh apply | remove | status | test |
check [MIN]`. `test` is the pre-flight item: it must say *egress blocked* and the AI must
answer. The rule is **not persistent**: after a reboot `test` says NOT ACTIVE, so re-run
`apply`.

Still worth doing (restart needed, then re-run `test`): start the vLLM container with
`VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1`, `HF_HUB_OFFLINE=1` (also stops the report attempt
at the source) and publish its port as `127.0.0.1:8000:8000` instead of on all interfaces
(the API needs a key, but nothing else needs to reach it). With the firewall blocking egress,
a restart before those variables are set should still come up, since the model is cached under
`/models/huggingface`, but test it before the expo, not on the day.

## Backup and restore (what git does not hold)

Git has the code and the logos. It does **not** have the replay clips, the joined
reels, `config/cameras*.yaml`, `.env` / `.meraki_key.env`, or the incident database.
Those live only on the VM. Take a copy to your laptop before the expo.

**From the laptop** (one line, ~215 MB, a few seconds; run it anywhere on the tailnet):

```bash
ssh marrabld@xenterm-wl180 'danger_zone/scripts/backup_demo_assets.sh -' > safety-sentry-backup-$(date +%F).tgz
```

Options go after the `-`: `--with-db` adds the incident database (+160 MB),
`--no-secrets` leaves out `.env` and `.meraki_key.env`. Incident snapshots (several GB)
are never included. **The default archive contains API keys: keep it on your laptop,
never in git or a shared drive.**

**Restore** (same VM, or a rebuilt one after `git clone` + `uv sync`):

```bash
scp safety-sentry-backup-*.tgz marrabld@xenterm-wl180:/tmp/
ssh marrabld@xenterm-wl180 'cd danger_zone && scripts/restore_demo_assets.sh /tmp/safety-sentry-backup-*.tgz && sudo systemctl restart danger-zone.service'
```

The restore checks every file against the checksums stored in the archive and exits
non-zero on any mismatch. Clips only, no keys: `rsync -avP
marrabld@xenterm-wl180:/models/danger_zone/data/demo/ ~/safety-sentry-demo/`.

## Ask Safety Sentry

The bar under the incident feed answers plain-English questions from the record
(press `/` to focus it, Esc to close). Good ones for the stage:

- "How many PPE violations since the mission changed?"
- "Are there any people not wearing hard hats?" — if the AI was not checking for
  hard hats it says so and offers **Start watching for hard hat**. This is the
  best moment in the demo: it retasks itself.
- "Which camera has the most violations?"
- "Did anyone go near the bench today?"

Answers show event counts (a person standing still is one event, not eight rows),
evidence thumbnails, and **▶ Jump** buttons that take the replay to that moment.
The default window is since the last mission change or Clear feed, and the answer
says which window it used. Everything is answered by the local model; numbers come
from the database, and an answer with a number the data does not contain is
replaced by a plain template sentence.

Before going on stage: press **Clear feed** after rehearsing so "since the mission
changed" starts clean, and ask each suggested question once to check.

## Zone rules (natural language)

A mission can mix PPE and zone rules in one sentence. The parser splits them;
the zone agent gives the VLM the clean frame and the rule text and files one
INTRUSION incident per violated rule. Zone-only missions silence the PPE agent.
Rules are capped at 3 and 140 characters each. Judgement is by the VLM, so
phrase rules around things visible in the picture ("near the bench", "left half
of the picture"), not coordinates. Setting a new mission replaces the whole
previous one (PPE and zones).

Adding footage: save each Meraki dashboard export as `<camera>-N.mp4` in
`data/demo/` (`demo-area`, `workshop`, `coffee`), then run
`scripts/build_demo_reel.sh <camera> [crop]`. Cut empty stretches with
`data/demo/trims.txt` (`coffee-1.mp4 0 25` lines keep only those seconds).
Short "pass" moments need a short per-camera `cooldown_seconds` (2 s) so the
agent samples them.

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

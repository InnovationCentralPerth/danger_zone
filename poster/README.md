# Safety Sentry — interactive poster

A single standalone HTML file (~1 MB, no network needed) for a tablet at the expo.
Landscape and portrait both work (built for a Galaxy Tab S9, 1280×800 / 800×1280 CSS px).
Every verdict, parsed mission and "Ask" answer in it was **recorded from the running
system**, not mocked; footage is labelled as replayed.

## Rebuild

```bash
python poster/record_results.py   # re-record real results (needs the vLLM server up)
python poster/build_poster.py     # -> poster/dist/safety-sentry-poster.html
```

`build_poster.py` needs `segno` (`uv pip install segno`). The output is gitignored and
reproducible. A copy is also written to `static/poster/`, so while the dashboard is
reachable it is served at `http://<vm>:8001/static/poster/safety-sentry-poster.html`.

The camera wall in tile 1 uses real frames from the three cameras: `python poster/extract_cctv.py`
writes them to `assets/cctv/` from the original exports (needs ffmpeg and the clips on the VM).

To refresh the dashboard screenshot used in "How it works": `poster/capture_dashboard.py`
(see the header of that file for the Playwright setup).

## Put it on the tablet (offline is the safe way)

1. Copy `safety-sentry-poster.html` to the tablet: USB / Nearby Share / Drive, or open the
   URL above on the tablet and save the page. The file keeps working with no internet.
2. Open it with Chrome from the Files app. For a kiosk feel use Fully Kiosk Browser
   (start URL `file:///sdcard/Download/safety-sentry-poster.html`, keep screen on) or turn
   on Android *screen pinning*. The poster also asks the browser to keep the screen awake.
3. Android's Back button returns from a panel to the overview.

## Editing

- Content and layout: `poster/template.html` (plain HTML/CSS/JS, no framework).
- Frames and missions: `MOMENTS` in `record_results.py`; frames live in `assets/frames/`.
  The workshop frame deliberately omits the goggles mission (too small at that distance);
  the "What we learned" panel says so.
- QR target: `LINKEDIN` in `build_poster.py` (the public company page, not the admin URL).

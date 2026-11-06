// danger_zone dashboard — SSE event handler

const evtSource = new EventSource("/events");
let incidentCount = 0;
let cameraSerial = null;
// serial -> {label, sourceType, demoLabel, vlmFps}; populated from /cameras/.
const cameraMeta = new Map();

const severityBadge = {
  HIGH:     'danger',
  CRITICAL: 'danger',
  MEDIUM:   'warning',
  LOW:      'secondary',
  INFO:     'info',
  undefined:'secondary',
};

// Severities that don't bump the red "live incident" counter.
const NON_ALERT_SEVERITIES = new Set(['INFO']);

function prependTo(containerId, html) {
  const el = document.getElementById(containerId);
  // Only clear if the container is still showing its initial placeholder
  // (a direct-child <p>). Hydrated incident cards contain <p> for the summary,
  // so an unscoped selector would wipe the whole list on the first SSE event.
  if (el.querySelector(':scope > p')) el.innerHTML = '';
  el.insertAdjacentHTML('afterbegin', html);
}

// ---- incident card helpers ------------------------------------------------

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function parseExtra(d) {
  if (!d) return {};
  // Live (SSE) incidents carry `extra` as an object; history rows carry `extra_json`.
  if (d.extra && typeof d.extra === 'object') return d.extra;
  if (!d.extra_json) return {};
  try { return JSON.parse(d.extra_json); } catch { return {}; }
}

function renderConfidenceBar(conf) {
  if (typeof conf !== 'number' || conf < 0 || conf > 1) return '';
  const pct = Math.round(conf * 100);
  const tier = conf >= 0.8 ? 'high' : conf >= 0.5 ? 'mid' : 'low';
  return `
    <div class="confidence-row">
      <span class="confidence-label">Confidence</span>
      <div class="confidence-bar-wrap">
        <div class="confidence-bar confidence-bar-${tier}" style="width:${pct}%"></div>
      </div>
      <span class="confidence-pct">${pct}%</span>
    </div>
  `;
}

function openSnapshotModal({ id, type, severity, time, summary }) {
  const title = document.getElementById('snapshotModalTitle');
  const img = document.getElementById('snapshotModalImg');
  const sum = document.getElementById('snapshotModalSummary');
  const api = document.getElementById('snapshotModalApi');
  if (!title || !img || !sum || !api) return false;
  const sevLabel = severity ? ` · ${severity}` : '';
  title.textContent = `Incident #${id} — ${type}${sevLabel} · ${time}`;
  img.src = `/incidents/${id}/snapshot`;
  img.alt = `Incident ${id} snapshot`;
  sum.textContent = summary || '';
  api.href = `/incidents/${id}`;
  // eslint-disable-next-line no-undef
  bootstrap.Modal.getOrCreateInstance(document.getElementById('snapshotModal')).show();
  return true;
}

// ---- camera tile ----------------------------------------------------------

async function loadConfiguredCamera() {
  // Discovery: prefer the live Meraki list (covers cameras not in cameras.yaml),
  // fall back to local YAML config if Meraki is unreachable. We merge both
  // lists so a YAML entry's friendly name beats the bare serial from Meraki.
  let yamlCams = [];
  try {
    const r = await fetch('/cameras/');
    yamlCams = (await r.json()) || [];
  } catch (e) { console.warn('Failed to load /cameras/', e); }

  let merakiCams = [];
  try {
    const r = await fetch('/cameras/devices');
    if (r.ok) merakiCams = (await r.json()) || [];
  } catch (e) { console.warn('Failed to load /cameras/devices', e); }

  // Build the merged camera list: YAML entries first (preserves friendly
  // name + location), then any Meraki-only serials with their device name.
  const yamlBySerial = new Map(yamlCams.map((c) => [c.serial, c]));
  const merged = [];
  for (const c of yamlCams) {
    const meta = {
      serial: c.serial,
      label: c.name || c.serial,
      sourceType: c.source_type || null,
      demoLabel: c.demo_label || null,
      vlmFps: c.vlm_fps ?? null,
    };
    cameraMeta.set(c.serial, meta);
    merged.push(meta);
  }
  for (const d of merakiCams) {
    if (yamlBySerial.has(d.serial)) continue;
    const meta = {
      serial: d.serial,
      label: d.name || d.serial,
      sourceType: null,
      demoLabel: null,
      vlmFps: null,
    };
    cameraMeta.set(d.serial, meta);
    merged.push(meta);
  }

  const select = document.getElementById('camera-select');
  select.innerHTML = '';
  if (merged.length === 0) {
    select.innerHTML = '<option value="">No cameras found</option>';
    select.disabled = true;
    return;
  }
  for (const cam of merged) {
    const opt = document.createElement('option');
    opt.value = cam.serial;
    opt.textContent = cam.label;
    select.appendChild(opt);
  }
  select.disabled = false;
  select.addEventListener('change', (e) => selectCamera(e.target.value));

  // Auto-select first camera. Persist the last choice in localStorage so a
  // reload keeps you on the same camera.
  const remembered = localStorage.getItem('dz.cameraSerial');
  const initial =
    (remembered && merged.find((c) => c.serial === remembered)) ? remembered : merged[0].serial;
  select.value = initial;
  selectCamera(initial);
  relabelCameras();
}

function cameraModeFor(meta) {
  if (!meta) return 'CLOUD SNAPSHOT';
  if (meta.demoLabel) return String(meta.demoLabel).toUpperCase();
  const t = String(meta.sourceType || '').toLowerCase();
  if (t === 'file') return 'REPLAY';
  if (t === 'rtsp') return 'LIVE';
  if (t === 'synthetic') return 'SYNTHETIC';
  return 'CLOUD SNAPSHOT';
}

function setCameraMode(meta) {
  const mode = cameraModeFor(meta);
  const badge = document.getElementById('camera-mode');
  const overlay = document.getElementById('camera-mode-overlay');
  const cls = mode.startsWith('REPLAY')
    ? 'bg-warning text-dark'
    : mode === 'LIVE'
      ? 'bg-success'
      : mode.startsWith('SYNTHETIC')
        ? 'bg-info text-dark'
        : 'bg-secondary';
  if (badge) {
    badge.textContent = mode;
    badge.className = `badge ${cls}`;
  }
  if (overlay) {
    overlay.textContent = mode;
    overlay.className = `camera-mode-overlay ${cls} d-none`;
  }
  // Watcher / Capture-now only mean something for Meraki cloud snapshots.
  const cloud = mode === 'CLOUD SNAPSHOT';
  document.querySelectorAll('.cloud-only').forEach((el) => el.classList.toggle('d-none', !cloud));
  const caption = document.getElementById('camera-caption');
  if (caption) {
    caption.textContent = meta ? meta.label : '';
    caption.classList.toggle('d-none', !meta);
  }
}

function selectCamera(serial) {
  if (!serial) return;
  cameraSerial = serial;
  localStorage.setItem('dz.cameraSerial', serial);
  document.getElementById('camera-serial').textContent = serial;
  setCameraMode(cameraMeta.get(serial));
  // Reset transient camera-tile state when switching.
  const img = document.getElementById('camera-image');
  const empty = document.getElementById('camera-empty');
  const overlay = document.getElementById('zone-overlay');
  const legend = document.getElementById('zone-legend');
  delete img.dataset.streamSerial;
  img.classList.add('d-none');
  overlay.classList.add('d-none');
  legend.classList.add('d-none');
  overlay.innerHTML = '';
  legend.innerHTML = '';
  empty.classList.remove('d-none');
  refreshSnapshot();
  loadZones();
  loadWatcherState();
  applyScope();
  setupPlayback();
}

// ---- replay scrubbing ------------------------------------------------------

const ICON_PAUSE = '<svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><rect x="2" y="1.5" width="3" height="9" rx="0.8"/><rect x="7" y="1.5" width="3" height="9" rx="0.8"/></svg>';
const ICON_PLAY = '<svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="M3 1.5v9l7.5-4.5z"/></svg>';

let playbackTimer = null;
let playbackDragging = false;
let playbackState = null;
let pendingLoopA = null;
let loopStatusShown = false;

function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

function renderPlayback(state) {
  const bar = document.getElementById('playback-bar');
  if (!bar) return;
  if (!state || !state.supported || !state.duration) {
    bar.classList.add('d-none');
    playbackState = null;
    return;
  }
  playbackState = state;
  bar.classList.remove('d-none');
  const slider = document.getElementById('playback-slider');
  slider.max = state.duration;
  if (!playbackDragging) slider.value = state.position;
  document.getElementById('playback-toggle').innerHTML = state.paused ? ICON_PLAY : ICON_PAUSE;
  document.getElementById('playback-time').textContent =
    `${fmtClock(playbackDragging ? slider.value : state.position)} / ${fmtClock(state.duration)}`;

  const band = document.getElementById('loop-band');
  const clearBtn = document.getElementById('loop-clear');
  if (state.loop) {
    const a = (state.loop.start / state.duration) * 100;
    const b = (state.loop.end / state.duration) * 100;
    band.style.left = `${a}%`;
    band.style.width = `${Math.max(b - a, 0.5)}%`;
    band.classList.remove('d-none');
    clearBtn.classList.remove('d-none');
    pendingLoopA = null;
    loopStatusShown = true;
    showLoopHint(`Looping ${fmtClock(state.loop.start)}–${fmtClock(state.loop.end)} — change the mission to re-judge this same moment.`);
  } else {
    band.classList.add('d-none');
    clearBtn.classList.add('d-none');
    if (loopStatusShown) {
      loopStatusShown = false;
      showLoopHint('');
    }
  }
}

function showLoopHint(text) {
  const el = document.getElementById('loop-hint');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('d-none', !text);
}

async function pollPlayback() {
  if (!cameraSerial || playbackDragging) return;
  const serial = cameraSerial;
  try {
    const r = await fetch(`/cameras/${serial}/playback`);
    if (r.ok && serial === cameraSerial) renderPlayback(await r.json());
  } catch (e) { /* transient; next tick retries */ }
}

async function sendPlayback(body) {
  if (!cameraSerial) return;
  try {
    const r = await fetch(`/cameras/${cameraSerial}/playback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      renderPlayback(await r.json());
    } else {
      let detail = `Request failed (${r.status})`;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
      showLoopHint(String(detail));
    }
  } catch (e) { console.error('Playback request failed', e); }
}

function setupPlayback() {
  clearInterval(playbackTimer);
  playbackTimer = null;
  playbackDragging = false;
  pendingLoopA = null;
  showLoopHint('');
  renderPlayback(null);
  const meta = cameraMeta.get(cameraSerial);
  if (!meta || String(meta.sourceType || '').toLowerCase() !== 'file') return;
  pollPlayback();
  playbackTimer = setInterval(pollPlayback, 1000);
}

function initPlaybackControls() {
  const slider = document.getElementById('playback-slider');
  const toggle = document.getElementById('playback-toggle');
  if (!slider || !toggle) return;
  slider.addEventListener('input', () => {
    playbackDragging = true;
    document.getElementById('playback-time').textContent =
      `${fmtClock(slider.value)} / ${fmtClock(playbackState?.duration)}`;
  });
  slider.addEventListener('change', async () => {
    await sendPlayback({ position: parseFloat(slider.value) });
    playbackDragging = false;
  });
  toggle.addEventListener('click', () => {
    sendPlayback({ paused: !(playbackState && playbackState.paused) });
  });
  document.getElementById('loop-a')?.addEventListener('click', () => {
    if (!playbackState) return;
    pendingLoopA = playbackState.position;
    showLoopHint(`Loop start set at ${fmtClock(pendingLoopA)} — play or scrub to the end, then press B.`);
  });
  document.getElementById('loop-b')?.addEventListener('click', () => {
    if (!playbackState) return;
    const end = playbackState.position;
    let start = pendingLoopA !== null ? pendingLoopA : Math.max(0, end - 10);
    let stop = end;
    if (start > stop) [start, stop] = [stop, start];
    pendingLoopA = null;
    sendPlayback({ loop_start: start, loop_end: stop, paused: false });
  });
  document.getElementById('loop-clear')?.addEventListener('click', () => {
    pendingLoopA = null;
    sendPlayback({ clear_loop: true });
  });
  document.getElementById('feed-clear')?.addEventListener('click', clearFeed);
  document.getElementById('scope-camera')?.addEventListener('click', () => setScope('camera'));
  document.getElementById('scope-all')?.addEventListener('click', () => setScope('all'));
  applyScope();
}

async function loadZones() {
  if (!cameraSerial) return;
  const overlay = document.getElementById('zone-overlay');
  const legend = document.getElementById('zone-legend');
  try {
    const r = await fetch(`/cameras/${cameraSerial}/zones`);
    if (!r.ok) return;
    const data = await r.json();
    overlay.innerHTML = '';
    legend.innerHTML = '';
    for (const z of (data.zones || [])) {
      // Whole-frame zones are placeholders; drawing them just tints the picture.
      if (z.full_frame) continue;
      const pts = (z.polygon || [])
        .map(([x, y]) => `${(x * 100).toFixed(2)},${(y * 100).toFixed(2)}`)
        .join(' ');
      if (!pts) continue;
      const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      poly.setAttribute('points', pts);
      poly.setAttribute('fill', z.colour);
      poly.setAttribute('fill-opacity', '0.18');
      poly.setAttribute('stroke', z.colour);
      poly.setAttribute('stroke-width', '0.4');
      poly.setAttribute('vector-effect', 'non-scaling-stroke');
      poly.dataset.zoneId = z.id;
      overlay.appendChild(poly);

      const pill = document.createElement('span');
      pill.className = 'zone-pill me-2';
      pill.style.setProperty('--zone-colour', z.colour);
      const suffix = z.required_item ? ` · requires ${z.required_item}` : '';
      pill.textContent = `${z.label}${suffix}`;
      legend.appendChild(pill);
    }
  } catch (e) {
    console.error('Failed to load zones', e);
  }
}

function refreshSnapshot() {
  if (!cameraSerial) return;
  const img = document.getElementById('camera-image');
  const empty = document.getElementById('camera-empty');
  const modeOverlay = document.getElementById('camera-mode-overlay');
  const meta = cameraMeta.get(cameraSerial) || {};
  const isStream = !!meta.sourceType;

  if (isStream) {
    // MJPEG: only (re)connect when switching cameras. The per-second
    // camera_snapshot SSE event must not reset the stream.
    if (img.dataset.streamSerial === cameraSerial && img.src.includes('/stream.mjpg')) return;
    img.dataset.streamSerial = cameraSerial;
    img.onerror = () => {
      img.classList.add('d-none');
      empty.classList.remove('d-none');
      modeOverlay?.classList.add('d-none');
      const status = document.getElementById('camera-status');
      status.className = 'badge bg-danger';
      status.textContent = 'stream stalled';
    };
    img.onload = () => {
      // Never claim "live" for a recording: the badge follows the camera's mode.
      const mode = cameraModeFor(cameraMeta.get(cameraSerial));
      const status = document.getElementById('camera-status');
      status.className = 'badge bg-success';
      status.textContent = mode === 'LIVE' ? 'live' : mode.startsWith('REPLAY') ? 'playing' : 'streaming';
    };
    img.classList.remove('d-none');
    empty.classList.add('d-none');
    document.getElementById('zone-overlay').classList.remove('d-none');
    document.getElementById('zone-legend').classList.remove('d-none');
    modeOverlay?.classList.remove('d-none');
    img.src = `/cameras/${cameraSerial}/stream.mjpg?ts=${Date.now()}`;
    return;
  }

  // Meraki-cloud snapshot path: poll the newest JPEG on disk.
  img.onerror = null;
  img.onload = null;
  const url = `/cameras/${cameraSerial}/snapshot/latest?ts=${Date.now()}`;
  // Use Image() loader (HEAD probe doesn't work with FastAPI FileResponse).
  const probe = new Image();
  probe.onload = () => {
    img.src = url;
    img.classList.remove('d-none');
    empty.classList.add('d-none');
    document.getElementById('zone-overlay').classList.remove('d-none');
    document.getElementById('zone-legend').classList.remove('d-none');
    modeOverlay?.classList.remove('d-none');
  };
  probe.onerror = () => { /* no snapshot yet — keep placeholder */ };
  probe.src = url;
}

async function captureNow() {
  if (!cameraSerial) return;
  const status = document.getElementById('camera-status');
  status.className = 'badge bg-warning text-dark me-2';
  status.textContent = 'capturing…';
  try {
    const r = await fetch(`/cameras/${cameraSerial}/snapshot`, { method: 'POST' });
    if (r.ok) {
      status.className = 'badge bg-success me-2';
      status.textContent = 'captured';
      refreshSnapshot();
    } else {
      const detail = await r.json().catch(() => ({}));
      status.className = 'badge bg-danger me-2';
      status.textContent = `error: ${detail.detail || r.status}`;
    }
  } catch (e) {
    status.className = 'badge bg-danger me-2';
    status.textContent = 'error';
  }
  setTimeout(() => {
    status.className = 'badge bg-secondary me-2';
    status.textContent = 'idle';
  }, 4000);
}

document.addEventListener('DOMContentLoaded', () => {
  loadConfiguredCamera();
  loadModels();
  loadAgentModes();
  loadIncidents();
  loadMissionContext();
  document.getElementById('snapshot-trigger').addEventListener('click', captureNow);
  document.getElementById('model-select').addEventListener('change', changeModel);
  document.getElementById('watcher-toggle').addEventListener('click', toggleWatcher);
  document.getElementById('mission-set').addEventListener('click', setMission);
  document.getElementById('mission-clear').addEventListener('click', clearMission);
  document.querySelectorAll('.mission-preset').forEach((btn) => {
    btn.addEventListener('click', () => {
      const input = document.getElementById('mission-input');
      input.value = btn.dataset.mission || '';
      setMission();
    });
  });
  document.getElementById('mission-input').addEventListener('keydown', (e) => {
    // Ctrl/Cmd+Enter submits — Enter alone inserts newline as usual
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      setMission();
    }
  });
});

// ---- analytics-gated watcher ----------------------------------------------

let watcherRunning = false;
let watcherDotResetTimer = null;

async function loadWatcherState() {
  if (!cameraSerial) return;
  try {
    const r = await fetch(`/cameras/${cameraSerial}/analytics-watcher`);
    if (!r.ok) return;
    renderWatcherState(await r.json());
  } catch (e) {
    console.error('Failed to load watcher state', e);
  }
}

function renderWatcherState(state) {
  watcherRunning = !!state.running;
  const pill = document.getElementById('watcher-pill');
  const dot = document.getElementById('watcher-dot');
  const label = document.getElementById('watcher-label');
  const intervalInput = document.getElementById('watcher-interval');
  const btn = document.getElementById('watcher-toggle');

  intervalInput.value = state.interval_seconds;
  if (state.running) {
    pill.classList.add('is-running');
    const mins = Math.floor(state.running_for_seconds / 60);
    const secs = state.running_for_seconds % 60;
    label.textContent = `Watcher: on (${mins}m${secs.toString().padStart(2, '0')}s)`;
    btn.textContent = 'Stop';
    btn.classList.remove('btn-outline-warning');
    btn.classList.add('btn-outline-danger');
    dot.className = `auto-dot ${_dotClassForStatus(state.last_status)}`;
  } else {
    pill.classList.remove('is-running');
    label.textContent = state.last_status === 'auto-shutoff' ? 'Watcher: shutoff' : 'Watcher: off';
    btn.textContent = 'Start';
    btn.classList.add('btn-outline-warning');
    btn.classList.remove('btn-outline-danger');
    dot.className = state.last_status === 'auto-shutoff' ? 'auto-dot shutoff' : 'auto-dot off';
  }
}

function _dotClassForStatus(status) {
  if (status === 'person') return 'person';
  if (status === 'quiet') return 'quiet';
  if (status === 'idle') return 'idle';
  if (status === 'auto-shutoff') return 'shutoff';
  if (status && status.startsWith('error')) return 'error';
  if (status && status.startsWith('snapshot-error')) return 'error';
  return '';
}

async function toggleWatcher() {
  if (!cameraSerial) return;
  const interval = parseInt(document.getElementById('watcher-interval').value, 10) || 5;
  const body = { enabled: !watcherRunning, interval_seconds: interval };
  try {
    const r = await fetch(`/cameras/${cameraSerial}/analytics-watcher`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) renderWatcherState(await r.json());
  } catch (e) {
    console.error('Failed to toggle watcher', e);
  }
}

// Re-load state once a second while running, so the running-for clock ticks.
setInterval(() => { if (watcherRunning) loadWatcherState(); }, 1000);

evtSource.addEventListener('analytics_watcher_tick', (e) => {
  const d = JSON.parse(e.data);
  if (cameraSerial && d.serial !== cameraSerial) return;
  const dot = document.getElementById('watcher-dot');
  const cls = _dotClassForStatus(d.status);
  dot.className = `auto-dot ${cls}`;
  if (watcherDotResetTimer) clearTimeout(watcherDotResetTimer);
  // Re-trigger pulse animation for person events
  if (cls === 'person') {
    watcherDotResetTimer = setTimeout(() => {
      dot.classList.remove('person');
      void dot.offsetWidth;
      dot.classList.add('person');
    }, 50);
  }
  if (d.status === 'auto-shutoff') loadWatcherState();
});

// ---- agent active/passive toggles -----------------------------------------

async function loadAgentModes() {
  try {
    const r = await fetch('/agents/modes');
    const modes = await r.json();
    renderAgentModes(modes);
  } catch (e) {
    console.error('Failed to load agent modes', e);
  }
}

function renderAgentModes(modes) {
  const host = document.getElementById('agent-modes');
  host.innerHTML = '';
  for (const m of modes) {
    const active = m.mode === 'active';
    const btn = document.createElement('button');
    btn.className = `btn btn-sm ${active ? 'btn-success' : 'btn-outline-secondary'}`;
    btn.title = `${m.agent}: click to toggle`;
    btn.textContent = `${m.agent}: ${m.mode}`;
    btn.addEventListener('click', () => toggleAgentMode(m.agent, m.mode));
    host.appendChild(btn);
  }
}

async function toggleAgentMode(agent, current) {
  const next = current === 'active' ? 'passive' : 'active';
  try {
    await fetch(`/agents/${agent}/mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: next }),
    });
    await loadAgentModes();
  } catch (e) {
    console.error('Failed to toggle agent mode', e);
  }
}

// ---- mission context (dynamic PPE requirements) --------------------------

async function loadMissionContext() {
  try {
    const r = await fetch('/agents/context');
    if (!r.ok) return;
    renderMissionContext(await r.json());
  } catch (e) {
    console.error('Failed to load mission context', e);
  }
}

function renderMissionContext(state) {
  const statusLabel = document.getElementById('mission-status-label');
  const clearBtn = document.getElementById('mission-clear');
  const chips = document.getElementById('mission-chips');
  const note = document.getElementById('mission-note');
  const meta = document.getElementById('mission-meta');
  const card = document.querySelector('.mission-card');
  if (!chips) return;

  chips.innerHTML = '';
  note.textContent = '';
  meta.textContent = '';

  if (!state || !state.active || !state.context) {
    statusLabel.textContent = '— default (from cameras.yaml)';
    clearBtn.classList.add('d-none');
    card?.classList.remove('mission-card-active');
    const chip = document.createElement('span');
    chip.className = 'badge mission-chip-default';
    chip.textContent = 'YAML default (hard_hat)';
    chips.appendChild(chip);
    return;
  }

  const ctx = state.context;
  const zoneRules = ctx.zone_rules || [];
  const parts = [];
  if (ctx.required_items.length) parts.push(`${ctx.required_items.length} PPE item${ctx.required_items.length === 1 ? '' : 's'}`);
  if (zoneRules.length) parts.push(`${zoneRules.length} zone rule${zoneRules.length === 1 ? '' : 's'}`);
  statusLabel.textContent = `— active (${parts.join(', ')})`;
  clearBtn.classList.remove('d-none');
  card?.classList.add('mission-card-active');

  const sevClass = `mission-chip-sev-${(ctx.severity_hint || 'medium').toLowerCase()}`;
  for (const item of ctx.required_items) {
    const chip = document.createElement('span');
    chip.className = `badge mission-chip ${sevClass}`;
    chip.textContent = item;
    chips.appendChild(chip);
  }
  for (const rule of zoneRules) {
    const chip = document.createElement('span');
    chip.className = 'badge mission-chip mission-chip-zone';
    chip.textContent = `⛔ ${rule}`;
    chips.appendChild(chip);
  }

  if (ctx.context_note) {
    note.textContent = `"${ctx.context_note}"`;
  }

  const parsed = ctx.parsed_at ? new Date(ctx.parsed_at + 'Z').toLocaleString() : '';
  const bits = [];
  if (ctx.model_id) bits.push('parsed by the on-site model');
  if (parsed) bits.push(parsed);
  if (ctx.severity_hint) bits.push(`severity: ${escapeHtml(ctx.severity_hint)}`);
  meta.innerHTML = bits.join(' · ');
}

function setMissionBusy(busy) {
  const btn = document.getElementById('mission-set');
  const label = document.getElementById('mission-set-label');
  const spinner = document.getElementById('mission-set-spinner');
  btn.disabled = busy;
  label.textContent = busy ? 'Parsing…' : 'Set Mission';
  spinner.classList.toggle('d-none', !busy);
}

function showMissionError(msg) {
  const el = document.getElementById('mission-error');
  if (!msg) { el.classList.add('d-none'); el.textContent = ''; return; }
  el.textContent = msg;
  el.classList.remove('d-none');
}

async function setMission() {
  const input = document.getElementById('mission-input');
  const text = (input.value || '').trim();
  showMissionError('');
  if (!text) { showMissionError('Please describe the required PPE.'); return; }

  setMissionBusy(true);
  try {
    const r = await fetch('/agents/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mission: text }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      showMissionError(detail.detail || `HTTP ${r.status}`);
      return;
    }
    const state = await r.json();
    renderMissionContext(state);
    input.value = '';
  } catch (e) {
    console.error('Failed to set mission', e);
    showMissionError('Network error — see console.');
  } finally {
    setMissionBusy(false);
  }
}

async function clearMission() {
  showMissionError('');
  try {
    const r = await fetch('/agents/context', { method: 'DELETE' });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      showMissionError(detail.detail || `HTTP ${r.status}`);
      return;
    }
    renderMissionContext(await r.json());
  } catch (e) {
    console.error('Failed to clear mission', e);
    showMissionError('Network error — see console.');
  }
}

// Live-refresh when another client (or external agent) changes the mission
evtSource.addEventListener('mission_context_changed', (e) => {
  try {
    const state = JSON.parse(e.data);
    renderMissionContext(state);
    addMissionMarker(state);
  } catch (err) { console.error('Bad mission_context_changed payload', err); }
});

// ---- model picker ---------------------------------------------------------

// Exact model identity (the "model card") by model id; the picker itself shows a neutral label.
const modelCards = new Map();
function showModelCard() {
  const sel = document.getElementById('model-select');
  const el = document.getElementById('model-card');
  if (!sel || !el) return;
  const card = modelCards.get(sel.value) || '';
  el.textContent = card ? `Model card: ${card}` : '';
}

async function loadModels() {
  const sel = document.getElementById('model-select');
  try {
    const [modelsR, activeR] = await Promise.all([
      fetch('/models/'),
      fetch('/models/active'),
    ]);
    const models = await modelsR.json();
    const active = (await activeR.json()).id;
    sel.innerHTML = '';
    modelCards.clear();
    for (const m of models) {
      modelCards.set(m.id, m.card || '');
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.label}${m.vision ? '' : ' (text)'}`;
      if (m.id === active) opt.selected = true;
      sel.appendChild(opt);
    }
    showModelCard();
  } catch (e) {
    console.error('Failed to load models', e);
    sel.innerHTML = '<option value="">error</option>';
  }
}

async function changeModel(e) {
  const id = e.target.value;
  if (!id) return;
  showModelCard();
  try {
    await fetch('/models/active', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
  } catch (err) {
    console.error('Failed to switch model', err);
  }
}

// New snapshots pushed via SSE (e.g. from motion handler)
evtSource.addEventListener('camera_snapshot', (e) => {
  const d = JSON.parse(e.data);
  if (cameraSerial && d.serial === cameraSerial) refreshSnapshot();
});

// ---- incident / heartbeat / trace -----------------------------------------

function truncate(text, n) {
  const t = String(text || '');
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

// ---- clear feed ------------------------------------------------------------
// Non-destructive: remember the newest incident id and only show newer ones.

let feedClearedAfterId = parseInt(localStorage.getItem('dz.feedClearedAfterId') || '0', 10) || 0;

async function clearFeed() {
  let newest = feedClearedAfterId;
  try {
    const r = await fetch('/incidents/?limit=1');
    if (r.ok) {
      const rows = await r.json();
      if (Array.isArray(rows) && rows.length) newest = Math.max(newest, rows[0].id || 0);
    }
  } catch (e) { /* fall back to the highest id already on screen */ }
  for (const el of document.querySelectorAll('#incident-list [data-incident-id]')) {
    newest = Math.max(newest, parseInt(el.dataset.incidentId || '0', 10) || 0);
  }
  feedClearedAfterId = newest;
  localStorage.setItem('dz.feedClearedAfterId', String(newest));

  document.getElementById('incident-list').innerHTML =
    '<p class="text-secondary text-center p-4">Feed cleared — watching…</p>';
  document.getElementById('trace-list').innerHTML = '';
  incidentCount = 0;
  document.getElementById('incident-count').textContent = '0';
  const note = document.getElementById('incident-hidden-note');
  if (note) note.classList.add('d-none');
}

function addMissionMarker(state) {
  const active = state && state.active && state.context;
  const text = active ? `New mission: “${truncate(state.context.raw_input, 140)}”` : 'Mission cleared — back to the default';
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  prependTo('incident-list',
    `<div class="mission-marker">▶ ${escapeHtml(text)}<small>${time}</small></div>`);
}

function relabelCameras() {
  for (const el of document.querySelectorAll('[data-camera-id]')) {
    const serial = el.dataset.cameraId;
    if (!serial) continue;
    const tag = el.querySelector('.camera-tag');
    if (tag) tag.textContent = cameraLabel(serial);
  }
}

function cameraLabel(serial) {
  const meta = cameraMeta.get(serial);
  return meta ? meta.label : serial;
}

// ---- incident / trace scope (this camera vs all cameras) -------------------

let incidentScope = localStorage.getItem('dz.incidentScope') === 'all' ? 'all' : 'camera';

function applyScope() {
  let hidden = 0;
  for (const el of document.querySelectorAll('#incident-list [data-camera-id], #trace-list [data-camera-id]')) {
    const other = incidentScope === 'camera' && el.dataset.cameraId && cameraSerial
      && el.dataset.cameraId !== cameraSerial;
    el.classList.toggle('d-none', !!other);
    if (other && el.closest('#incident-list')) hidden++;
  }
  document.getElementById('scope-camera')?.classList.toggle('active', incidentScope === 'camera');
  document.getElementById('scope-all')?.classList.toggle('active', incidentScope === 'all');
  const note = document.getElementById('incident-hidden-note');
  if (note) {
    note.textContent = hidden ? `${hidden} from other cameras hidden` : '';
    note.classList.toggle('d-none', !hidden);
  }
}

function setScope(scope) {
  incidentScope = scope;
  localStorage.setItem('dz.incidentScope', scope);
  applyScope();
}

function renderIncidentCard(d) {
  const isAlert = !NON_ALERT_SEVERITIES.has(d.severity);
  const badge = severityBadge[d.severity] || 'secondary';
  const date = new Date(d.timestamp || d.created_at);
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const day = date.toLocaleDateString([], { day: '2-digit', month: 'short' });
  const demoTag = d.demo ? '<span class="badge bg-secondary ms-1">DEMO</span>' : '';
  const cardClass = isAlert ? `incident-card ${d.severity}` : 'incident-card info-note';
  const cameraId = d.camera_id;
  const clientMac = d.client_mac;
  // The camera is already shown as a tag, so drop the serial from the sentence.
  const summary = escapeHtml(String(d.summary || '').replace(/ on camera [A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}/, ''));
  const type = escapeHtml((d.type || '').replace(/_/g, ' '));
  const sev = escapeHtml(d.severity || '');

  const extra = parseExtra(d);
  const conf = typeof extra.vlm_confidence === 'number' ? extra.vlm_confidence : null;
  const confBar = renderConfidenceBar(conf);

  const thumb = (d.id && d.snapshot_path)
    ? `<a href="/incidents/${d.id}/snapshot"
          class="incident-thumb-link"
          data-incident-id="${d.id}"
          data-incident-type="${type}"
          data-incident-severity="${sev}"
          data-incident-time="${time} · ${day}"
          data-incident-summary="${summary}">
         <img src="/incidents/${d.id}/snapshot" class="incident-thumb" alt="snapshot" loading="lazy"
              onerror="this.parentElement.style.display='none'">
       </a>`
    : '';

  const mission = typeof extra.mission === 'string' ? extra.mission : '';
  const missionTag = mission
    ? `<span class="incident-meta-tag mission-tag" title="${escapeHtml(mission)}">Mission: ${escapeHtml(truncate(mission, 80))}</span>`
    : '';

  const metaTags = [
    cameraId ? `<span class="incident-meta-tag camera-tag">${escapeHtml(cameraLabel(cameraId))}</span>` : '',
    missionTag,
    clientMac ? `<span class="incident-meta-tag">MAC ${escapeHtml(clientMac)}</span>` : '',
  ].filter(Boolean).join('');

  return `
    <div class="${cardClass}" data-incident-id="${d.id || ''}" data-camera-id="${escapeHtml(cameraId || '')}">
      <div class="incident-card-header">
        <div class="incident-card-header-left">
          <span class="badge bg-${badge}">${type}</span>
          <span class="incident-severity-pill incident-severity-${sev}">${sev}</span>
          ${demoTag}
        </div>
        <small class="incident-card-time text-muted">${time} <span class="text-secondary">·</span> ${day}</small>
      </div>
      <div class="incident-card-body">
        ${thumb}
        <div class="incident-card-details">
          <p class="incident-summary">${summary}</p>
          ${confBar}
          ${metaTags ? `<div class="incident-card-meta">${metaTags}</div>` : ''}
        </div>
      </div>
    </div>
  `;
}

async function loadIncidents() {
  try {
    const r = await fetch('/incidents/?limit=60');
    if (!r.ok) return;
    let rows = await r.json();
    if (Array.isArray(rows)) rows = rows.filter((row) => (row.id || 0) > feedClearedAfterId);
    if (!Array.isArray(rows) || rows.length === 0) return;
    const host = document.getElementById('incident-list');
    host.innerHTML = '';
    // Sort newest-first (API already does, but be defensive)
    rows.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    // Bump the live counter for non-INFO rows so the badge reflects reality
    for (const row of rows) {
      if (!NON_ALERT_SEVERITIES.has(row.severity)) incidentCount++;
      host.insertAdjacentHTML('beforeend', renderIncidentCard(row));
    }
    document.getElementById('incident-count').textContent = incidentCount;
    applyScope();
  } catch (e) {
    console.error('Failed to hydrate incidents', e);
  }
}

evtSource.addEventListener('incident_new', (e) => {
  const d = JSON.parse(e.data);
  // Skip duplicates already rendered by the hydrate pass
  if (d.id && document.querySelector(`[data-incident-id="${d.id}"]`)) return;
  if (d.id && d.id <= feedClearedAfterId) return;
  const isAlert = !NON_ALERT_SEVERITIES.has(d.severity);
  if (isAlert) {
    incidentCount++;
    document.getElementById('incident-count').textContent = incidentCount;
  }
  prependTo('incident-list', renderIncidentCard(d));
  applyScope();
  if (isAlert && d.camera_id && d.camera_id === cameraSerial) flashAlert();

  // If incident has a snapshot, refresh the tile
  if (d.snapshot_path && cameraSerial && d.camera_id === cameraSerial) {
    setTimeout(refreshSnapshot, 500);
  }
});

evtSource.addEventListener('heartbeat', (e) => {
  const d = JSON.parse(e.data);
  const banner = document.getElementById('heartbeat-banner');
  banner.className = 'alert alert-info mb-0 py-2';
  banner.innerHTML = `<strong>Agent status:</strong> ${d.message}`;
});

// Agent decisions are written for engineers; show the audience plain English and keep
// the raw text as a tooltip.
function prettyDecision(raw) {
  const t = String(raw || '');
  const pct = (v) => `${Math.round(parseFloat(v) * 100)}%`;
  let m;
  if ((m = t.match(/^silent \(multi\): items=.*?, conf=[\d.]+, in_area=(\d+)/))) {
    return m[1] === '0' ? 'Nobody in view — nothing to report' : `${m[1]} in view · not confident enough to raise an alert`;
  }
  if ((m = t.match(/^PPE_VIOLATION \(multi\) confirmed: (.*) \(conf=([\d.]+)\) -> incident #(\d+)/))) {
    return `Violation confirmed — missing ${m[1]} · ${pct(m[2])} sure → incident #${m[3]}`;
  }
  if ((m = t.match(/^PPE_OK \(multi\): (\d+) person\(s\) compliant with (.*) \(conf=([\d.]+)\)/))) {
    return `${m[1]} compliant with ${m[2]} · ${pct(m[3])} sure`;
  }
  if ((m = t.match(/^ZONE RULE violated \((\d+)\): (.*)/))) {
    return `Zone rule broken: ${m[2].replace(/\(conf=([\d.]+)\)/g, (_, c) => `(${pct(c)} sure)`)}`;
  }
  if (/^cleared: rule \d+ ok/.test(t)) {
    return t.replace(/^cleared: /, 'Zone rules clear — ').replace(/rule (\d+) ok \(conf=([\d.]+)\)/g, (_, n, c) => `rule ${n} (${pct(c)})`);
  }
  return t;
}

evtSource.addEventListener('agent_trace', (e) => {
  const d = JSON.parse(e.data);
  const ts = new Date(d.timestamp).toLocaleTimeString();
  prependTo('trace-list', `
    <div class="trace-entry text-muted" data-camera-id="${escapeHtml(d.serial || '')}">
      ${d.serial ? `<span class="camera-tag me-1">${escapeHtml(cameraLabel(d.serial))}</span>` : ''}
      <span class="text-info fw-bold">${escapeHtml(String(d.agent || '').replace(/_/g, ' '))}</span>
      <span class="ms-1" title="${escapeHtml(d.decision)}">${escapeHtml(prettyDecision(d.decision))}</span>
      <small class="float-end">${ts} · ${d.latency_ms}ms</small>
    </div>
  `);
  applyScope();
  if (d.serial && d.serial === cameraSerial) pulseAiHud(d);
});

// The AI chip on the video: lights up each time an agent finishes judging a frame
// from the camera on screen, with how long the model took.
let aiHudTimer = null;
function pulseAiHud(d) {
  const hud = document.getElementById('ai-hud');
  const text = document.getElementById('ai-hud-text');
  if (!hud || !text) return;
  const secs = typeof d.latency_ms === 'number' ? (d.latency_ms / 1000).toFixed(1) : '';
  text.textContent = `${String(d.agent || 'agent').replace('_', ' ')} · ${secs}s`;
  hud.classList.add('busy');
  clearTimeout(aiHudTimer);
  aiHudTimer = setTimeout(() => {
    hud.classList.remove('busy');
    text.textContent = 'AI watching';
  }, 2200);
}

function flashAlert() {
  const wrap = document.getElementById('camera-image-wrap');
  if (!wrap) return;
  wrap.classList.remove('alert-flash');
  void wrap.offsetWidth; // restart the animation
  wrap.classList.add('alert-flash');
}

// Click-to-modal for incident thumbnails (event delegation — survives
// re-renders and works for hydrated + SSE-streamed cards).
document.addEventListener('click', (e) => {
  const link = e.target.closest('.incident-thumb-link');
  if (!link) return;
  if (typeof bootstrap === 'undefined') return; // graceful fallback to href
  e.preventDefault();
  openSnapshotModal({
    id:       link.dataset.incidentId,
    type:     link.dataset.incidentType,
    severity: link.dataset.incidentSeverity,
    time:     link.dataset.incidentTime,
    summary:  link.dataset.incidentSummary,
  });
});

evtSource.onerror = () => {
  document.getElementById('heartbeat-banner').className = 'alert alert-danger mb-0 py-2';
  document.getElementById('heartbeat-banner').textContent = 'Lost connection to agent stream — retrying…';
};

// ---- Ask Safety Sentry -------------------------------------------------------
// A plain-English question box. The server turns the question into a fixed query over
// the incident record and answers from the facts, with evidence you can click.

const ASK_SUGGESTIONS = [
  'How many PPE violations since the mission changed?',
  'Are there any people not wearing hard hats?',
  'Which camera has the most violations?',
  'What are you watching for right now?',
];
let askBusy = false;

function askEls() {
  return {
    form: document.getElementById('ask-form'),
    input: document.getElementById('ask-input'),
    send: document.getElementById('ask-send'),
    sheet: document.getElementById('ask-sheet'),
    thread: document.getElementById('ask-thread'),
    suggestions: document.getElementById('ask-suggestions'),
  };
}

function openAsk() {
  const { sheet } = askEls();
  sheet?.classList.remove('d-none');
}

function closeAsk() {
  const { sheet, input } = askEls();
  sheet?.classList.add('d-none');
  input?.blur();
}

function renderAskSuggestions() {
  const { suggestions } = askEls();
  if (!suggestions) return;
  suggestions.innerHTML = '<div class="ask-hint">Ask about what Safety Sentry has seen, in plain English:</div>';
  for (const q of ASK_SUGGESTIONS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-chip';
    b.textContent = q;
    b.addEventListener('click', () => sendAsk(q));
    suggestions.appendChild(b);
  }
}

function askEvidenceHtml(e) {
  const label = escapeHtml(String(e.type || '').replace(/_/g, ' '));
  const thumb = e.has_snapshot
    ? `<a href="/incidents/${e.incident_id}/snapshot" class="incident-thumb-link"
          data-incident-id="${e.incident_id}" data-incident-type="${label}" data-incident-severity=""
          data-incident-time="${escapeHtml(e.time)}" data-incident-summary="${escapeHtml(e.summary)}">
         <img src="/incidents/${e.incident_id}/snapshot" class="incident-thumb" alt="Evidence" loading="lazy">
       </a>`
    : '';
  const jump = e.replay_position !== null && e.replay_position !== undefined
    ? `<button type="button" class="btn btn-sm ask-jump" data-camera="${escapeHtml(e.camera_id)}"
               data-pos="${e.replay_position}" title="Jump the replay to this moment">▶ Jump to ${fmtClock(e.replay_position)}</button>`
    : '';
  return `<div class="ask-ev">${thumb}
    <div class="ask-ev-cap"><b>${escapeHtml(e.time)}</b> · ${escapeHtml(truncate(e.camera, 28))}</div>${jump}</div>`;
}

function renderAskAnswer(card, data) {
  const f = data.facts || {};
  const chips = [];
  const asChips = (obj, cls) => {
    const entries = Object.entries(obj || {});
    if (entries.length > 1) {
      for (const [k, v] of entries) chips.push(`<span class="incident-meta-tag ${cls}">${escapeHtml(truncate(k, 44))} · ${v}</span>`);
    }
  };
  asChips(f.by_camera, 'camera-tag');
  asChips(f.by_item, '');
  asChips(f.by_rule, 'mission-tag');

  const actions = (data.actions || []).map((a, i) =>
    `<button type="button" class="btn btn-sm btn-primary ask-action" data-i="${i}">${escapeHtml(a.label)}</button>`).join('');
  const meta = [];
  if (typeof f.events === 'number') meta.push(`${f.events} event${f.events === 1 ? '' : 's'} from ${f.samples} detection${f.samples === 1 ? '' : 's'}`);
  if (f.window && f.window.label) meta.push(escapeHtml(f.window.label));
  meta.push(`${(data.took_ms / 1000).toFixed(1)}s`);

  card.querySelector('.ask-a').classList.remove('thinking');
  card.querySelector('.ask-a').textContent = data.answer;
  card.insertAdjacentHTML('beforeend', `
    ${chips.length ? `<div class="ask-breakdown">${chips.join('')}</div>` : ''}
    ${(data.evidence || []).length ? `<div class="ask-evidence">${data.evidence.map(askEvidenceHtml).join('')}</div>` : ''}
    ${actions ? `<div class="ask-actions">${actions}</div>` : ''}
    <div class="ask-meta">${meta.join(' · ')}</div>`);
  card._actions = data.actions || [];
}

async function sendAsk(question) {
  const { input, send, thread, suggestions } = askEls();
  const q = String(question || '').trim();
  if (!q || askBusy || !thread) return;
  askBusy = true;
  send.disabled = true;
  openAsk();
  suggestions.classList.add('d-none');
  input.value = '';

  const card = document.createElement('div');
  card.className = 'ask-card';
  card.innerHTML = `<div class="ask-q">${escapeHtml(q)}</div>
    <div class="ask-a thinking"><span class="ask-dots"><span></span><span></span><span></span></span> Checking the record…</div>`;
  thread.appendChild(card);
  card.scrollIntoView({ block: 'end', behavior: 'smooth' });

  try {
    const r = await fetch('/assistant/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, feed_cleared_after_id: feedClearedAfterId || null }),
    });
    if (!r.ok) {
      let detail = `Something went wrong (${r.status}).`;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
      throw new Error(typeof detail === 'string' ? detail : 'Something went wrong.');
    }
    renderAskAnswer(card, await r.json());
  } catch (e) {
    const a = card.querySelector('.ask-a');
    a.classList.remove('thinking');
    a.textContent = e.message || 'I couldn’t answer that just now.';
  } finally {
    askBusy = false;
    send.disabled = false;
    card.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }
}

async function jumpToMoment(serial, position) {
  const sel = document.getElementById('camera-select');
  if (sel && sel.value !== serial) {
    sel.value = serial;
    selectCamera(serial);
  }
  const r = await fetch(`/cameras/${serial}/playback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // A few seconds of lead-in so the viewer sees it happen, not just the aftermath.
    body: JSON.stringify({ position: Math.max(0, position - 3), paused: false }),
  });
  if (r.ok) {
    const wrap = document.getElementById('camera-image-wrap');
    wrap.classList.remove('jump-flash');
    void wrap.offsetWidth;
    wrap.classList.add('jump-flash');
    return true;
  }
  return false;
}

function initAssistant() {
  const { form, input, thread } = askEls();
  if (!form || !input) return;
  renderAskSuggestions();

  form.addEventListener('submit', (e) => { e.preventDefault(); sendAsk(input.value); });
  input.addEventListener('focus', openAsk);
  document.getElementById('ask-close')?.addEventListener('click', closeAsk);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAsk();
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
    if (e.key === '/' && !typing) { e.preventDefault(); input.focus(); }
  });

  thread.addEventListener('click', async (e) => {
    const jump = e.target.closest('.ask-jump');
    if (jump) {
      jump.disabled = true;
      const ok = await jumpToMoment(jump.dataset.camera, parseFloat(jump.dataset.pos));
      jump.textContent = ok ? '✓ Jumped — watch the video' : 'Could not jump';
      setTimeout(() => { jump.disabled = false; }, 1500);
      return;
    }
    const act = e.target.closest('.ask-action');
    if (act) {
      const card = act.closest('.ask-card');
      const action = card && card._actions ? card._actions[parseInt(act.dataset.i, 10)] : null;
      if (!action || action.type !== 'set_mission') return;
      act.disabled = true;
      act.textContent = 'Setting mission…';
      try {
        const r = await fetch('/agents/context', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mission: action.mission }),
        });
        act.textContent = r.ok ? '✓ Now watching' : 'Could not set the mission';
        if (!r.ok) act.disabled = false;
      } catch (err) {
        act.textContent = 'Could not set the mission';
        act.disabled = false;
      }
    }
  });
}

// ---- status bar (clock, system, cameras, AI) -------------------------------

function initOpsBar() {
  const dot = document.getElementById('ops-dot');
  const sys = document.getElementById('ops-system');
  if (!dot || !sys) return;

  const setSystem = (ok) => {
    dot.className = `ops-dot ${ok ? 'ok' : 'bad'}`;
    sys.textContent = ok ? 'ONLINE' : 'OFFLINE';
  };
  evtSource.addEventListener('open', () => setSystem(true));
  evtSource.addEventListener('error', () => setSystem(false));

  const clock = (zone) => new Date().toLocaleTimeString('en-AU', {
    timeZone: zone, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const tick = () => {
    document.getElementById('ops-clock-perth').textContent = clock('Australia/Perth');
    document.getElementById('ops-clock-melb').textContent = clock('Australia/Melbourne');
  };
  tick();
  setInterval(tick, 1000);

  const refresh = async () => {
    try {
      const r = await fetch('/cameras/ingest');
      const running = r.ok ? (await r.json()).filter((c) => c.running).length : 0;
      const total = cameraMeta.size || running;
      const modes = new Set([...cameraMeta.values()].map((m) => cameraModeFor(m)));
      const mode = modes.size === 1 ? ` · ${[...modes][0]}` : '';
      document.getElementById('ops-cameras').textContent = total ? `${running}/${total} streaming${mode}` : '–';
    } catch (e) { /* keep last value */ }
    const sel = document.getElementById('model-select');
    const label = sel && sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : '';
    if (label && label !== 'loading…') document.getElementById('ops-ai').textContent = label;
  };
  refresh();
  setInterval(refresh, 5000);
}

// Wire up replay scrubbing, incident-scope buttons and the status bar once everything above is defined.
initPlaybackControls();
initAssistant();
initOpsBar();

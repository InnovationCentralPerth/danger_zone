// danger_zone dashboard — SSE event handler

const evtSource = new EventSource("/events");
let incidentCount = 0;
let cameraSerial = null;

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
  if (!d || !d.extra_json) return {};
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
  try {
    const r = await fetch('/cameras/');
    const cams = await r.json();
    if (cams && cams.length) {
      cameraSerial = cams[0].serial;
      document.getElementById('camera-serial').textContent =
        `${cams[0].name} (${cameraSerial})`;
      refreshSnapshot();  // try to show whatever's on disk
      loadZones();
      loadWatcherState();
    }
  } catch (e) { console.error('Failed to load camera config', e); }
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
  const url = `/cameras/${cameraSerial}/snapshot/latest?ts=${Date.now()}`;
  // Use Image() loader (HEAD probe doesn't work with FastAPI FileResponse).
  const probe = new Image();
  probe.onload = () => {
    img.src = url;
    img.classList.remove('d-none');
    empty.classList.add('d-none');
    document.getElementById('zone-overlay').classList.remove('d-none');
    document.getElementById('zone-legend').classList.remove('d-none');
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
  document.getElementById('snapshot-trigger').addEventListener('click', captureNow);
  document.getElementById('model-select').addEventListener('change', changeModel);
  document.getElementById('watcher-toggle').addEventListener('click', toggleWatcher);
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

// ---- model picker ---------------------------------------------------------

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
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.label}${m.vision ? '' : ' (text)'}`;
      if (m.id === active) opt.selected = true;
      sel.appendChild(opt);
    }
  } catch (e) {
    console.error('Failed to load models', e);
    sel.innerHTML = '<option value="">error</option>';
  }
}

async function changeModel(e) {
  const id = e.target.value;
  if (!id) return;
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
  const summary = escapeHtml(d.summary);
  const type = escapeHtml(d.type || '');
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

  const metaTags = [
    cameraId ? `<span class="incident-meta-tag">Camera ${escapeHtml(cameraId)}</span>` : '',
    clientMac ? `<span class="incident-meta-tag">MAC ${escapeHtml(clientMac)}</span>` : '',
  ].filter(Boolean).join('');

  return `
    <div class="${cardClass}" data-incident-id="${d.id || ''}">
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
    const r = await fetch('/incidents/?limit=20');
    if (!r.ok) return;
    const rows = await r.json();
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
  } catch (e) {
    console.error('Failed to hydrate incidents', e);
  }
}

evtSource.addEventListener('incident_new', (e) => {
  const d = JSON.parse(e.data);
  // Skip duplicates already rendered by the hydrate pass
  if (d.id && document.querySelector(`[data-incident-id="${d.id}"]`)) return;
  const isAlert = !NON_ALERT_SEVERITIES.has(d.severity);
  if (isAlert) {
    incidentCount++;
    document.getElementById('incident-count').textContent = incidentCount;
  }
  prependTo('incident-list', renderIncidentCard(d));

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

evtSource.addEventListener('agent_trace', (e) => {
  const d = JSON.parse(e.data);
  const ts = new Date(d.timestamp).toLocaleTimeString();
  prependTo('trace-list', `
    <div class="trace-entry text-muted">
      <span class="text-info fw-bold">${d.agent}</span>
      <span class="ms-1">${d.decision}</span>
      <small class="float-end">${ts} · ${d.latency_ms}ms</small>
    </div>
  `);
});

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

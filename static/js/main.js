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
  if (el.querySelector('p')) el.innerHTML = '';  // clear placeholder
  el.insertAdjacentHTML('afterbegin', html);
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
      loadAutoCaptureState();
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
  document.getElementById('snapshot-trigger').addEventListener('click', captureNow);
  document.getElementById('model-select').addEventListener('change', changeModel);
  document.getElementById('auto-capture-toggle').addEventListener('click', toggleAutoCapture);
});

// ---- auto-capture loop ----------------------------------------------------

let autoCaptureRunning = false;
let autoDotResetTimer = null;

async function loadAutoCaptureState() {
  if (!cameraSerial) return;
  try {
    const r = await fetch(`/cameras/${cameraSerial}/auto-capture`);
    if (!r.ok) return;
    renderAutoCaptureState(await r.json());
  } catch (e) {
    console.error('Failed to load auto-capture state', e);
  }
}

function renderAutoCaptureState(state) {
  autoCaptureRunning = !!state.running;
  const pill = document.getElementById('auto-capture-pill');
  const dot = document.getElementById('auto-capture-dot');
  const label = document.getElementById('auto-capture-label');
  const intervalInput = document.getElementById('auto-capture-interval');
  const btn = document.getElementById('auto-capture-toggle');

  intervalInput.value = state.interval_seconds;
  if (state.running) {
    pill.classList.add('is-running');
    const mins = Math.floor(state.running_for_seconds / 60);
    const secs = state.running_for_seconds % 60;
    label.textContent = `Auto: on (${mins}m${secs.toString().padStart(2, '0')}s)`;
    btn.textContent = 'Stop';
    btn.classList.remove('btn-outline-warning');
    btn.classList.add('btn-outline-danger');
    dot.className = `auto-dot ${state.last_status === 'ok' ? 'ok' : state.last_status === 'error' ? 'error' : state.last_status === 'idle' ? 'idle' : ''}`;
  } else {
    pill.classList.remove('is-running');
    label.textContent = state.last_status === 'auto-shutoff' ? 'Auto: shutoff' : 'Auto: off';
    btn.textContent = 'Start';
    btn.classList.add('btn-outline-warning');
    btn.classList.remove('btn-outline-danger');
    dot.className = state.last_status === 'auto-shutoff' ? 'auto-dot shutoff' : 'auto-dot off';
  }
}

async function toggleAutoCapture() {
  if (!cameraSerial) return;
  const interval = parseInt(document.getElementById('auto-capture-interval').value, 10) || 5;
  const body = { enabled: !autoCaptureRunning, interval_seconds: interval };
  try {
    const r = await fetch(`/cameras/${cameraSerial}/auto-capture`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) renderAutoCaptureState(await r.json());
  } catch (e) {
    console.error('Failed to toggle auto-capture', e);
  }
}

// Re-load state once a second while running, so the running-for clock ticks.
setInterval(() => { if (autoCaptureRunning) loadAutoCaptureState(); }, 1000);

evtSource.addEventListener('auto_capture_tick', (e) => {
  const d = JSON.parse(e.data);
  if (cameraSerial && d.serial !== cameraSerial) return;
  const dot = document.getElementById('auto-capture-dot');
  const cls = d.status === 'ok' ? 'ok'
            : d.status === 'error' ? 'error'
            : d.status === 'auto-shutoff' ? 'shutoff'
            : d.status === 'idle' ? 'idle'
            : 'off';
  dot.className = `auto-dot ${cls}`;
  if (autoDotResetTimer) clearTimeout(autoDotResetTimer);
  // Re-trigger the pulse animation by re-adding the ok class
  if (cls === 'ok') {
    autoDotResetTimer = setTimeout(() => {
      dot.classList.remove('ok');
      void dot.offsetWidth;
      dot.classList.add('ok');
    }, 50);
  }
  if (d.status === 'auto-shutoff') loadAutoCaptureState();
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

evtSource.addEventListener('incident_new', (e) => {
  const d = JSON.parse(e.data);
  const isAlert = !NON_ALERT_SEVERITIES.has(d.severity);
  if (isAlert) {
    incidentCount++;
    document.getElementById('incident-count').textContent = incidentCount;
  }

  const badge = severityBadge[d.severity] || 'secondary';
  const ts = new Date(d.timestamp).toLocaleTimeString();
  const demoTag = d.demo ? '<span class="badge bg-secondary ms-1">DEMO</span>' : '';
  const cardClass = isAlert ? `incident-card ${d.severity}` : 'incident-card info-note';

  prependTo('incident-list', `
    <div class="${cardClass} bg-dark p-3 mx-2 mt-2 rounded">
      <div class="d-flex justify-content-between align-items-start">
        <span class="badge bg-${badge}">${d.type}</span>
        ${demoTag}
        <small class="text-muted">${ts}</small>
      </div>
      <p class="mb-0 mt-2 small">${d.summary}</p>
      ${d.camera_id ? `<small class="text-muted">Camera: ${d.camera_id}</small>` : ''}
      ${d.client_mac ? `<small class="text-muted">MAC: ${d.client_mac}</small>` : ''}
    </div>
  `);

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

evtSource.onerror = () => {
  document.getElementById('heartbeat-banner').className = 'alert alert-danger mb-0 py-2';
  document.getElementById('heartbeat-banner').textContent = 'Lost connection to agent stream — retrying…';
};

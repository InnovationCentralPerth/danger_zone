// danger_zone dashboard — SSE event handler

const evtSource = new EventSource("/events");
let incidentCount = 0;
let cameraSerial = null;

const severityBadge = {
  HIGH:     'danger',
  CRITICAL: 'danger',
  MEDIUM:   'warning',
  LOW:      'secondary',
  undefined:'secondary',
};

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
    }
  } catch (e) { console.error('Failed to load camera config', e); }
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
  incidentCount++;
  document.getElementById('incident-count').textContent = incidentCount;

  const badge = severityBadge[d.severity] || 'secondary';
  const ts = new Date(d.timestamp).toLocaleTimeString();
  const demoTag = d.demo ? '<span class="badge bg-secondary ms-1">DEMO</span>' : '';

  prependTo('incident-list', `
    <div class="incident-card ${d.severity} bg-dark p-3 mx-2 mt-2 rounded">
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

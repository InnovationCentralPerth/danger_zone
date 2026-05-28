// danger_zone dashboard — SSE event handler

const evtSource = new EventSource("/events");
let incidentCount = 0;

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

// Incident events
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
});

// Heartbeat events
evtSource.addEventListener('heartbeat', (e) => {
  const d = JSON.parse(e.data);
  const banner = document.getElementById('heartbeat-banner');
  banner.className = 'alert alert-info mb-0 py-2';
  banner.innerHTML = `<strong>Agent status:</strong> ${d.message}`;
});

// Agent trace events
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

// Connection status
evtSource.onerror = () => {
  document.getElementById('heartbeat-banner').className = 'alert alert-danger mb-0 py-2';
  document.getElementById('heartbeat-banner').textContent = 'Lost connection to agent stream — retrying…';
};

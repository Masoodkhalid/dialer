'use strict';

// ── Auth guard ─────────────────────────────────────────────────────────────────
const _authToken = localStorage.getItem('dialer_token');
if (!_authToken) { window.location.href = '/login'; }
const _username = localStorage.getItem('dialer_username') || '';
const _navUser  = document.getElementById('nav-user');
if (_navUser) _navUser.textContent = '👤 ' + _username;

function logout() {
  localStorage.clear();
  window.location.href = '/login';
}

const state = {
  agents: {},
  campaigns: {},
  activeCalls: {},
  history: [],
  dids: [],
  currentCampaignId: null,
};

// ── Clock ──────────────────────────────────────────────────────────────────────
function tickClock() {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}
setInterval(tickClock, 1000);
tickClock();

// ── Connection ─────────────────────────────────────────────────────────────────
let _ws = null, _polling = false;

function setStatus(state) {
  const el = document.getElementById('esl-badge');
  if (!el) return;
  const map = {
    online:  ['online',  '● Connected'],
    polling: ['polling', '◌ Polling'],
    offline: ['offline', '● Offline'],
  };
  const [cls, label] = map[state] || map.offline;
  el.className = 'badge-pill ' + cls;
  el.textContent = label;
}

function startWebSocket() {
  const tok = encodeURIComponent(_authToken || '');
  _ws = new WebSocket(`ws://${location.host}/ws?token=${tok}`);
  _ws.onopen    = () => { setStatus('online'); _polling = false; };
  _ws.onmessage = ev => { const {type, data} = JSON.parse(ev.data); handleMsg(type, data); };
  _ws.onclose   = () => {
    setStatus('polling');
    if (!_polling) { _polling = true; pollAll(); setInterval(pollAll, 2000); }
  };
  _ws.onerror = () => _ws.close();
}

async function pollAll() {
  try {
    const [agents, campaigns, calls] = await Promise.all([
      api('GET', '/agents'),
      api('GET', '/campaigns'),
      api('GET', '/calls'),
    ]);
    agents.forEach(a => (state.agents[a.id] = a));
    campaigns.forEach(c => (state.campaigns[c.id] = c));

    const terminal = new Set(['completed', 'failed', 'dropped']);
    state.activeCalls = {};
    calls.forEach(c => {
      if (!terminal.has(c.status)) {
        state.activeCalls[c.id] = c;
      } else if (!state.history.find(h => h.id === c.id)) {
        state.history.unshift(c);
      }
    });
    state.history = state.history.slice(0, 200);
    refreshAll();
    setStatus('online');
  } catch { setStatus('offline'); }
}

function handleMsg(type, data) {
  switch (type) {
    case 'snapshot':
      data.agents.forEach(a => (state.agents[a.id] = a));
      data.campaigns.forEach(c => (state.campaigns[c.id] = c));
      data.active_calls.forEach(c => (state.activeCalls[c.id] = c));
      if (data.history) { state.history = data.history.slice().reverse(); }
      if (data.dids)    { state.dids = data.dids; populateDIDSelect(); }
      refreshAll(); break;

    case 'agent_update':
      state.agents[data.id] = data; renderAgents(); break;

    case 'campaign_started': case 'campaign_paused':
    case 'campaign_resumed': case 'campaign_stopped':
    case 'campaign_completed': case 'campaign_reset':
      if (state.campaigns[data.id]) {
        Object.assign(state.campaigns[data.id], data);
        // Merge nested stats if present
        if (data.stats) state.campaigns[data.id].stats = data.stats;
      }
      populateCampaignSelect();
      if (state.currentCampaignId === data.id) refreshStatsBar();
      break;

    // Live stats push — fires on every dial/answer/drop
    case 'campaign_update':
      if (state.campaigns[data.id]) {
        state.campaigns[data.id].status = data.status;
        if (data.stats) state.campaigns[data.id].stats = data.stats;
      }
      populateCampaignSelect();
      if (state.currentCampaignId === data.id) refreshStatsBar();
      break;

    case 'call_dialing': case 'call_answered': case 'call_bridged':
      state.activeCalls[data.id] = data; renderActiveCalls(); break;

    case 'call_ended':
      delete state.activeCalls[data.id];
      if (['completed','dropped'].includes(data.status)) {
        if (!state.history.find(h => h.id === data.id)) state.history.unshift(data);
        if (state.history.length > 200) state.history.pop();
      }
      renderActiveCalls(); renderHistory();
      if (state.currentCampaignId === data.campaign_id) refreshStatsBar();
      break;

    case 'call_ai_analysis': {
      const h = state.history.find(c => c.id === data.call_id);
      if (h) { h.ai_summary = data.summary; h.ai_sentiment = data.sentiment; renderHistory(); }
      const a = state.activeCalls[data.call_id];
      if (a) { a.ai_summary = data.summary; renderActiveCalls(); }
      break;
    }
  }
}

startWebSocket();

// ── Tabs ───────────────────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

// ── Campaign actions ───────────────────────────────────────────────────────────
async function createCampaign() {
  const name = document.getElementById('camp-name').value.trim();
  if (!name) return alert('Enter a campaign name');
  const res = await api('POST', '/campaigns', { name });
  state.campaigns[res.id] = res;
  populateCampaignSelect();
  document.getElementById('camp-select').value = res.id;
  loadCampaign();
  document.getElementById('camp-name').value = '';
}

function loadCampaign() {
  const id = document.getElementById('camp-select').value;
  state.currentCampaignId = id || null;
  document.getElementById('camp-controls').style.display = id ? '' : 'none';
  document.getElementById('stats-bar').style.display = id ? 'flex' : 'none';
  refreshStatsBar();
}

function refreshStatsBar() {
  const c = state.campaigns[state.currentCampaignId];
  if (!c) return;
  const s = c.stats || {};
  setText('sb-name',    c.name);
  setText('sb-status',  c.status);
  setText('sb-total',   s.contacts_total   ?? 0);
  setText('sb-dialed',  s.contacts_dialed  ?? 0);
  setText('sb-answered',s.calls_answered   ?? 0);
  setText('sb-dropped', s.calls_dropped    ?? 0);
  setText('sb-machine', s.calls_machine    ?? 0);

  const dialed = s.contacts_dialed || 0;
  const total  = s.contacts_total  || 1;
  const pct = Math.min(100, Math.round((dialed / total) * 100));
  document.getElementById('sb-progress').style.width = pct + '%';

  // Show real answer/drop rate — cap at 100% so it's always sane
  const ansRate = dialed > 0 ? Math.min(1, (s.calls_answered || 0) / dialed) : 0;
  const dropRate = dialed > 0 ? Math.min(1, (s.calls_dropped  || 0) / dialed) : 0;
  setText('sb-ansrate',  (ansRate  * 100).toFixed(1) + '%');
  setText('sb-droprate', (dropRate * 100).toFixed(1) + '%');
}

async function uploadContacts(ev) {
  const id = state.currentCampaignId;
  if (!id || !ev.target.files[0]) return;
  const form = new FormData();
  form.append('file', ev.target.files[0]);
  const res = await fetch(`/campaigns/${id}/upload`, { method: 'POST', body: form });
  const data = await res.json();
  alert(`✓ ${data.added} contacts uploaded (total ${data.total})`);
  Object.assign(state.campaigns[id], await api('GET', `/campaigns/${id}`));
  refreshStatsBar();
}

async function startCampaign()  { await campAction('start');  }
async function pauseCampaign()  { await campAction('pause');  }
async function resumeCampaign() { await campAction('resume'); }
async function stopCampaign()   { await campAction('stop');   }

async function resetCampaign() {
  const id = state.currentCampaignId;
  if (!id) return;
  const c = state.campaigns[id];
  const total = c?.stats?.contacts_total ?? 0;
  if (!confirm(`Reset all ${total} contacts as un-dialed and clear stats?\n\nAfter reset, click ▶ Start to dial the same list again.`)) return;
  try {
    const res = await api('POST', `/campaigns/${id}/reset`);
    Object.assign(state.campaigns[id], await api('GET', `/campaigns/${id}`));
    populateCampaignSelect();
    refreshStatsBar();
    // Also clear history entries belonging to this campaign
    state.history = state.history.filter(c => c.campaign_id !== id);
    renderHistory();
    alert(`✓ Reset complete — ${res.contacts} contacts ready to dial again. Click ▶ Start when ready.`);
  } catch (err) {
    alert('Reset failed: ' + err.message);
  }
}

async function campAction(action) {
  const id = state.currentCampaignId;
  if (!id) return;
  await api('POST', `/campaigns/${id}/${action}`);
  Object.assign(state.campaigns[id], await api('GET', `/campaigns/${id}`));
  populateCampaignSelect();
  refreshStatsBar();
}

// ── Hangup ────────────────────────────────────────────────────────────────────
async function hangupCall(callId) {
  try {
    await api('POST', `/calls/${callId}/hangup`);
  } catch(e) {
    alert('Hangup failed: ' + e.message);
  }
}

// ── DID selector ───────────────────────────────────────────────────────────────
function populateDIDSelect() {
  const sel = document.getElementById('quick-did');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">— Select DID (caller ID) —</option>';
  state.dids.filter(d => d.active).forEach(d => {
    const o = document.createElement('option');
    o.value = d.number;
    o.textContent = `${d.number}${d.label ? ' — ' + d.label : ''}`;
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
}

// ── Quick Dial ─────────────────────────────────────────────────────────────────
async function quickDial() {
  const num = document.getElementById('quick-number').value.trim();
  const did = document.getElementById('quick-did')?.value || '';
  const res = document.getElementById('quick-result');
  if (!num) return;
  res.style.color = 'var(--muted)';
  res.textContent = '⏳ Dialing ' + num + '…';
  try {
    const body = { phone: num, name: 'Quick Dial' };
    if (did) body.caller_id = did;
    await api('POST', '/calls/quick-dial', body);
    res.style.color = '#4ade80';
    res.textContent = '✓ Call placed — watch Active Calls tab';
    document.getElementById('quick-number').value = '';
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector('.tab').classList.add('active');
    document.getElementById('tab-active').classList.add('active');
  } catch(e) {
    res.style.color = '#f87171';
    res.textContent = '✗ ' + e.message;
  }
}

// ── Agents ─────────────────────────────────────────────────────────────────────
async function addAgent() {
  const name = document.getElementById('agent-name').value.trim();
  const ext  = document.getElementById('agent-ext').value.trim();
  if (!name || !ext) return alert('Enter name and extension');
  const ag = await api('POST', '/agents', { name, extension: ext });
  state.agents[ag.id] = ag;
  renderAgents();
  document.getElementById('agent-name').value = '';
  document.getElementById('agent-ext').value  = '';
}

async function toggleAgent(id) {
  const a = state.agents[id];
  if (!a) return;
  await api('POST', a.status === 'offline' ? '/agents/login' : '/agents/logout', { agent_id: id });
}

// ── Render ─────────────────────────────────────────────────────────────────────
function refreshAll() {
  populateCampaignSelect();
  renderAgents();
  renderActiveCalls();
  renderHistory();
  if (state.currentCampaignId) refreshStatsBar();
}

function populateCampaignSelect() {
  const sel = document.getElementById('camp-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— select —</option>';
  Object.values(state.campaigns).forEach(c => {
    const o = document.createElement('option');
    o.value = c.id;
    o.textContent = `${c.name} [${c.status}]`;
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
}

function renderAgents() {
  const list = document.getElementById('agent-list');
  list.innerHTML = '';
  const agents = Object.values(state.agents);
  if (!agents.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:16px">No agents added yet</div>';
    return;
  }
  agents.forEach(a => {
    const label = a.status === 'offline' ? 'Login' : 'Logout';
    const el = document.createElement('div');
    el.className = 'agent-card';
    el.innerHTML = `
      <span class="agent-dot dot-${a.status}"></span>
      <div class="agent-info">
        <div class="agent-name">${a.name}</div>
        <div class="agent-meta">Ext ${a.extension} · ${a.status.replace('_',' ')} · ${a.calls_handled} calls</div>
      </div>
      <button class="agent-action" onclick="toggleAgent('${a.id}')">${label}</button>`;
    list.appendChild(el);
  });
}

function renderActiveCalls() {
  const tbody = document.getElementById('active-tbody');
  const empty = document.getElementById('active-empty');
  const calls  = Object.values(state.activeCalls);
  tbody.innerHTML = '';
  empty.classList.toggle('show', calls.length === 0);

  const badge = document.getElementById('active-badge');
  badge.textContent = calls.length;
  badge.classList.toggle('has-items', calls.length > 0);

  calls.forEach(c => {
    const agName = c.agent_id ? (state.agents[c.agent_id]?.name ?? '—') : '—';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span style="font-family:monospace">${c.contact.phone}</span></td>
      <td>${c.contact.name ?? '—'}</td>
      <td><span class="chip chip-${c.status}">${c.status}</span></td>
      <td>${agName}</td>
      <td style="font-variant-numeric:tabular-nums">${liveDuration(c.answer_time)}</td>
      <td>${c.amd_result ?? '—'}</td>
      <td>${c.disposition ?? '—'}</td>
      <td><button class="btn btn-red" style="padding:2px 10px;font-size:11px" onclick="hangupCall('${c.id}')">✕ End</button></td>`;
    tbody.appendChild(tr);
  });
}

function renderHistory() {
  const tbody = document.getElementById('history-tbody');
  const empty = document.getElementById('history-empty');
  empty.classList.toggle('show', state.history.length === 0);

  const badge = document.getElementById('history-badge');
  badge.textContent = state.history.length;
  badge.classList.toggle('has-items', state.history.length > 0);

  tbody.innerHTML = '';
  state.history.slice(0, 100).forEach(c => {
    const sent = c.ai_sentiment ?? '';
    const recHtml = c.recording_path
      ? `<span style="display:inline-flex;gap:4px;align-items:center">
           <button class="rec-btn" onclick="playRecording('${c.recording_path}','${c.contact.phone}')">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
             Play
           </button>
           <a href="/recordings/${c.recording_path}?download=true"
              download="${c.recording_path}"
              class="rec-btn" style="text-decoration:none">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
             Save
           </a>
         </span>`
      : `<span class="no-recording">—</span>`;

    const sipCode = c.sip_code || '—';
    const sipCls  = sipCode === '—' ? '' :
      parseInt(sipCode) >= 500 ? 'chip-failed' :
      parseInt(sipCode) >= 400 ? 'chip-amd_check' : 'chip-bridged';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span style="font-family:monospace">${c.contact.phone}</span></td>
      <td>${c.contact.name ?? '—'}</td>
      <td>${c.duration != null ? c.duration + 's' : '—'}</td>
      <td>${c.disposition ?? '—'}</td>
      <td>${sipCode !== '—' ? `<span class="chip ${sipCls}" style="font-size:9px">${sipCode}</span>` : '—'}</td>
      <td style="font-size:10px;color:var(--muted)">${c.hangup_cause || '—'}</td>
      <td>${sent ? `<span class="chip chip-${sent}">${sent}</span>` : '—'}</td>
      <td>${recHtml}</td>
      <td style="max-width:220px;white-space:normal;font-size:11px;color:var(--muted)">${c.ai_summary ?? '—'}</td>`;
    tbody.appendChild(tr);
  });
}

// ── Recording playback ─────────────────────────────────────────────────────────
let _audioEl = null;
function playRecording(path, phone) {
  if (_audioEl) { _audioEl.pause(); _audioEl = null; }
  _audioEl = new Audio(`/recordings/${path}`);
  _audioEl.play().catch(e => alert('Could not play recording: ' + e.message));
}

// ── Utilities ──────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = {
    method,
    headers: {
      'Content-Type':  'application/json',
      'Authorization': `Bearer ${_authToken}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401) { logout(); return; }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

function liveDuration(startIso) {
  if (!startIso) return '—';
  const s = Math.max(0, Math.floor((Date.now() - new Date(startIso + 'Z').getTime()) / 1000));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2,'0')}`;
}

// Refresh active call durations every second
setInterval(() => {
  if (Object.keys(state.activeCalls).length) renderActiveCalls();
}, 1000);

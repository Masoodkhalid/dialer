'use strict';

// ── Auth guard ─────────────────────────────────────────────────────────────────
const _authToken = localStorage.getItem('dialer_token');
if (!_authToken) { window.location.href = '/login'; }
const _username = localStorage.getItem('dialer_username') || '';
const _role     = localStorage.getItem('dialer_role')     || 'user';
const _isAdmin  = _role === 'superadmin';
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
  const _wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  _ws = new WebSocket(`${_wsProto}//${location.host}/ws?token=${tok}`);
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

    case 'agent_removed':
      delete state.agents[data.agent_id]; renderAgents(); break;

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
      if (type === 'campaign_completed' && state.currentCampaignId === data.id) {
        // Show a non-blocking toast
        _showToast('🏁 Campaign complete! Choose contacts to re-dial below.', 'green');
      }
      break;

    case 'hopper_advanced':
      if (state.campaigns[data.id]) {
        if (data.stats) state.campaigns[data.id].stats = data.stats;
        state.campaigns[data.id].status = 'running';
      }
      if (state.currentCampaignId === data.id) {
        refreshStatsBar();
        _showToast(`📦 Hopper batch ${data.batch}/${data.total} loaded (${data.size} contacts)`, 'purple');
      }
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

    case 'call_failed':
      // call_failed payload is {call_id, reason} — remove from active, don't add to history
      if (data.call_id) delete state.activeCalls[data.call_id];
      renderActiveCalls(); break;

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

// ── Mobile sidebar ─────────────────────────────────────────────────────────────
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const ov = document.getElementById('overlay');
  sb.classList.toggle('open');
  ov.classList.toggle('show');
}
function closeSidebar() {
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('overlay')?.classList.remove('show');
}
function openDialPanel() {
  toggleSidebar();
  // scroll quick-dial section into view inside sidebar
  setTimeout(() => {
    const el = document.getElementById('quick-number');
    if (el) { el.scrollIntoView({ behavior: 'smooth' }); el.focus(); }
  }, 200);
}
function toggleAgentPanel() {
  toggleSidebar();
  setTimeout(() => {
    document.getElementById('agent-list')?.scrollIntoView({ behavior: 'smooth' });
  }, 200);
}
function switchTabMobile(name) {
  // close sidebar if open
  closeSidebar();
  // switch to correct tab
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name)?.classList.add('active');
  // activate the correct desktop tab button
  document.querySelectorAll('.tab').forEach(t => {
    if (t.getAttribute('onclick')?.includes(`'${name}'`)) t.classList.add('active');
  });
}

_applyRoleVisibility();
startWebSocket();

function _applyRoleVisibility() {
  if (_isAdmin) return;   // admins see everything

  // Hide campaign create / upload / control buttons — agents are read-only
  const hide = ['camp-create-row', 'camp-upload-btn', 'camp-btn-group', 'agents-admin-link'];
  hide.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
}

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

  // Hopper batch indicator
  const hopperPill = document.getElementById('sb-hopper-pill');
  const hopperEl   = document.getElementById('sb-hopper');
  if (s.hopper_total && s.hopper_total > 1) {
    hopperPill.style.display = '';
    const rem = s.hopper_remaining ?? 0;
    setText('sb-hopper', `Batch ${s.hopper_batch}/${s.hopper_total} · ${rem} left`);
  } else if (s.hopper_total === 1) {
    // Single batch — just show remaining
    hopperPill.style.display = '';
    setText('sb-hopper', `${s.hopper_remaining ?? 0} remaining`);
  } else {
    hopperPill.style.display = 'none';
  }

  // Show/hide re-dial panel when campaign completes
  const redialPanel = document.getElementById('redial-panel');
  if (redialPanel) {
    const show = (c.status === 'completed');
    redialPanel.style.display = show ? '' : 'none';
    if (show) _buildRedialStats(c, s);
  }
}

function _buildRedialStats(c, s) {
  const el = document.getElementById('redial-stats');
  if (!el) return;
  const total    = s.contacts_total  || 0;
  const answered = s.calls_answered  || 0;
  const machine  = s.calls_machine   || 0;
  const dropped  = s.calls_dropped   || 0;
  const failed   = s.calls_failed    || 0;
  // Estimate no_answer from remainder
  const dialed   = s.contacts_dialed || 0;
  const noAns    = Math.max(0, dialed - answered - machine - dropped - failed);
  el.innerHTML = `
    <span class="redial-stat redial-green">✓ Answered: ${answered}</span>
    <span class="redial-stat redial-amber">📵 No Answer: ${noAns}</span>
    <span class="redial-stat redial-amber">📟 Machine: ${machine}</span>
    <span class="redial-stat redial-red">✕ Dropped: ${dropped}</span>
    <span class="redial-stat redial-red">⚠ Failed: ${failed}</span>
    <span class="redial-stat">Total: ${total}</span>
  `;
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

async function redialCampaign() {
  const id = state.currentCampaignId;
  if (!id) return;
  const flt = document.getElementById('redial-filter').value;
  const res  = document.getElementById('redial-result');
  res.style.color = 'var(--muted)';
  res.textContent = '⏳ Preparing contacts…';
  try {
    const data = await api('POST', `/campaigns/${id}/redial`, { filter: flt });
    res.style.color = 'var(--green)';
    res.textContent = `✓ ${data.reset} contacts reset (${data.total_undialed} ready). Click ▶ Start to dial.`;
    Object.assign(state.campaigns[id], await api('GET', `/campaigns/${id}`));
    populateCampaignSelect();
    refreshStatsBar();
  } catch(e) {
    res.style.color = 'var(--red)';
    res.textContent = '✗ ' + e.message;
  }
}

// ── Toast notifications ────────────────────────────────────────────────────────
let _toastTimer = null;
function _showToast(msg, color = 'green') {
  let el = document.getElementById('toast-msg');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast-msg';
    document.body.appendChild(el);
  }
  el.className = `toast toast-${color} show`;
  el.textContent = msg;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 4000);
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
  let agents = Object.values(state.agents);

  if (!agents.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:16px">No agents yet — <a href="/admin" style="color:var(--purple)">add users in Admin Panel</a></div>';
    return;
  }

  // Non-admin agents only see their own card
  if (!_isAdmin) {
    agents = agents.filter(a => a.name === _username);
    if (!agents.length) {
      list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:16px">No agent profile found for your account.</div>';
      return;
    }
  }

  agents.forEach(a => {
    const el = document.createElement('div');
    el.className = 'agent-card' + (a.name === _username ? ' agent-card-self' : '');

    if (_isAdmin) {
      // Admin: simple login/logout toggle per agent
      const label = a.status === 'offline' ? 'Login' : 'Logout';
      el.innerHTML = `
        <span class="agent-dot dot-${a.status}"></span>
        <div class="agent-info">
          <div class="agent-name">${a.name}</div>
          <div class="agent-meta">Ext ${a.extension} · ${a.status.replace('_',' ')} · ${a.calls_handled} calls</div>
        </div>
        <button class="agent-action" onclick="toggleAgent('${a.id}')">${label}</button>`;
    } else {
      // Own card: richer controls — Login/Logout + Break/Return
      const isOffline = a.status === 'offline';
      const isOnBreak = a.status === 'break';
      const isOnCall  = a.status === 'on_call' || a.status === 'wrap_up';
      el.innerHTML = `
        <span class="agent-dot dot-${a.status}"></span>
        <div class="agent-info" style="flex:1">
          <div class="agent-name">${a.name} <span style="font-size:10px;color:var(--muted);font-weight:400">· Ext ${a.extension}</span></div>
          <div class="agent-meta">${a.status.replace('_',' ')} · ${a.calls_handled} calls today</div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
          ${isOffline
            ? `<button class="agent-action" onclick="toggleAgent('${a.id}')">Login</button>`
            : `<button class="agent-action agent-action-red" onclick="toggleAgent('${a.id}')">Logout</button>`}
          ${(!isOffline && !isOnCall)
            ? (isOnBreak
                ? `<button class="agent-action agent-action-green" onclick="agentReturn('${a.id}')">Return</button>`
                : `<button class="agent-action agent-action-amber" onclick="agentBreak('${a.id}')">Break</button>`)
            : ''}
        </div>`;
    }
    list.appendChild(el);
  });
}

async function agentBreak(id) {
  try { await api('POST', '/agents/break', { agent_id: id }); }
  catch(e) { alert('Could not go on break: ' + e.message); }
}
async function agentReturn(id) {
  try { await api('POST', '/agents/return', { agent_id: id }); }
  catch(e) { alert('Could not return from break: ' + e.message); }
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
           <button class="rec-btn" onclick="downloadRecording('${c.recording_path}')" style="cursor:pointer">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
             Save
           </button>
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
let _audioEl  = null;
let _audioBlobUrl = null;
async function playRecording(path, phone) {
  if (_audioEl) { _audioEl.pause(); _audioEl = null; }
  if (_audioBlobUrl) { URL.revokeObjectURL(_audioBlobUrl); _audioBlobUrl = null; }
  try {
    // fetch with auth token (plain <audio> / new Audio() doesn't send headers)
    const res = await fetch(`/recordings/${path}`, {
      headers: { 'Authorization': `Bearer ${_authToken}` },
    });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const blob = await res.blob();
    _audioBlobUrl = URL.createObjectURL(blob);
    _audioEl = new Audio(_audioBlobUrl);
    _audioEl.onended = () => { URL.revokeObjectURL(_audioBlobUrl); _audioBlobUrl = null; };
    await _audioEl.play();
  } catch (e) {
    alert('Could not play recording: ' + e.message);
  }
}

async function downloadRecording(path) {
  try {
    const res = await fetch(`/recordings/${path}?download=true`, {
      headers: { 'Authorization': `Bearer ${_authToken}` },
    });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = path;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Could not download recording: ' + e.message);
  }
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

'use strict';

// ── Auth guard ─────────────────────────────────────────────────────────────────
const _token = localStorage.getItem('dialer_token');
const _role  = localStorage.getItem('dialer_role');
if (!_token || _role !== 'superadmin') {
  window.location.href = '/login';
}

document.getElementById('adm-user').textContent =
  '👤 ' + (localStorage.getItem('dialer_username') || 'admin');

// ── Clock ──────────────────────────────────────────────────────────────────────
setInterval(() => {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}, 1000);

// ── State ──────────────────────────────────────────────────────────────────────
let _allCalls = [];
let _allUsers = [];
let _allDids  = [];

// ── Sections ───────────────────────────────────────────────────────────────────
function showSection(name, btn) {
  document.querySelectorAll('.adm-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.adm-nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('sec-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'reports') loadReports();
  if (name === 'users')   loadUsers();
  if (name === 'dids')    loadDids();
}

// ── API ────────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = {
    method,
    headers: {
      'Content-Type':  'application/json',
      'Authorization': `Bearer ${_token}`,
    },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401 || res.status === 403) {
    localStorage.clear();
    window.location.href = '/login';
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function logout() {
  localStorage.clear();
  window.location.href = '/login';
}

// ── Reports ────────────────────────────────────────────────────────────────────
async function loadReports() {
  try {
    const [summary, calls] = await Promise.all([
      api('GET', '/admin/reports/summary'),
      api('GET', '/admin/reports/calls'),
    ]);
    _allCalls = calls;
    renderSummary(summary);
    renderSipGrid(summary.sip_codes);
    renderCauseGrid(summary.hangup_causes);
    buildSipFilter(summary.sip_codes);
    renderReportTable(_allCalls);
  } catch (err) {
    console.error('Reports load error:', err);
  }
}

function renderSummary(s) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set('s-total',     s.total);
  set('s-answered',  s.answered);
  set('s-completed', s.completed);
  set('s-dropped',   s.dropped);
  set('s-failed',    s.failed);
}

function sipClass(code) {
  if (!code || code === '—') return 'sip-unk';
  const n = parseInt(code);
  if (n >= 200 && n < 300) return 'sip-2xx';
  if (n >= 400 && n < 500) return 'sip-4xx';
  if (n >= 500)             return 'sip-5xx';
  return 'sip-unk';
}

function sipLabel(code) {
  const map = {
    '200': '200 OK',        '404': '404 Not Found',
    '480': '480 Unavailable','486': '486 Busy',
    '487': '487 Cancelled', '488': '488 Not Acceptable',
    '503': '503 Service Unavailable','408': '408 Timeout',
    '403': '403 Forbidden', '401': '401 Unauthorized',
  };
  return map[code] || (code || '—');
}

function renderSipGrid(codes) {
  const el = document.getElementById('sip-grid');
  const entries = Object.entries(codes).sort((a, b) => b[1] - a[1]);
  if (!entries.length) { el.innerHTML = '<span style="color:var(--muted);font-size:12px">No data yet</span>'; return; }
  el.innerHTML = entries.map(([code, count]) =>
    `<span class="sip-chip ${sipClass(code)}">${sipLabel(code)} <strong>${count}</strong></span>`
  ).join('');
}

function renderCauseGrid(causes) {
  const el = document.getElementById('cause-grid');
  const entries = Object.entries(causes).sort((a, b) => b[1] - a[1]);
  if (!entries.length) { el.innerHTML = '<span style="color:var(--muted);font-size:12px">No data yet</span>'; return; }
  el.innerHTML = entries.map(([cause, count]) =>
    `<span class="sip-chip sip-unk">${cause} <strong>${count}</strong></span>`
  ).join('');
}

function buildSipFilter(codes) {
  const sel = document.getElementById('filter-sip');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All SIP codes</option>';
  Object.keys(codes).sort().forEach(code => {
    const o = document.createElement('option');
    o.value = code; o.textContent = sipLabel(code);
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
}

function renderReportTable(calls) {
  const tbody = document.getElementById('report-tbody');
  tbody.innerHTML = '';
  const sorted = [...calls].sort((a, b) =>
    new Date(b.start_time || 0) - new Date(a.start_time || 0)
  );
  sorted.forEach(c => {
    const dt = c.start_time
      ? new Date(c.start_time + (c.start_time.endsWith('Z') ? '' : 'Z')).toLocaleString()
      : '—';
    const dur  = c.duration != null ? c.duration + 's' : '—';
    const code = c.sip_code || '—';
    const rec  = c.recording_path
      ? `<a href="/recordings/${c.recording_path}?download=true" download
            class="rec-btn" style="text-decoration:none;font-size:10px">⬇ Save</a>`
      : '—';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="white-space:nowrap;font-size:10px">${dt}</td>
      <td style="font-family:monospace">${c.contact?.phone ?? '—'}</td>
      <td>${c.contact?.name ?? '—'}</td>
      <td>${c.campaign_id === 'quick' ? 'Quick Dial' : (c.campaign_id?.slice(0,8) ?? '—')}</td>
      <td style="font-family:monospace;font-size:10px">${c.caller_id || '—'}</td>
      <td>${c.agent_id ? c.agent_id.slice(0,8) : '—'}</td>
      <td>${dur}</td>
      <td><span class="chip chip-${c.status}">${c.status}</span></td>
      <td><span class="sip-chip ${sipClass(code)}" style="padding:2px 8px;font-size:10px">${sipLabel(code)}</span></td>
      <td><span class="cause-chip">${c.hangup_cause || '—'}</span></td>
      <td>${c.disposition || '—'}</td>
      <td>${rec}</td>`;
    tbody.appendChild(tr);
  });
}

function filterTable() {
  const phone = document.getElementById('filter-phone').value.toLowerCase();
  const disp  = document.getElementById('filter-disp').value;
  const sip   = document.getElementById('filter-sip').value;
  const filtered = _allCalls.filter(c => {
    if (phone && !(c.contact?.phone || '').includes(phone)) return false;
    if (disp  && c.disposition !== disp) return false;
    if (sip   && c.sip_code   !== sip)  return false;
    return true;
  });
  renderReportTable(filtered);
}

function exportCSV() {
  const rows = [['DateTime','Phone','Name','CampaignID','DID','AgentID','Duration','Status','SIPCode','HangupCause','Disposition']];
  _allCalls.forEach(c => {
    rows.push([
      c.start_time || '',
      c.contact?.phone || '',
      c.contact?.name || '',
      c.campaign_id || '',
      c.caller_id || '',
      c.agent_id || '',
      c.duration ?? '',
      c.status || '',
      c.sip_code || '',
      c.hangup_cause || '',
      c.disposition || '',
    ]);
  });
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = `calls-${new Date().toISOString().slice(0,10)}.csv`;
  a.click(); URL.revokeObjectURL(url);
}

// ── Users ──────────────────────────────────────────────────────────────────────
async function loadUsers() {
  try {
    _allUsers = await api('GET', '/admin/users');
    renderUsers();
  } catch (err) {
    console.error('Users load error:', err);
  }
}

function renderUsers() {
  const tbody = document.getElementById('user-tbody');
  tbody.innerHTML = '';
  _allUsers.forEach(u => {
    const badge = u.role === 'superadmin'
      ? '<span class="user-badge user-badge-admin">Super Admin</span>'
      : '<span class="user-badge user-badge-user">User</span>';
    const created = u.created_at
      ? new Date(u.created_at + (u.created_at.endsWith('Z') ? '' : 'Z')).toLocaleDateString()
      : '—';
    // Show agent link pill if this user has an extension/agent
    const agentPill = u.extension
      ? `<span style="background:#7c3aed22;color:#a78bfa;border:1px solid #7c3aed55;border-radius:4px;padding:1px 6px;font-size:9px;white-space:nowrap">🎧 Agent Ext ${u.extension}</span>`
      : '<span style="color:var(--muted);font-size:10px">No ext</span>';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${u.username}</strong></td>
      <td>${badge}</td>
      <td>${agentPill}</td>
      <td>${created}</td>
      <td style="display:flex;gap:6px">
        <button class="btn btn-amber" style="padding:3px 10px;font-size:10px"
                onclick="resetPw('${u.username}')">Reset PW</button>
        <button class="btn btn-red" style="padding:3px 10px;font-size:10px"
                onclick="deleteUser('${u.username}')">Delete</button>
      </td>`;
    tbody.appendChild(tr);
  });
}

async function createUser() {
  const username  = document.getElementById('u-username').value.trim();
  const password  = document.getElementById('u-password').value || '1234';
  const extension = document.getElementById('u-ext').value.trim();
  const role      = document.getElementById('u-role').value;
  const msgEl     = document.getElementById('u-msg');
  if (!username) { msgEl.style.color='#f87171'; msgEl.textContent='✗ Username required'; return; }
  try {
    const result = await api('POST', '/admin/users', { username, password, extension: extension || null, role });
    const agentNote = result.agent_id
      ? ` — also registered as dialer agent (Ext ${extension})`
      : '';
    msgEl.style.color = '#4ade80';
    msgEl.textContent = `✓ User "${username}" created (pw: ${password})${agentNote}`;
    document.getElementById('u-username').value = '';
    document.getElementById('u-password').value = '';
    document.getElementById('u-ext').value = '';
    await loadUsers();
  } catch (err) {
    msgEl.style.color = '#f87171';
    msgEl.textContent = '✗ ' + err.message;
  }
}

async function resetPw(username) {
  const pw = prompt(`New password for "${username}" (leave blank for "1234"):`);
  if (pw === null) return;
  try {
    await api('POST', `/admin/users/${username}/reset-password`, { password: pw || '1234' });
    alert(`✓ Password reset for ${username}. New password: ${pw || '1234'}`);
  } catch (err) {
    alert('✗ ' + err.message);
  }
}

async function deleteUser(username) {
  if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/admin/users/${username}`);
    await loadUsers();
  } catch (err) {
    alert('✗ ' + err.message);
  }
}

// ── DIDs ───────────────────────────────────────────────────────────────────────
async function loadDids() {
  try {
    _allDids = await api('GET', '/admin/dids');
    renderDids();
  } catch (err) {
    console.error('DIDs load error:', err);
  }
}

function renderDids() {
  const tbody = document.getElementById('did-tbody');
  tbody.innerHTML = '';
  _allDids.forEach(d => {
    const statusDot = d.active
      ? '<span class="dot-active">● Active</span>'
      : '<span class="dot-inactive">● Inactive</span>';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="did-num">${d.number}</td>
      <td>${d.label || '—'}</td>
      <td>${statusDot}</td>
      <td style="display:flex;gap:6px">
        <button class="btn ${d.active ? 'btn-amber' : 'btn-green'}"
                style="padding:3px 10px;font-size:10px"
                onclick="toggleDid('${d.id}',${!d.active})">
          ${d.active ? 'Deactivate' : 'Activate'}
        </button>
        <button class="btn btn-red" style="padding:3px 10px;font-size:10px"
                onclick="deleteDid('${d.id}','${d.number}')">Delete</button>
      </td>`;
    tbody.appendChild(tr);
  });
}

async function addDid() {
  const number = document.getElementById('d-number').value.trim();
  const label  = document.getElementById('d-label').value.trim();
  const msgEl  = document.getElementById('d-msg');
  if (!number) { msgEl.style.color='#f87171'; msgEl.textContent='✗ Number required'; return; }
  try {
    await api('POST', '/admin/dids', { number, label });
    msgEl.style.color = '#4ade80';
    msgEl.textContent = `✓ DID ${number} added`;
    document.getElementById('d-number').value = '';
    document.getElementById('d-label').value  = '';
    await loadDids();
  } catch (err) {
    msgEl.style.color = '#f87171';
    msgEl.textContent = '✗ ' + err.message;
  }
}

async function toggleDid(id, active) {
  try {
    await api('PATCH', `/admin/dids/${id}`, { active });
    await loadDids();
  } catch (err) {
    alert('✗ ' + err.message);
  }
}

async function deleteDid(id, number) {
  if (!confirm(`Delete DID ${number}?`)) return;
  try {
    await api('DELETE', `/admin/dids/${id}`);
    await loadDids();
  } catch (err) {
    alert('✗ ' + err.message);
  }
}

// ── Init ───────────────────────────────────────────────────────────────────────
loadReports();

'use strict';

// ── Auth guard ─────────────────────────────────────────────────────────────────
const _token = localStorage.getItem('dialer_token');
const _role  = localStorage.getItem('dialer_role');
if (!_token || _role !== 'superadmin') {
  window.location.href = '/login';
}

document.getElementById('adm-user').textContent =
  localStorage.getItem('dialer_username') || 'admin';

function toggleAdmSidebar() {
  document.getElementById('adm-sidebar')?.classList.toggle('open');
  document.getElementById('overlay')?.classList.toggle('show');
}
function closeAdmSidebar() {
  document.getElementById('adm-sidebar')?.classList.remove('open');
  document.getElementById('overlay')?.classList.remove('show');
}

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
  closeAdmSidebar();
  if (name === 'reports')     loadReports();
  if (name === 'users')       loadUsers();
  if (name === 'dids')        loadDids();
  if (name === 'subscribers') loadSubscribers();
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
    renderBars('sip-bars',   summary.sip_codes,    sipBarColor);
    renderBars('cause-bars', summary.hangup_causes, causeBarColor);
    renderBars('disp-bars',  summary.dispositions,  () => 'bar-purple');
    renderAgentPerf(summary.agent_performance);
    buildDropdowns(calls, summary);
    filterTable();
  } catch (err) {
    console.error('Reports load error:', err);
  }
}

// ── Formatters ─────────────────────────────────────────────────────────────────
function fmtDur(sec) {
  if (!sec && sec !== 0) return '—';
  if (sec < 60)  return sec + 's';
  return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
}

function sipLabel(code) {
  const map = {
    '200':'200 OK', '180':'180 Ringing', '183':'183 Progress',
    '404':'404 Not Found', '480':'480 Unavailable', '486':'486 Busy',
    '487':'487 Cancelled', '488':'488 Not Acceptable',
    '503':'503 Unavailable', '408':'408 Timeout',
    '403':'403 Forbidden', '401':'401 Unauthorized',
    '500':'500 Server Error', '603':'603 Declined',
  };
  return map[code] || (code && code !== '—' ? code : '—');
}

function sipBarColor(code) {
  if (!code || code === '—') return 'bar-gray';
  const n = parseInt(code);
  if (n >= 200 && n < 300) return 'bar-green';
  if (n >= 400 && n < 500) return 'bar-amber';
  if (n >= 500)             return 'bar-red';
  return 'bar-gray';
}

function causeBarColor(cause) {
  if (!cause || cause === '—') return 'bar-gray';
  if (cause === 'NORMAL_CLEARING')                              return 'bar-green';
  if (cause.includes('TIMEOUT') || cause.includes('NO_ANSWER')) return 'bar-amber';
  if (cause.includes('BUSY') || cause.includes('REJECT'))       return 'bar-red';
  return 'bar-purple';
}

// ── KPI Cards ──────────────────────────────────────────────────────────────────
function renderSummary(s) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
  set('s-total',     s.total);
  set('s-answered',  s.answered);
  set('s-completed', s.completed);
  set('s-dropped',   s.dropped);
  set('s-failed',    s.failed);
  set('s-ans-rate',  s.answer_rate != null ? s.answer_rate + '%' : '—');
  set('s-avg-dur',   fmtDur(s.avg_duration));
  const connRate = s.total > 0 ? ((s.answered / s.total) * 100).toFixed(1) + '%' : '—';
  set('s-conn-rate', connRate);
}

// ── Bar Charts ─────────────────────────────────────────────────────────────────
function renderBars(containerId, data, colorFn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = Object.entries(data || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 14);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!entries.length || total === 0) {
    el.innerHTML = '<span style="color:var(--muted);font-size:12px">No data yet</span>';
    return;
  }
  el.innerHTML = entries.map(([key, count]) => {
    const pct    = ((count / total) * 100).toFixed(1);
    const barPct = Math.max(2, (count / total) * 100);
    const cls    = colorFn ? colorFn(key) : 'bar-purple';
    const label  = containerId === 'sip-bars' ? sipLabel(key) : key;
    return `<div class="bar-row">
      <span class="bar-label" title="${key}">${label}</span>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${barPct}%"></div></div>
      <span class="bar-count">${count}<span class="bar-pct"> (${pct}%)</span></span>
    </div>`;
  }).join('');
}

// ── Agent Performance Table ────────────────────────────────────────────────────
function renderAgentPerf(agents) {
  const el = document.getElementById('agent-perf-tbody');
  if (!el) return;
  if (!agents || !agents.length) {
    el.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted)">No agent data yet</td></tr>';
    return;
  }
  el.innerHTML = agents.map(a => {
    const connRate = a.calls > 0 ? ((a.answered / a.calls) * 100).toFixed(0) + '%' : '—';
    return `<tr>
      <td><strong>${a.name}</strong></td>
      <td style="text-align:center">${a.calls}</td>
      <td style="text-align:center">
        <span style="color:var(--green)">${a.answered}</span>
        <span style="color:var(--muted);font-size:10px"> (${connRate})</span>
      </td>
      <td style="text-align:center">${fmtDur(a.avg_duration)}</td>
    </tr>`;
  }).join('');
}

// ── Populate filter dropdowns from loaded data ─────────────────────────────────
function buildDropdowns(calls, summary) {
  // SIP codes
  const sipSel = document.getElementById('filter-sip');
  const curSip = sipSel.value;
  sipSel.innerHTML = '<option value="">All SIP codes</option>';
  Object.keys(summary.sip_codes || {}).sort().forEach(code => {
    const o = document.createElement('option');
    o.value = code; o.textContent = sipLabel(code);
    sipSel.appendChild(o);
  });
  if (curSip) sipSel.value = curSip;

  // Hangup causes
  const causeSel = document.getElementById('filter-cause');
  const curCause = causeSel.value;
  causeSel.innerHTML = '<option value="">All hangup causes</option>';
  Object.keys(summary.hangup_causes || {}).sort().forEach(c => {
    if (c === '—') return;
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    causeSel.appendChild(o);
  });
  if (curCause) causeSel.value = curCause;

  // Dispositions
  const dispSel = document.getElementById('filter-disp');
  const curDisp = dispSel.value;
  dispSel.innerHTML = '<option value="">All dispositions</option>';
  Object.keys(summary.dispositions || {}).sort().forEach(d => {
    if (d === '—') return;
    const o = document.createElement('option');
    o.value = d; o.textContent = d;
    dispSel.appendChild(o);
  });
  if (curDisp) dispSel.value = curDisp;

  // Agents
  const agentSel = document.getElementById('filter-agent');
  const curAgent = agentSel.value;
  agentSel.innerHTML = '<option value="">All agents</option>';
  const agentNames = [...new Set(calls.map(c => c.agent_name).filter(Boolean))].sort();
  agentNames.forEach(name => {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    agentSel.appendChild(o);
  });
  if (curAgent) agentSel.value = curAgent;

  // Campaigns
  const campSel = document.getElementById('filter-campaign');
  const curCamp = campSel.value;
  campSel.innerHTML = '<option value="">All campaigns</option>';
  const campNames = [...new Set(calls.map(c => c.campaign_name).filter(Boolean))].sort();
  campNames.forEach(name => {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    campSel.appendChild(o);
  });
  if (curCamp) campSel.value = curCamp;
}

// ── Call Log ───────────────────────────────────────────────────────────────────
function filterTable() {
  const search   = (document.getElementById('filter-search')?.value   || '').toLowerCase();
  const agent    = document.getElementById('filter-agent')?.value    || '';
  const campaign = document.getElementById('filter-campaign')?.value || '';
  const status   = document.getElementById('filter-status')?.value   || '';
  const sip      = document.getElementById('filter-sip')?.value      || '';
  const cause    = document.getElementById('filter-cause')?.value    || '';
  const disp     = document.getElementById('filter-disp')?.value     || '';
  const dateFrom = document.getElementById('filter-date-from')?.value || '';
  const dateTo   = document.getElementById('filter-date-to')?.value   || '';

  const filtered = _allCalls.filter(c => {
    if (search   && !(c.contact?.phone || '').includes(search)
                 && !(c.contact?.name  || '').toLowerCase().includes(search)) return false;
    if (agent    && c.agent_name    !== agent)    return false;
    if (campaign && c.campaign_name !== campaign) return false;
    if (status   && c.status        !== status)   return false;
    if (sip      && c.sip_code      !== sip)      return false;
    if (cause    && c.hangup_cause  !== cause)    return false;
    if (disp     && c.disposition   !== disp)     return false;
    if (dateFrom) {
      const dt = c.start_time ? new Date(c.start_time + (c.start_time.endsWith('Z') ? '' : 'Z')) : null;
      if (!dt || dt < new Date(dateFrom)) return false;
    }
    if (dateTo) {
      const dt = c.start_time ? new Date(c.start_time + (c.start_time.endsWith('Z') ? '' : 'Z')) : null;
      if (!dt || dt > new Date(dateTo + 'T23:59:59Z')) return false;
    }
    return true;
  });

  const countEl = document.getElementById('log-count');
  if (countEl) countEl.textContent = `${filtered.length} of ${_allCalls.length} calls`;

  renderReportTable(filtered);
}

function clearFilters() {
  ['filter-search','filter-agent','filter-campaign','filter-status',
   'filter-sip','filter-cause','filter-disp','filter-date-from','filter-date-to']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
  filterTable();
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
    const code    = c.sip_code || '—';
    const sipCls  = sipBarColor(code) === 'bar-green' ? 'sip-2xx' : sipBarColor(code) === 'bar-amber' ? 'sip-4xx' : sipBarColor(code) === 'bar-red' ? 'sip-5xx' : 'sip-unk';
    const recHtml = c.recording_path
      ? `<a href="/recordings/${c.recording_path}?download=true" download
            class="rec-btn" style="text-decoration:none;font-size:10px">⬇ Save</a>`
      : '—';
    const sentCls  = c.ai_sentiment === 'positive' ? 'sip-2xx' : c.ai_sentiment === 'negative' ? 'sip-5xx' : 'sip-unk';
    const rowStyle = c.status === 'completed' ? 'background:rgba(74,222,128,.04)'
                   : c.status === 'failed'    ? 'background:rgba(248,113,113,.04)'
                   : c.status === 'dropped'   ? 'background:rgba(251,191,36,.04)' : '';
    const tr = document.createElement('tr');
    tr.style.cssText = rowStyle;
    tr.innerHTML = `
      <td style="white-space:nowrap;font-size:10px;color:var(--muted)">${dt}</td>
      <td style="font-family:monospace;font-weight:600">${c.contact?.phone ?? '—'}</td>
      <td>${c.contact?.name ?? '—'}</td>
      <td><span style="font-size:10px">${c.campaign_name || '—'}</span></td>
      <td><span style="font-size:10px;color:var(--purple-l)">${c.agent_name || '—'}</span></td>
      <td style="font-family:monospace;font-size:10px">${c.caller_id || '—'}</td>
      <td style="font-variant-numeric:tabular-nums">${fmtDur(c.duration)}</td>
      <td><span class="chip chip-${c.status}" style="font-size:9px">${c.status}</span></td>
      <td><span class="sip-chip ${sipCls}" style="padding:2px 6px;font-size:10px">${sipLabel(code)}</span></td>
      <td><span class="cause-chip" style="font-size:9px">${c.hangup_cause || '—'}</span></td>
      <td style="font-size:10px">${c.amd_result || '—'}</td>
      <td style="font-size:10px">${c.disposition || '—'}</td>
      <td>${c.ai_sentiment ? `<span class="sip-chip ${sentCls}" style="padding:2px 6px;font-size:9px">${c.ai_sentiment}</span>` : '—'}</td>
      <td>${recHtml}</td>`;
    tbody.appendChild(tr);
  });
}

function exportCSV() {
  const rows = [['DateTime','Phone','Name','Campaign','Agent','DID','Duration(s)','Status','SIPCode','SIPLabel','HangupCause','AMD','Disposition','Sentiment','AISummary']];
  _allCalls.forEach(c => {
    rows.push([
      c.start_time || '',
      c.contact?.phone || '',
      c.contact?.name  || '',
      c.campaign_name  || c.campaign_id || '',
      c.agent_name     || c.agent_id    || '',
      c.caller_id      || '',
      c.duration       ?? '',
      c.status         || '',
      c.sip_code       || '',
      sipLabel(c.sip_code) || '',
      c.hangup_cause   || '',
      c.amd_result     || '',
      c.disposition    || '',
      c.ai_sentiment   || '',
      (c.ai_summary    || '').replace(/\n/g, ' '),
    ]);
  });
  const csv  = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = `calls-${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
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
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn btn-amber" style="padding:3px 10px;font-size:10px"
                onclick="resetPw('${u.username}')">Reset PW</button>
        ${u.extension ? `<button class="btn btn-purple" style="padding:3px 10px;font-size:10px"
                onclick="resetSipPw('${u.username}')">SIP PW</button>` : ''}
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

async function resetSipPw(username) {
  const pw = prompt(
    `Set SIP password for "${username}".\n\n` +
    `This is the password used by the WebPhone to register with FreeSWITCH.\n` +
    `It must match what is configured in FreeSWITCH's user directory.\n\n` +
    `Leave blank to use "1234":`
  );
  if (pw === null) return;
  try {
    await api('POST', `/admin/users/${username}/reset-sip-password`, { password: pw || '1234' });
    alert(`✓ SIP password set for ${username}.`);
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
    const forSaleBadge = d.for_sale
      ? '<span style="background:rgba(16,185,129,.15);color:#6ee7b7;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">Listed</span>'
      : '<span style="background:rgba(71,85,105,.2);color:#94a3b8;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">Not Listed</span>';
    const owner = d.owner_username
      ? `<span style="color:var(--purple-l);font-size:10px">👤 ${d.owner_username}</span>`
      : '';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="did-num" style="font-family:monospace">${d.number}</td>
      <td>${d.label || '—'}</td>
      <td>${statusDot} ${owner}</td>
      <td>${forSaleBadge}</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn ${d.active ? 'btn-amber' : 'btn-green'}"
                style="padding:3px 10px;font-size:10px"
                onclick="toggleDid('${d.id}',${!d.active})">
          ${d.active ? 'Deactivate' : 'Activate'}
        </button>
        <button class="btn ${d.for_sale ? 'btn-red' : 'btn-purple'}"
                style="padding:3px 10px;font-size:10px"
                onclick="toggleForSale('${d.id}',${!d.for_sale})">
          ${d.for_sale ? '✕ Delist' : '+ List for Sale'}
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

async function toggleForSale(id, forSale) {
  try {
    await api('PATCH', `/admin/dids/${id}`, { for_sale: forSale });
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

// ── Subscribers ────────────────────────────────────────────────────────────────
let _allSubscribers = [];

async function loadSubscribers() {
  const tbody = document.getElementById('sub-tbody');
  tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted)">Loading…</td></tr>';
  try {
    _allSubscribers = await api('GET', '/admin/subscribers');
    renderSubscribers(_allSubscribers);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;color:var(--red-l)">${err.message}</td></tr>`;
  }
}

function renderSubscribers(rows) {
  const tbody  = document.getElementById('sub-tbody');
  const total  = rows.length;
  const active = rows.filter(r => r.has_subscription && r.subscription?.is_active).length;
  const cancelled = rows.filter(r => r.cancelled_at).length;
  const dids   = rows.filter(r => r.did_number).length;

  document.getElementById('sub-total').textContent     = total;
  document.getElementById('sub-active').textContent    = active;
  document.getElementById('sub-cancelled').textContent = cancelled;
  document.getElementById('sub-dids').textContent      = dids;
  document.getElementById('sub-count').textContent     = `${total} users`;

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted)">No subscribers found</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const sub       = r.subscription;
    const isActive  = sub?.is_active;
    const statusBadge = isActive
      ? '<span style="background:rgba(16,185,129,.15);color:#6ee7b7;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">ACTIVE</span>'
      : r.did_number
        ? '<span style="background:rgba(239,68,68,.15);color:#fca5a5;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">CANCELLED</span>'
        : '<span style="background:rgba(71,85,105,.2);color:#94a3b8;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">NO PLAN</span>';

    const minLeft = sub
      ? `${Math.max(0, Math.round((sub.minutes_total || 0) - (sub.minutes_used || 0)))} min`
      : '—';

    const fmt = iso => iso ? new Date(iso).toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric'}) : '—';

    return `<tr>
      <td style="color:var(--muted);font-family:monospace;font-size:10px">${(r.id||'').slice(0,8)}…</td>
      <td><strong>${esc(r.username)}</strong></td>
      <td style="color:var(--text2)">${esc(r.email || '—')}</td>
      <td style="color:var(--purple-l)">${r.extension ? `Ext ${r.extension}` : '—'}</td>
      <td>${r.did_number ? `<span style="color:var(--green-l);font-family:monospace">${fmt_phone(r.did_number)}</span>` : '—'}</td>
      <td>${sub?.plan_name ? esc(sub.plan_name) : '—'}</td>
      <td>${minLeft}</td>
      <td>${fmt(sub?.purchased_at || r.subscription_started)}</td>
      <td style="color:var(--red-l)">${fmt(r.cancelled_at)}</td>
      <td>${statusBadge}</td>
      <td style="color:var(--muted)">${fmt(r.created_at)}</td>
    </tr>`;
  }).join('');
}

function filterSubscribers() {
  const q      = (document.getElementById('sub-search').value || '').toLowerCase();
  const status = document.getElementById('sub-filter-status').value;
  const rows   = _allSubscribers.filter(r => {
    const matchQ = !q || r.username.toLowerCase().includes(q) || (r.email||'').toLowerCase().includes(q);
    const isActive = r.subscription?.is_active;
    const matchS = !status
      || (status === 'active' && isActive)
      || (status === 'none' && !isActive);
    return matchQ && matchS;
  });
  renderSubscribers(rows);
}

function exportSubscribersCSV() {
  const rows = _allSubscribers;
  if (!rows.length) return;
  const fmt = iso => iso ? new Date(iso).toLocaleDateString() : '';
  const header = ['ID','Username','Email','Extension','DID','Plan','MinLeft','PurchaseDate','CancelledDate','Status','Joined'];
  const lines  = rows.map(r => {
    const sub = r.subscription;
    const minLeft = sub ? Math.max(0, Math.round((sub.minutes_total||0)-(sub.minutes_used||0))) : '';
    return [
      r.id, r.username, r.email||'', r.extension||'', r.did_number||'',
      sub?.plan_name||'', minLeft,
      fmt(sub?.purchased_at), fmt(r.cancelled_at),
      sub?.is_active ? 'Active' : r.did_number ? 'Cancelled' : 'No Plan',
      fmt(r.created_at),
    ].map(v => `"${String(v).replace(/"/g,'""')}"`).join(',');
  });
  const blob = new Blob([[header.join(','), ...lines].join('\n')], {type:'text/csv'});
  const a = Object.assign(document.createElement('a'), {href:URL.createObjectURL(blob), download:'subscribers.csv'});
  document.body.appendChild(a); a.click(); a.remove();
}

function fmt_phone(n) {
  const s = String(n).replace(/\D/g,'');
  return s.length === 11 && s[0] === '1'
    ? `+1 (${s.slice(1,4)}) ${s.slice(4,7)}-${s.slice(7)}`
    : '+' + s;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init ───────────────────────────────────────────────────────────────────────
loadReports();

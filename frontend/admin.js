'use strict';

// ── Auth guard ─────────────────────────────────────────────────────────────────
const _token = localStorage.getItem('dialer_token');
const _role  = localStorage.getItem('dialer_role');
if (!_token || _role !== 'superadmin') {
  window.location.href = '/login';
}
const _me = localStorage.getItem('dialer_username') || 'admin';
const el_user = document.getElementById('adm-user');
if (el_user) el_user.textContent = _me;

// ── Sidebar ────────────────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('adm-sidebar')?.classList.toggle('open');
  document.getElementById('overlay')?.classList.toggle('show');
}
function closeSidebar() {
  document.getElementById('adm-sidebar')?.classList.remove('open');
  document.getElementById('overlay')?.classList.remove('show');
}

// ── Clock ──────────────────────────────────────────────────────────────────────
setInterval(() => {
  const el = document.getElementById('header-clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}, 1000);

// ── Global state ───────────────────────────────────────────────────────────────
let _allCalls       = [];
let _allUsers       = [];
let _allDids        = [];
let _allInbound     = [];
let _allSubscribers = [];
let _allApps        = [];
let _globalAppFilter = '';   // '' = show all apps

// ── Global App Filter ──────────────────────────────────────────────────────────
function onAppFilterChange() {
  _globalAppFilter = document.getElementById('global-app-filter')?.value || '';
  // Re-render whichever section is active
  const active = document.querySelector('.adm-section.active');
  if (!active) return;
  const id = active.id.replace('sec-', '');
  if (id === 'users')       renderUsers();
  if (id === 'subscribers') filterSubscribers();
  if (id === 'apps')        renderApps(_allApps);
}

function populateAppFilter(apps) {
  const sel = document.getElementById('global-app-filter');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">All Apps</option>';
  apps.forEach(a => {
    const o = document.createElement('option');
    o.value = a.app_id;
    o.textContent = a.app_id === 'default' ? '📱 Default App' : `📱 ${a.app_id}`;
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;

  // Also populate subscribers app filter
  const subSel = document.getElementById('sub-filter-app');
  if (subSel) {
    const curSub = subSel.value;
    subSel.innerHTML = '<option value="">All apps</option>';
    apps.forEach(a => {
      const o = document.createElement('option');
      o.value = a.app_id;
      o.textContent = a.app_id === 'default' ? 'Default' : a.app_id;
      subSel.appendChild(o);
    });
    if (curSub) subSel.value = curSub;
  }
}

// ── Section navigation ─────────────────────────────────────────────────────────
function showSection(name, btn) {
  document.querySelectorAll('.adm-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.adm-nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('sec-' + name)?.classList.add('active');
  btn.classList.add('active');
  closeSidebar();
  if (name === 'reports')     loadReports();
  if (name === 'users')       loadUsers();
  if (name === 'dids')        loadDids();
  if (name === 'subscribers') loadSubscribers();
  if (name === 'inbound')     loadInbound();
  if (name === 'apps')        loadApps();
}

// ── API helper ─────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${_token}` },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401 || res.status === 403) {
    localStorage.clear(); window.location.href = '/login'; return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function logout() { localStorage.clear(); window.location.href = '/login'; }

// ── Formatters ─────────────────────────────────────────────────────────────────
function fmtDur(sec) {
  if (!sec && sec !== 0) return '—';
  if (sec < 60) return sec + 's';
  return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
}
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}
function fmtDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString();
}
function fmt_phone(n) {
  const s = String(n || '').replace(/\D/g, '');
  return s.length === 11 && s[0] === '1'
    ? `+1 (${s.slice(1,4)}) ${s.slice(4,7)}-${s.slice(7)}`
    : '+' + s;
}
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function appBadge(id) {
  if (!id || id === 'default') return '<span class="badge-app">default</span>';
  return `<span class="badge-app">${esc(id)}</span>`;
}

function sipLabel(code) {
  const map = {
    '200':'200 OK','180':'180 Ringing','183':'183 Progress',
    '404':'404 Not Found','480':'480 Unavailable','486':'486 Busy',
    '487':'487 Cancelled','503':'503 Unavailable','408':'408 Timeout',
    '403':'403 Forbidden','401':'401 Unauthorized',
    '500':'500 Server Error','603':'603 Declined',
  };
  return map[code] || (code && code !== '—' ? code : '—');
}
function sipBarColor(code) {
  if (!code || code === '—') return 'bar-gray';
  const n = parseInt(code);
  if (n >= 200 && n < 300) return 'bar-green';
  if (n >= 400 && n < 500) return 'bar-amber';
  if (n >= 500) return 'bar-red';
  return 'bar-gray';
}
function causeBarColor(cause) {
  if (!cause || cause === '—') return 'bar-gray';
  if (cause === 'NORMAL_CLEARING') return 'bar-green';
  if (cause.includes('TIMEOUT') || cause.includes('NO_ANSWER')) return 'bar-amber';
  if (cause.includes('BUSY') || cause.includes('REJECT')) return 'bar-red';
  return 'bar-purple';
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
  } catch (err) { console.error('Reports:', err); }
}

function renderSummary(s) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
  set('s-total',     s.total);
  set('s-answered',  s.answered);
  set('s-completed', s.completed);
  set('s-dropped',   s.dropped);
  set('s-failed',    s.failed);
  set('s-ans-rate',  s.answer_rate != null ? s.answer_rate + '%' : '—');
  set('s-avg-dur',   fmtDur(s.avg_duration));
  set('s-conn-rate', s.total > 0 ? ((s.answered / s.total) * 100).toFixed(1) + '%' : '—');
}

function renderBars(containerId, data, colorFn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = Object.entries(data || {}).sort((a, b) => b[1] - a[1]).slice(0, 14);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!entries.length || !total) {
    el.innerHTML = '<span style="color:var(--muted);font-size:12px">No data yet</span>';
    return;
  }
  el.innerHTML = entries.map(([key, count]) => {
    const pct = ((count / total) * 100).toFixed(1);
    const barPct = Math.max(2, (count / total) * 100);
    const cls = colorFn ? colorFn(key) : 'bar-purple';
    const label = containerId === 'sip-bars' ? sipLabel(key) : key;
    return `<div class="bar-row">
      <span class="bar-label" title="${key}">${label}</span>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${barPct}%"></div></div>
      <span class="bar-count">${count}</span>
    </div>`;
  }).join('');
}

function renderAgentPerf(agents) {
  const el = document.getElementById('agent-perf-tbody');
  if (!el) return;
  if (!agents?.length) {
    el.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted)">No agent data yet</td></tr>';
    return;
  }
  el.innerHTML = agents.map(a => {
    const connRate = a.calls > 0 ? ((a.answered / a.calls) * 100).toFixed(0) + '%' : '—';
    return `<tr>
      <td><strong>${esc(a.name)}</strong></td>
      <td>${a.calls}</td>
      <td><span style="color:var(--green-l)">${a.answered}</span> <span style="color:var(--muted);font-size:10px">(${connRate})</span></td>
      <td>${fmtDur(a.avg_duration)}</td>
    </tr>`;
  }).join('');
}

function buildDropdowns(calls, summary) {
  const fill = (selId, items, labelFn) => {
    const sel = document.getElementById(selId);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = sel.options[0].outerHTML;
    items.forEach(v => {
      const o = document.createElement('option');
      o.value = v; o.textContent = labelFn ? labelFn(v) : v;
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  };
  fill('filter-sip',   Object.keys(summary.sip_codes || {}).sort(), sipLabel);
  fill('filter-cause', Object.keys(summary.hangup_causes || {}).filter(k => k !== '—').sort());
  fill('filter-agent', [...new Set(calls.map(c => c.agent_name).filter(Boolean))].sort());
  fill('filter-campaign', [...new Set(calls.map(c => c.campaign_name).filter(Boolean))].sort());
}

function filterTable() {
  const search   = (document.getElementById('filter-search')?.value   || '').toLowerCase();
  const agent    = document.getElementById('filter-agent')?.value    || '';
  const campaign = document.getElementById('filter-campaign')?.value || '';
  const status   = document.getElementById('filter-status')?.value   || '';
  const sip      = document.getElementById('filter-sip')?.value      || '';
  const cause    = document.getElementById('filter-cause')?.value    || '';
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
  if (countEl) countEl.textContent = `${filtered.length} of ${_allCalls.length}`;
  renderReportTable(filtered);
}

function clearFilters() {
  ['filter-search','filter-agent','filter-campaign','filter-status','filter-sip','filter-cause','filter-date-from','filter-date-to']
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  filterTable();
}

function statusBadgeClass(status) {
  const map = { completed:'badge-completed', answered:'badge-answered', failed:'badge-failed', dropped:'badge-dropped' };
  return map[status] || 'badge-none';
}

function renderReportTable(calls) {
  const tbody = document.getElementById('report-tbody');
  tbody.innerHTML = '';
  [...calls].sort((a, b) => new Date(b.start_time || 0) - new Date(a.start_time || 0)).forEach(c => {
    const tr = document.createElement('tr');
    const recHtml = c.recording_path
      ? `<a href="/recordings/${c.recording_path}?download=true" download style="color:var(--blue);font-size:10px">⬇ Save</a>`
      : '—';
    tr.innerHTML = `
      <td style="font-size:10px;color:var(--muted);white-space:nowrap">${fmtDateTime(c.start_time)}</td>
      <td style="font-family:monospace;font-weight:600">${c.contact?.phone ?? '—'}</td>
      <td>${esc(c.contact?.name ?? '—')}</td>
      <td style="font-size:10px">${esc(c.campaign_name || '—')}</td>
      <td style="color:var(--purple-l);font-size:10px">${esc(c.agent_name || '—')}</td>
      <td style="font-family:monospace;font-size:10px">${c.caller_id || '—'}</td>
      <td>${fmtDur(c.duration)}</td>
      <td><span class="badge ${statusBadgeClass(c.status)}">${c.status}</span></td>
      <td style="font-size:10px">${sipLabel(c.sip_code || '—')}</td>
      <td style="font-size:10px">${c.hangup_cause || '—'}</td>
      <td style="font-size:10px">${c.amd_result || '—'}</td>
      <td style="font-size:10px">${c.disposition || '—'}</td>
      <td style="font-size:10px">${c.ai_sentiment || '—'}</td>
      <td>${recHtml}</td>`;
    tbody.appendChild(tr);
  });
}

function exportCSV() {
  const rows = [['DateTime','Phone','Name','Campaign','Agent','DID','Duration(s)','Status','SIPCode','SIPLabel','HangupCause','AMD','Disposition','Sentiment']];
  _allCalls.forEach(c => rows.push([
    c.start_time||'', c.contact?.phone||'', c.contact?.name||'',
    c.campaign_name||c.campaign_id||'', c.agent_name||c.agent_id||'',
    c.caller_id||'', c.duration??'', c.status||'',
    c.sip_code||'', sipLabel(c.sip_code)||'', c.hangup_cause||'',
    c.amd_result||'', c.disposition||'', c.ai_sentiment||'',
  ]));
  _dlCSV(rows, `calls-${new Date().toISOString().slice(0,10)}.csv`);
}

// ── Users ──────────────────────────────────────────────────────────────────────
async function loadUsers() {
  try {
    _allUsers = await api('GET', '/admin/users');
    const countEl = document.getElementById('users-count');
    if (countEl) countEl.textContent = `${_allUsers.length} users`;
    renderUsers();
  } catch (err) { console.error('Users:', err); }
}

function renderUsers() {
  const tbody = document.getElementById('user-tbody');
  if (!tbody) return;
  const q = (document.getElementById('user-search')?.value || '').toLowerCase();
  const rows = _allUsers.filter(u => {
    if (_globalAppFilter && (u.app_id || 'default') !== _globalAppFilter) return false;
    if (q && !u.username.toLowerCase().includes(q) && !(u.email || '').toLowerCase().includes(q)) return false;
    return true;
  });
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">No users found</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(u => {
    const roleBadge = u.role === 'superadmin'
      ? '<span class="badge badge-admin">Admin</span>'
      : '<span class="badge badge-none">Agent</span>';
    const extCell = u.extension
      ? `<span style="color:var(--purple-l);font-size:11px">Ext ${u.extension}</span>`
      : '<span style="color:var(--muted)">—</span>';
    return `<tr>
      <td><strong>${esc(u.username)}</strong></td>
      <td>${appBadge(u.app_id)}</td>
      <td style="color:var(--text2)">${esc(u.email || '—')}</td>
      <td>${roleBadge}</td>
      <td>${extCell}</td>
      <td style="color:var(--muted);font-size:11px">${fmtDate(u.created_at)}</td>
      <td>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-xs" onclick="resetPw('${esc(u.username)}')">Reset PW</button>
          ${u.extension ? `<button class="btn btn-ghost btn-xs" onclick="resetSipPw('${esc(u.username)}')">SIP PW</button>` : ''}
          <button class="btn btn-danger btn-xs" onclick="deleteUser('${esc(u.username)}')">Delete</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function filterUsers() { renderUsers(); }

async function createUser() {
  const username  = document.getElementById('u-username').value.trim();
  const password  = document.getElementById('u-password').value || '1234';
  const extension = document.getElementById('u-ext').value.trim();
  const role      = document.getElementById('u-role').value;
  const msgEl     = document.getElementById('u-msg');
  if (!username) { msgEl.style.color='var(--red-l)'; msgEl.textContent='✗ Username required'; return; }
  try {
    const result = await api('POST', '/admin/users', { username, password, extension: extension || null, role });
    msgEl.style.color = 'var(--green-l)';
    msgEl.textContent = `✓ User "${username}" created${result.agent_id ? ` (Agent Ext ${extension})` : ''}`;
    document.getElementById('u-username').value = '';
    document.getElementById('u-password').value = '';
    document.getElementById('u-ext').value = '';
    await loadUsers();
  } catch (err) { msgEl.style.color = 'var(--red-l)'; msgEl.textContent = '✗ ' + err.message; }
}

async function resetPw(username) {
  const pw = prompt(`New password for "${username}" (blank = 1234):`);
  if (pw === null) return;
  try {
    await api('POST', `/admin/users/${username}/reset-password`, { password: pw || '1234' });
    alert(`✓ Password reset for ${username}`);
  } catch (err) { alert('✗ ' + err.message); }
}

async function resetSipPw(username) {
  const pw = prompt(`SIP password for "${username}":\n(Must match FreeSWITCH user directory. Blank = 1234)`);
  if (pw === null) return;
  try {
    await api('POST', `/admin/users/${username}/reset-sip-password`, { password: pw || '1234' });
    alert(`✓ SIP password set for ${username}`);
  } catch (err) { alert('✗ ' + err.message); }
}

async function deleteUser(username) {
  if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
  try { await api('DELETE', `/admin/users/${username}`); await loadUsers(); }
  catch (err) { alert('✗ ' + err.message); }
}

// ── DIDs ───────────────────────────────────────────────────────────────────────
async function loadDids() {
  try {
    _allDids = await api('GET', '/admin/dids');
    const countEl = document.getElementById('did-count');
    if (countEl) countEl.textContent = `${_allDids.length} numbers`;
    renderDids();
  } catch (err) { console.error('DIDs:', err); }
}

function renderDids() {
  const tbody = document.getElementById('did-tbody');
  if (!tbody) return;
  tbody.innerHTML = _allDids.map(d => {
    const statusBadge = d.active
      ? '<span class="badge badge-active">Active</span>'
      : '<span class="badge badge-none">Inactive</span>';
    const forSaleBadge = d.for_sale
      ? '<span class="badge badge-active">Listed</span>'
      : '<span class="badge badge-none">Not Listed</span>';
    const ownerCell = d.owner_username
      ? `<span style="color:var(--purple-l);font-size:11px">${esc(d.owner_username)}</span>`
      : '<span style="color:var(--muted)">—</span>';
    return `<tr>
      <td style="font-family:monospace;font-weight:600">${fmt_phone(d.number)}</td>
      <td style="color:var(--text2)">${esc(d.label || '—')}</td>
      <td>${ownerCell}</td>
      <td>${statusBadge}</td>
      <td>${forSaleBadge}</td>
      <td>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-xs" onclick="toggleDid('${d.id}',${!d.active})">
            ${d.active ? 'Deactivate' : 'Activate'}
          </button>
          <button class="btn ${d.for_sale ? 'btn-danger' : 'btn-success'} btn-xs" onclick="toggleForSale('${d.id}',${!d.for_sale})">
            ${d.for_sale ? '✕ Delist' : '+ List for Sale'}
          </button>
          <button class="btn btn-danger btn-xs" onclick="deleteDid('${d.id}','${d.number}')">Delete</button>
        </div>
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No DIDs yet</td></tr>';
}

async function addDid() {
  const number = document.getElementById('d-number').value.trim();
  const label  = document.getElementById('d-label').value.trim();
  const msgEl  = document.getElementById('d-msg');
  if (!number) { msgEl.style.color='var(--red-l)'; msgEl.textContent='✗ Number required'; return; }
  try {
    await api('POST', '/admin/dids', { number, label });
    msgEl.style.color = 'var(--green-l)';
    msgEl.textContent = `✓ DID ${number} added`;
    document.getElementById('d-number').value = '';
    document.getElementById('d-label').value  = '';
    await loadDids();
  } catch (err) { msgEl.style.color='var(--red-l)'; msgEl.textContent='✗ '+err.message; }
}

async function toggleDid(id, active) {
  try { await api('PATCH', `/admin/dids/${id}`, { active }); await loadDids(); }
  catch (err) { alert('✗ ' + err.message); }
}

async function toggleForSale(id, forSale) {
  try {
    if (forSale) {
      // When re-listing, also clear owner so it appears in store
      await api('PATCH', `/admin/dids/${id}`, { for_sale: true });
    } else {
      await api('PATCH', `/admin/dids/${id}`, { for_sale: false });
    }
    await loadDids();
  } catch (err) { alert('✗ ' + err.message); }
}

async function deleteDid(id, number) {
  if (!confirm(`Delete DID ${number}?`)) return;
  try { await api('DELETE', `/admin/dids/${id}`); await loadDids(); }
  catch (err) { alert('✗ ' + err.message); }
}

// ── Subscribers ────────────────────────────────────────────────────────────────
async function loadSubscribers() {
  const tbody = document.getElementById('sub-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--muted)">Loading…</td></tr>';
  try {
    _allSubscribers = await api('GET', '/admin/subscribers');
    _calcSubKPIs(_allSubscribers);
    filterSubscribers();
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:var(--red-l)">${err.message}</td></tr>`;
  }
}

function _calcSubKPIs(rows) {
  // Unique users (deduplicated by username)
  const uniqueUsers = new Set(rows.map(r => r.username)).size;
  const active    = rows.filter(r => r.subscription?.is_active).length;
  const noplan    = rows.filter(r => !r.has_subscription).length;
  const didCount  = rows.filter(r => r.did_number && r.subscription?.is_active).length;
  // Revenue = sum of price * (1 + renewals) for all subscriptions ever
  const revenue = rows.reduce((s, r) => {
    if (!r.subscription) return s;
    const price   = r.subscription.price   || 0;
    const renewals = r.subscription.renewals || 0;
    return s + price * (1 + renewals);
  }, 0);
  const mrr = rows.filter(r => r.subscription?.is_active)
    .reduce((s, r) => s + (r.subscription.price || 0), 0);

  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set('sub-total',     uniqueUsers);
  set('sub-active',    active);
  set('sub-cancelled', noplan);
  set('sub-dids',      didCount);
  set('sub-revenue',   '$' + revenue.toFixed(2));
  set('sub-mrr',       '$' + mrr.toFixed(2));
}

function filterSubscribers() {
  const q      = (document.getElementById('sub-search')?.value || '').toLowerCase();
  const status = document.getElementById('sub-filter-status')?.value || '';
  const appF   = document.getElementById('sub-filter-app')?.value   || _globalAppFilter;

  const rows = _allSubscribers.filter(r => {
    if (appF && (r.app_id || 'default') !== appF) return false;
    if (q && !r.username.toLowerCase().includes(q) && !(r.email||'').toLowerCase().includes(q)) return false;
    const isActive = r.subscription?.is_active;
    const hasCancelled = r.cancelled_at;
    if (status === 'active'    && !isActive)      return false;
    if (status === 'none'      && r.has_subscription) return false;
    if (status === 'cancelled' && !hasCancelled)  return false;
    return true;
  });

  const countEl = document.getElementById('sub-count');
  if (countEl) countEl.textContent = `${rows.length} records`;
  renderSubscribers(rows);
}

function renderSubscribers(rows) {
  const tbody = document.getElementById('sub-tbody');
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--muted)">No records found</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const sub = r.subscription;
    const isActive = sub?.is_active;
    const hasCancelled = r.cancelled_at;

    let statusBadge;
    if (isActive)       statusBadge = '<span class="badge badge-active">Active</span>';
    else if (hasCancelled) statusBadge = '<span class="badge badge-cancelled">Cancelled</span>';
    else               statusBadge = '<span class="badge badge-none">No Plan</span>';

    const minLeft = sub
      ? Math.max(0, Math.round((sub.minutes_total || 0) - (sub.minutes_used || 0))) + ' min'
      : '—';
    const revenue = sub ? '$' + ((sub.price||0) * (1 + (sub.renewals||0))).toFixed(2) : '—';

    return `<tr>
      <td><strong>${esc(r.username)}</strong></td>
      <td>${appBadge(r.app_id)}</td>
      <td style="color:var(--text2)">${esc(r.email || '—')}</td>
      <td style="color:var(--purple-l)">${r.extension ? `Ext ${r.extension}` : '—'}</td>
      <td style="font-family:monospace;font-size:11px">${r.did_number ? fmt_phone(r.did_number) : '—'}</td>
      <td style="font-size:11px">${esc(sub?.plan_name || '—')}</td>
      <td style="color:var(--green-l);font-weight:600">${revenue}</td>
      <td>${minLeft}</td>
      <td style="color:var(--muted)">${fmtDate(sub?.purchased_at)}</td>
      <td style="color:var(--red-l)">${fmtDate(r.cancelled_at)}</td>
      <td>${statusBadge}</td>
      <td style="color:var(--muted)">${fmtDate(r.created_at)}</td>
    </tr>`;
  }).join('');
}

function exportSubscribersCSV() {
  const rows = [['Username','AppID','Email','Extension','DID','Plan','Revenue','MinLeft','Purchased','Cancelled','Status','Joined']];
  _allSubscribers.forEach(r => {
    const sub = r.subscription;
    const minLeft = sub ? Math.max(0, Math.round((sub.minutes_total||0)-(sub.minutes_used||0))) : '';
    const revenue = sub ? ((sub.price||0)*(1+(sub.renewals||0))).toFixed(2) : '';
    rows.push([
      r.username, r.app_id||'default', r.email||'', r.extension||'', r.did_number||'',
      sub?.plan_name||'', revenue ? '$'+revenue : '', minLeft,
      fmtDate(sub?.purchased_at), fmtDate(r.cancelled_at),
      sub?.is_active ? 'Active' : r.cancelled_at ? 'Cancelled' : 'No Plan',
      fmtDate(r.created_at),
    ]);
  });
  _dlCSV(rows, 'subscribers.csv');
}

// ── Apps Overview ──────────────────────────────────────────────────────────────
async function loadApps() {
  try {
    // Load both apps summary and full subscriber list for the per-user table
    const [apps, subs] = await Promise.all([
      api('GET', '/admin/apps'),
      api('GET', '/admin/subscribers'),
    ]);
    _allApps = apps;
    _allSubscribers = subs;
    populateAppFilter(apps);
    renderApps(apps);
    renderAppsUserTable(subs);
  } catch (err) { console.error('Apps:', err); }
}

function renderApps(apps) {
  const grid = document.getElementById('apps-grid');
  if (!grid) return;
  if (!apps.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:12px">No app data yet — register users with an app_id to see them here.</div>';
    return;
  }
  const filtered = _globalAppFilter ? apps.filter(a => a.app_id === _globalAppFilter) : apps;
  grid.innerHTML = filtered.map(a => `
    <div class="app-card">
      <div class="app-card-name">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
        ${esc(a.app_id === 'default' ? 'Default App' : a.app_id)}
      </div>
      <div class="app-stat-row">
        <div class="app-stat"><div class="app-stat-val">${a.users}</div><div class="app-stat-lbl">Users</div></div>
        <div class="app-stat"><div class="app-stat-val" style="color:var(--green-l)">${a.active_subs}</div><div class="app-stat-lbl">Active Plans</div></div>
        <div class="app-stat"><div class="app-stat-val" style="color:var(--amber-l)">$${(a.revenue||0).toFixed(2)}</div><div class="app-stat-lbl">Revenue</div></div>
      </div>
    </div>
  `).join('');
}

function renderAppsUserTable(rows) {
  const tbody = document.getElementById('apps-user-tbody');
  if (!tbody) return;
  const filtered = _globalAppFilter
    ? rows.filter(r => (r.app_id || 'default') === _globalAppFilter)
    : rows;
  // Deduplicate by username (show most recent only)
  const seen = new Set();
  const unique = filtered.filter(r => {
    if (seen.has(r.username)) return false;
    seen.add(r.username); return true;
  });
  if (!unique.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No users</td></tr>';
    return;
  }
  tbody.innerHTML = unique.map(r => {
    const sub = r.subscription;
    const isActive = sub?.is_active;
    let statusBadge;
    if (isActive) statusBadge = '<span class="badge badge-active">Active</span>';
    else if (r.has_subscription) statusBadge = '<span class="badge badge-cancelled">Cancelled</span>';
    else statusBadge = '<span class="badge badge-none">No Plan</span>';
    return `<tr>
      <td>${appBadge(r.app_id)}</td>
      <td><strong>${esc(r.username)}</strong></td>
      <td style="color:var(--text2)">${esc(r.email || '—')}</td>
      <td style="color:var(--purple-l)">${r.extension ? `Ext ${r.extension}` : '—'}</td>
      <td style="color:var(--muted)">${fmtDate(r.created_at)}</td>
      <td>${statusBadge}</td>
    </tr>`;
  }).join('');
}

// ── Inbound Calls ──────────────────────────────────────────────────────────────
async function loadInbound() {
  const tbody = document.getElementById('inb-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted)">Loading…</td></tr>';
  try {
    const data = await api('GET', '/admin/inbound');
    _allInbound = data.calls || [];
    const s = data.stats || {};
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? 0; };
    set('inb-total',    s.total);
    set('inb-answered', s.answered);
    set('inb-missed',   s.missed);
    set('inb-live',     s.live);
    set('inb-today',    s.today);
    renderInbound(_allInbound);
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--red-l)">${err.message}</td></tr>`;
  }
}

function renderInbound(rows) {
  const tbody = document.getElementById('inb-tbody');
  const countEl = document.getElementById('inb-count');
  if (countEl) countEl.textContent = `${rows.length} calls`;
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted)">No inbound calls yet</td></tr>';
    return;
  }
  const statusBadgeInb = s => {
    const map = {
      answered:'badge-answered', completed:'badge-answered',
      ringing:'badge-dropped', missed:'badge-cancelled', rejected:'badge-cancelled', failed:'badge-cancelled',
    };
    return `<span class="badge ${map[s]||'badge-none'}">${(s||'—').toUpperCase()}</span>`;
  };
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td style="color:var(--muted);white-space:nowrap">${fmtDateTime(r.start_time)}</td>
      <td style="font-family:monospace;font-weight:600">${fmt_phone(r.caller)}</td>
      <td style="color:var(--green-l);font-family:monospace">${fmt_phone(r.did)}</td>
      <td>${r.owner_username ? esc(r.owner_username) : '<span style="color:var(--muted)">—</span>'}</td>
      <td style="color:var(--purple-l)">${r.extension ? `Ext ${esc(r.extension)}` : '—'}</td>
      <td>${statusBadgeInb(r.status)}</td>
      <td>${r.duration ? fmtDur(r.duration) : '—'}</td>
      <td style="color:var(--muted);font-size:10px">${esc(r.hangup_cause || '—')}</td>
    </tr>`).join('');
}

function filterInbound() {
  const q      = (document.getElementById('inb-search')?.value || '').toLowerCase();
  const status = document.getElementById('inb-filter-status')?.value || '';
  const rows = _allInbound.filter(r =>
    (!q || String(r.caller||'').includes(q) || String(r.did||'').includes(q) || String(r.owner_username||'').toLowerCase().includes(q)) &&
    (!status || r.status === status)
  );
  renderInbound(rows);
}

function exportInboundCSV() {
  const rows = [['Time','Caller','DID','ReceivedBy','Extension','Status','TalkSeconds','HangupCause']];
  _allInbound.forEach(r => rows.push([
    fmtDateTime(r.start_time), r.caller||'', r.did||'', r.owner_username||'',
    r.extension||'', r.status||'', r.duration||0, r.hangup_cause||'',
  ]));
  _dlCSV(rows, 'inbound_calls.csv');
}

// ── CSV helper ─────────────────────────────────────────────────────────────────
function _dlCSV(rows, filename) {
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const a = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(new Blob([csv], {type:'text/csv'})),
    download: filename,
  });
  document.body.appendChild(a); a.click(); a.remove();
}

// ── Init ───────────────────────────────────────────────────────────────────────
(async () => {
  // Load apps in background for filter dropdowns
  try {
    _allApps = await api('GET', '/admin/apps');
    populateAppFilter(_allApps);
  } catch (_) {}
  loadReports();
})();

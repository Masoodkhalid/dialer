'use strict';

/**
 * WebPhone module — JsSIP-based SIP-over-WebSocket client.
 *
 * Agents can toggle between:
 *   softphone → Zoiper / external SIP client (no change to existing flow)
 *   webphone  → JsSIP registers over ws://<host>:5066 directly in the browser
 *
 * When webphone mode is active the dialer targets transport=ws so only the
 * browser endpoint rings, bypassing Zoiper even if it is also registered.
 */

// ── State ──────────────────────────────────────────────────────────────────────
const WP = {
  ua:          null,
  session:     null,
  config:      null,
  mode:        'softphone',
  regStatus:   'unregistered',
  callState:   'idle',
  callTimer:   null,
  callStart:   null,
  muted:       false,
  minimised:   false,
};

const _remoteAudio = new Audio();
_remoteAudio.autoplay = true;

// ── Bootstrap ──────────────────────────────────────────────────────────────────
async function wpInit() {
  try {
    const res = await fetch('/agent/config', {
      headers: { Authorization: `Bearer ${localStorage.getItem('dialer_token')}` },
    });
    if (!res.ok) { console.warn('webphone: agent config not available'); return; }
    WP.config = await res.json();
  } catch (e) {
    console.warn('webphone: config fetch failed', e);
    return;
  }

  // Only show widget to users who have a SIP extension
  if (!WP.config.extension) return;

  _renderWidget();

  // Restore saved mode — prefer localStorage, fall back to server-stored preference
  const saved   = localStorage.getItem('wp_mode');
  const initial = saved || WP.config.phone_type || 'softphone';
  await _setMode(initial, false);   // false = don't re-save on initial load
}

// ── JsSIP lifecycle ────────────────────────────────────────────────────────────
function _startUA() {
  const cfg = WP.config;

  if (!cfg || !cfg.extension || !cfg.ws_url) {
    _showStatusText('⚠ Missing WebSocket URL — check server .env');
    return;
  }

  if (!cfg.sip_password) {
    // Show the password-setup panel inside the widget
    const setup = document.getElementById('wp-setup');
    if (setup) setup.style.display = '';
    _showStatusText('⚠ Enter SIP password below to activate');
    return;
  }

  if (WP.ua) _stopUA();

  let socket;
  try {
    socket = new JsSIP.WebSocketInterface(cfg.ws_url);
  } catch (e) {
    console.error('webphone: WebSocketInterface failed', e);
    _showStatusText('⚠ WebSocket error — check FS_WS_URL');
    return;
  }

  const ua = new JsSIP.UA({
    sockets:      [socket],
    uri:          `sip:${cfg.extension}@${cfg.sip_domain}`,
    password:     cfg.sip_password,
    display_name: cfg.display_name || cfg.extension,
    register:     true,
    register_expires: 300,
    connection_recovery_min_interval: 2,
    connection_recovery_max_interval: 30,
    log: { builtinEnabled: false },
  });

  ua.on('registered',     ()  => { WP.regStatus = 'registered';   _renderStatus(); });
  ua.on('unregistered',   ()  => { WP.regStatus = 'unregistered'; _renderStatus(); });
  ua.on('registrationFailed', (e) => {
    WP.regStatus = 'failed';
    console.error('webphone reg failed:', e.cause, e.response);
    _renderStatus();
  });
  ua.on('connected',    ()  => { _renderStatus(); });
  ua.on('disconnected', ()  => { WP.regStatus = 'unregistered'; _renderStatus(); });

  ua.on('newRTCSession', (data) => {
    const session = data.session;
    if (session.direction === 'outgoing') return;

    // Reject if already in a call
    if (WP.session && WP.callState !== 'idle') {
      session.terminate();
      return;
    }

    WP.session   = session;
    WP.callState = 'ringing';
    WP.muted     = false;
    _renderCallState();
    _playRingtone(true);
    _expandWidget();

    session.on('accepted', () => {
      WP.callState = 'active';
      WP.callStart = Date.now();
      _playRingtone(false);
      _startCallTimer();
      _renderCallState();
    });

    session.on('peerconnection', (e) => {
      const pc = e.peerconnection;
      pc.ontrack = (ev) => {
        if (ev.streams && ev.streams[0]) {
          _remoteAudio.srcObject = ev.streams[0];
        }
      };
    });

    session.on('ended',  () => _onSessionEnd());
    session.on('failed', () => _onSessionEnd());
  });

  try {
    ua.start();
  } catch (e) {
    console.error('webphone: UA start failed', e);
    _showStatusText('⚠ Could not start — check console');
    return;
  }

  WP.ua        = ua;
  WP.regStatus = 'connecting';
  _renderStatus();
}

function _stopUA() {
  if (!WP.ua) return;
  try { WP.ua.stop(); } catch (_) {}
  WP.ua        = null;
  WP.session   = null;
  WP.callState = 'idle';
  WP.regStatus = 'unregistered';
  _playRingtone(false);
  _stopCallTimer();
  _renderStatus();
  _renderCallState();
}

function _onSessionEnd() {
  WP.session   = null;
  WP.callState = 'idle';
  WP.muted     = false;
  _playRingtone(false);
  _stopCallTimer();
  _remoteAudio.srcObject = null;
  _renderCallState();
}

// ── Call controls ──────────────────────────────────────────────────────────────
function wpAnswer() {
  if (!WP.session || WP.callState !== 'ringing') return;
  WP.session.answer({
    mediaConstraints: { audio: true, video: false },
    sessionTimersExpires: 120,
  });
}

function wpReject() {
  if (!WP.session) return;
  WP.session.terminate();
}

function wpHangup() {
  if (!WP.session) return;
  WP.session.terminate();
}

function wpToggleMute() {
  if (!WP.session || WP.callState !== 'active') return;
  WP.muted = !WP.muted;
  WP.muted ? WP.session.mute({ audio: true }) : WP.session.unmute({ audio: true });
  _renderCallState();
}

// ── Mode switch ────────────────────────────────────────────────────────────────
function wpSetMode(mode) { _setMode(mode, true); }

async function _setMode(mode, save) {
  WP.mode = mode;
  localStorage.setItem('wp_mode', mode);
  _renderModeButtons();

  // Always hide setup panel first; _startUA will re-show if needed
  const setup = document.getElementById('wp-setup');
  if (setup) setup.style.display = 'none';

  try {
    if (mode === 'webphone') {
      _startUA();
    } else {
      _stopUA();
      _renderStatus();
    }
  } catch (e) {
    console.error('webphone: mode switch error', e);
    _showStatusText('⚠ Error: ' + e.message);
  }

  // Save preference to server so the dialer uses the right bridge mode
  if (save) {
    try {
      await fetch('/agent/preferences', {
        method:  'PATCH',
        headers: {
          'Content-Type':  'application/json',
          'Authorization': `Bearer ${localStorage.getItem('dialer_token')}`,
        },
        body: JSON.stringify({ phone_type: mode }),
      });
    } catch (e) {
      console.warn('webphone: could not save preference', e);
    }
  }
}

// ── Ringtone (oscillator-based, no file needed) ────────────────────────────────
let _ringCtx = null;
function _playRingtone(on) {
  if (on) {
    if (_ringCtx) return;
    try {
      const ctx  = new AudioContext();
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(480, ctx.currentTime);
      gain.gain.setValueAtTime(0.25, ctx.currentTime);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      let ring = true;
      const iv = setInterval(() => {
        ring = !ring;
        gain.gain.setValueAtTime(ring ? 0.25 : 0, ctx.currentTime);
        if (!_ringCtx) { clearInterval(iv); try { osc.stop(); ctx.close(); } catch (_) {} }
      }, 1000);
      _ringCtx = { ctx, osc, gain, iv };
    } catch (e) { console.warn('webphone: ringtone failed', e); }
  } else {
    if (!_ringCtx) return;
    try { clearInterval(_ringCtx.iv); _ringCtx.osc.stop(); _ringCtx.ctx.close(); } catch (_) {}
    _ringCtx = null;
  }
}

// ── Call timer ─────────────────────────────────────────────────────────────────
function _startCallTimer() {
  _stopCallTimer();
  WP.callTimer = setInterval(() => {
    const el = document.getElementById('wp-timer');
    if (el && WP.callStart) {
      const s = Math.floor((Date.now() - WP.callStart) / 1000);
      const m = Math.floor(s / 60);
      el.textContent = `${String(m).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
    }
  }, 1000);
}

function _stopCallTimer() {
  if (WP.callTimer) { clearInterval(WP.callTimer); WP.callTimer = null; }
}

// ── Widget open/close ──────────────────────────────────────────────────────────
function wpToggleWidget() {
  WP.minimised = !WP.minimised;
  const body    = document.getElementById('wp-body');
  const chevron = document.getElementById('wp-chevron');
  if (body)    body.style.display    = WP.minimised ? 'none' : '';
  if (chevron) chevron.textContent   = WP.minimised ? '▲' : '▼';
}

function _expandWidget() {
  if (WP.minimised) wpToggleWidget();
}

// ── Save SIP password from within widget ──────────────────────────────────────
async function wpSaveSipPassword() {
  const pw = (document.getElementById('wp-sip-pw')?.value || '').trim();
  if (!pw) return;
  try {
    const res = await fetch('/agent/preferences', {
      method:  'PATCH',
      headers: {
        'Content-Type':  'application/json',
        'Authorization': `Bearer ${localStorage.getItem('dialer_token')}`,
      },
      body: JSON.stringify({ sip_password: pw }),
    });
    if (res.ok) {
      WP.config.sip_password = pw;
      const setup = document.getElementById('wp-setup');
      if (setup) setup.style.display = 'none';
      _startUA();
    } else {
      alert('Could not save SIP password (server error)');
    }
  } catch (e) {
    alert('Could not save SIP password: ' + e.message);
  }
}

// ── Render helpers ─────────────────────────────────────────────────────────────
function _renderWidget() {
  const el = document.createElement('div');
  el.id = 'webphone-widget';
  el.innerHTML = `
<div class="wp-header" onclick="wpToggleWidget()">
  <span class="wp-title">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M3 18v-6a9 9 0 0 1 18 0v6"/>
      <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>
    </svg>
    WebPhone
  </span>
  <div style="display:flex;gap:6px;align-items:center">
    <span id="wp-reg-dot" class="wp-dot wp-dot-off"></span>
    <span id="wp-chevron" class="wp-chevron">▼</span>
  </div>
</div>

<div id="wp-body" class="wp-body">

  <!-- Mode toggle -->
  <div class="wp-mode-row">
    <button id="wp-btn-soft" class="wp-mode-btn" onclick="wpSetMode('softphone')">
      📟 Softphone
    </button>
    <button id="wp-btn-web" class="wp-mode-btn" onclick="wpSetMode('webphone')">
      🌐 Webphone
    </button>
  </div>

  <!-- Status line -->
  <div id="wp-status-line" class="wp-status-line">Initialising…</div>

  <!-- SIP password setup (shown when not configured) -->
  <div id="wp-setup" class="wp-setup" style="display:none">
    <div style="font-size:10px;color:var(--muted);margin-bottom:6px">
      Enter the SIP password for ext <strong>${WP.config?.extension}</strong>
      (must match FreeSWITCH directory)
    </div>
    <div class="wp-input-row">
      <input id="wp-sip-pw" type="password" placeholder="SIP password…" class="wp-input"/>
      <button class="wp-save-btn" onclick="wpSaveSipPassword()">Save</button>
    </div>
  </div>

  <!-- Call state panel -->
  <div id="wp-call-panel" style="display:none">
    <div id="wp-call-info" class="wp-call-info"></div>
    <div id="wp-call-btns" class="wp-call-btns"></div>
  </div>

</div>`;
  document.body.appendChild(el);
}

function _renderModeButtons() {
  const soft = document.getElementById('wp-btn-soft');
  const web  = document.getElementById('wp-btn-web');
  if (!soft || !web) return;
  soft.classList.toggle('active', WP.mode === 'softphone');
  web.classList.toggle('active',  WP.mode === 'webphone');
}

// Update just the status line text and dot
function _showStatusText(text, color, dotCls) {
  const el  = document.getElementById('wp-status-line');
  const dot = document.getElementById('wp-reg-dot');
  if (el)  { el.textContent = text; if (color) el.style.color = color; }
  if (dot && dotCls) dot.className = dotCls;
}

function _renderStatus() {
  const dot    = document.getElementById('wp-reg-dot');
  const status = document.getElementById('wp-status-line');
  if (!status) return;

  if (WP.mode === 'softphone') {
    if (dot) dot.className = 'wp-dot wp-dot-off';
    status.textContent  = '📟 Using Zoiper / softphone';
    status.style.color  = 'var(--muted)';
    return;
  }

  // Webphone mode — show registration state
  const map = {
    registered:   ['wp-dot wp-dot-on',  '● Registered — ready for calls', 'var(--green)'],
    connecting:   ['wp-dot wp-dot-warn','◌ Connecting to FreeSWITCH…',    'var(--amber)'],
    unregistered: ['wp-dot wp-dot-off', '○ Not registered',                'var(--muted)'],
    failed:       ['wp-dot wp-dot-err', '✕ Registration failed — check SIP password & port 5066', 'var(--red)'],
  };
  const [cls, label, color] = map[WP.regStatus] || map.unregistered;
  if (dot) dot.className = cls;
  status.textContent = label;
  status.style.color = color;
}

function _renderCallState() {
  const panel  = document.getElementById('wp-call-panel');
  const info   = document.getElementById('wp-call-info');
  const btns   = document.getElementById('wp-call-btns');
  const widget = document.getElementById('webphone-widget');
  if (!panel || !info || !btns) return;

  if (WP.callState === 'idle') {
    panel.style.display = 'none';
    widget?.classList.remove('wp-ringing', 'wp-active');
    return;
  }

  panel.style.display = '';

  if (WP.callState === 'ringing') {
    widget?.classList.add('wp-ringing');
    widget?.classList.remove('wp-active');
    info.innerHTML = `<div class="wp-call-label">📞 Incoming Call</div>`;
    btns.innerHTML = `
      <button class="wp-btn wp-btn-green" onclick="wpAnswer()">✓ Answer</button>
      <button class="wp-btn wp-btn-red"   onclick="wpReject()">✕ Reject</button>`;
  } else if (WP.callState === 'active') {
    widget?.classList.remove('wp-ringing');
    widget?.classList.add('wp-active');
    info.innerHTML = `
      <div class="wp-call-label" style="color:var(--red)">🔴 On Call</div>
      <div class="wp-call-timer" id="wp-timer">00:00</div>`;
    btns.innerHTML = `
      <button class="wp-btn ${WP.muted ? 'wp-btn-amber' : 'wp-btn-muted'}" onclick="wpToggleMute()">
        ${WP.muted ? '🔇 Unmute' : '🔇 Mute'}
      </button>
      <button class="wp-btn wp-btn-red" onclick="wpHangup()">📵 Hangup</button>`;
    if (WP.callStart) {
      const s = Math.floor((Date.now() - WP.callStart) / 1000);
      const m = Math.floor(s / 60);
      const el = document.getElementById('wp-timer');
      if (el) el.textContent = `${String(m).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
    }
  }
}

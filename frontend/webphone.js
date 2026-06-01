'use strict';

/**
 * WebPhone module — JsSIP-based SIP-over-WebSocket client.
 *
 * Works alongside the existing Zoiper softphone flow.
 * When the user switches to "webphone" mode:
 *   1. JsSIP registers their extension over ws://<freeswitchhost>:5066
 *   2. Incoming calls (bridged by the dialer) ring in the browser
 *   3. Agent answers / rejects / hangs up from the widget
 * When the user switches back to "softphone" mode the UA is stopped
 * and the widget collapses back to a simple indicator.
 */

// ── State ──────────────────────────────────────────────────────────────────────
const WP = {
  ua:            null,     // JsSIP.UA instance
  session:       null,     // active RTCSession
  config:        null,     // agent config from /agent/config
  mode:          'softphone',
  regStatus:     'unregistered', // 'unregistered' | 'registered' | 'failed'
  callState:     'idle',         // 'idle' | 'ringing' | 'active'
  callTimer:     null,
  callStart:     null,
  muted:         false,
  minimised:     false,
};

const _remoteAudio = new Audio();
_remoteAudio.autoplay = true;

// ── Bootstrap ──────────────────────────────────────────────────────────────────
async function wpInit() {
  // Fetch agent config (extension, sip_password, ws_url, phone_type, sip_domain)
  try {
    const res = await fetch('/agent/config', {
      headers: { Authorization: `Bearer ${localStorage.getItem('dialer_token')}` },
    });
    if (!res.ok) { console.warn('webphone: no agent config'); return; }
    WP.config = await res.json();
  } catch (e) {
    console.warn('webphone: config fetch failed', e);
    return;
  }

  // Render the widget into the page
  _renderWidget();

  // Restore saved phone type preference
  const saved = localStorage.getItem('wp_mode');
  const initial = saved || WP.config.phone_type || 'softphone';
  _setMode(initial, false);   // false = no server save on first load
}

// ── JsSIP lifecycle ────────────────────────────────────────────────────────────
function _startUA() {
  const cfg = WP.config;
  if (!cfg || !cfg.extension || !cfg.sip_password || !cfg.ws_url) {
    _setStatus('⚠ Configure SIP password');
    return;
  }

  if (WP.ua) { _stopUA(); }

  const socket = new JsSIP.WebSocketInterface(cfg.ws_url);
  const ua = new JsSIP.UA({
    sockets:      [socket],
    uri:          `sip:${cfg.extension}@${cfg.sip_domain}`,
    password:     cfg.sip_password,
    display_name: cfg.display_name || cfg.extension,
    register:     true,
    register_expires: 300,
    connection_recovery_min_interval: 2,
    connection_recovery_max_interval: 30,
  });

  ua.on('registered',     ()  => { WP.regStatus = 'registered'; _renderStatus(); });
  ua.on('unregistered',   ()  => { WP.regStatus = 'unregistered'; _renderStatus(); });
  ua.on('registrationFailed', (e) => {
    WP.regStatus = 'failed';
    console.error('webphone reg failed:', e.cause);
    _renderStatus();
  });
  ua.on('connected',    () => _renderStatus());
  ua.on('disconnected', () => { WP.regStatus = 'unregistered'; _renderStatus(); });

  ua.on('newRTCSession', (data) => {
    const session = data.session;
    if (session.direction === 'outgoing') return;  // dialer doesn't make outgoing calls here

    // If already on a call, reject the new one
    if (WP.session && WP.callState !== 'idle') {
      session.terminate();
      return;
    }

    WP.session  = session;
    WP.callState = 'ringing';
    WP.muted    = false;
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

    session.on('ended',   () => _onSessionEnd());
    session.on('failed',  () => _onSessionEnd());
  });

  ua.start();
  WP.ua = ua;
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
  if (WP.muted) {
    WP.session.mute({ audio: true });
  } else {
    WP.session.unmute({ audio: true });
  }
  _renderCallState();
}

// ── Mode switch ────────────────────────────────────────────────────────────────
function wpSetMode(mode) { _setMode(mode, true); }

async function _setMode(mode, save) {
  WP.mode = mode;
  localStorage.setItem('wp_mode', mode);
  _renderModeButtons();

  if (mode === 'webphone') {
    _startUA();
  } else {
    _stopUA();
  }

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
    } catch (_) {}
  }
}

// ── Ringtone ───────────────────────────────────────────────────────────────────
let _ringAudio = null;
function _playRingtone(on) {
  if (on) {
    if (_ringAudio) return;
    // Simple oscillator-based ring (no file needed)
    try {
      const ctx  = new AudioContext();
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type      = 'sine';
      osc.frequency.setValueAtTime(480, ctx.currentTime);
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      // Pulse every 2 s: ring 1 s, silence 1 s
      let ringing = true;
      const pulse = setInterval(() => {
        ringing = !ringing;
        gain.gain.setValueAtTime(ringing ? 0.3 : 0, ctx.currentTime);
        if (!_ringAudio) { clearInterval(pulse); osc.stop(); ctx.close(); }
      }, 1000);
      _ringAudio = { ctx, osc, gain, pulse };
    } catch (e) { console.warn('ringtone failed', e); }
  } else {
    if (!_ringAudio) return;
    try {
      clearInterval(_ringAudio.pulse);
      _ringAudio.osc.stop();
      _ringAudio.ctx.close();
    } catch (_) {}
    _ringAudio = null;
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
      el.textContent = `${String(m).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}`;
    }
  }, 1000);
}

function _stopCallTimer() {
  if (WP.callTimer) { clearInterval(WP.callTimer); WP.callTimer = null; }
}

// ── Widget toggle ──────────────────────────────────────────────────────────────
function wpToggleWidget() {
  WP.minimised = !WP.minimised;
  const body = document.getElementById('wp-body');
  const chevron = document.getElementById('wp-chevron');
  if (body) body.style.display = WP.minimised ? 'none' : '';
  if (chevron) chevron.textContent = WP.minimised ? '▲' : '▼';
}

function _expandWidget() {
  if (WP.minimised) wpToggleWidget();
}

// ── Render helpers ─────────────────────────────────────────────────────────────
function _renderWidget() {
  // Insert widget HTML into the page
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
    <button id="wp-btn-soft" class="wp-mode-btn active" onclick="wpSetMode('softphone')">
      📟 Softphone
    </button>
    <button id="wp-btn-web" class="wp-mode-btn" onclick="wpSetMode('webphone')">
      🌐 Webphone
    </button>
  </div>

  <!-- Status line -->
  <div id="wp-status-line" class="wp-status-line">Select Webphone to activate</div>

  <!-- SIP password setup (shown when not configured) -->
  <div id="wp-setup" class="wp-setup" style="display:none">
    <div style="font-size:10px;color:var(--muted);margin-bottom:6px">SIP password needed to register</div>
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
  _renderModeButtons();
  _renderStatus();
  _renderCallState();
}

function _renderModeButtons() {
  const soft = document.getElementById('wp-btn-soft');
  const web  = document.getElementById('wp-btn-web');
  if (!soft || !web) return;
  soft.classList.toggle('active', WP.mode === 'softphone');
  web.classList.toggle('active',  WP.mode === 'webphone');
}

function _renderStatus() {
  const dot    = document.getElementById('wp-reg-dot');
  const status = document.getElementById('wp-status-line');
  const setup  = document.getElementById('wp-setup');
  if (!status) return;

  // Show setup panel if webphone mode but no SIP password
  const needsSetup = WP.mode === 'webphone' && (!WP.config?.sip_password);
  if (setup) setup.style.display = needsSetup ? '' : 'none';

  if (WP.mode === 'softphone') {
    if (dot) { dot.className = 'wp-dot wp-dot-off'; }
    status.textContent = '📟 Using Zoiper / softphone';
    status.style.color = 'var(--muted)';
    return;
  }

  const map = {
    registered:   ['wp-dot wp-dot-on',  '● Registered',  'var(--green)'],
    connecting:   ['wp-dot wp-dot-warn','◌ Connecting…', 'var(--amber)'],
    unregistered: ['wp-dot wp-dot-off', '○ Unregistered','var(--muted)'],
    failed:       ['wp-dot wp-dot-err', '✕ Reg failed',  'var(--red)'],
  };
  const [cls, label, color] = map[WP.regStatus] || map.unregistered;
  if (dot) dot.className = cls;
  status.textContent = label;
  status.style.color = color;
}

function _renderCallState() {
  const panel = document.getElementById('wp-call-panel');
  const info  = document.getElementById('wp-call-info');
  const btns  = document.getElementById('wp-call-btns');
  if (!panel || !info || !btns) return;

  const widget = document.getElementById('webphone-widget');

  if (WP.callState === 'idle') {
    panel.style.display = 'none';
    widget?.classList.remove('wp-ringing', 'wp-active');
    return;
  }

  panel.style.display = '';

  if (WP.callState === 'ringing') {
    widget?.classList.add('wp-ringing');
    widget?.classList.remove('wp-active');
    info.innerHTML = `
      <div class="wp-call-label">📞 Incoming Call</div>
      <div class="wp-call-sub" id="wp-caller">Dialer</div>`;
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
      <button id="wp-mute-btn" class="wp-btn ${WP.muted ? 'wp-btn-amber' : 'wp-btn-muted'}" onclick="wpToggleMute()">
        ${WP.muted ? '🔇 Unmute' : '🔇 Mute'}
      </button>
      <button class="wp-btn wp-btn-red" onclick="wpHangup()">📵 Hangup</button>`;
    if (WP.callStart) {
      const s = Math.floor((Date.now() - WP.callStart) / 1000);
      const m = Math.floor(s / 60);
      const timerEl = document.getElementById('wp-timer');
      if (timerEl) timerEl.textContent = `${String(m).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}`;
    }
  }
}

// ── Save SIP password ──────────────────────────────────────────────────────────
async function wpSaveSipPassword() {
  const pw = document.getElementById('wp-sip-pw')?.value?.trim();
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
      document.getElementById('wp-setup').style.display = 'none';
      _startUA();  // restart with new password
    }
  } catch (e) {
    alert('Could not save SIP password: ' + e.message);
  }
}

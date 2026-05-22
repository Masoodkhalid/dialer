'use strict';

// If already logged in, redirect immediately
const _existing = localStorage.getItem('dialer_token');
if (_existing) {
  const role = localStorage.getItem('dialer_role');
  window.location.href = role === 'superadmin' ? '/admin' : '/';
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  const errEl    = document.getElementById('err-box');
  const btn      = document.getElementById('login-btn');

  btn.disabled    = true;
  btn.textContent = 'Signing in…';
  errEl.style.display = 'none';

  try {
    const res = await fetch('/auth/login', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username, password }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Invalid credentials');
    }

    const data = await res.json();
    localStorage.setItem('dialer_token',    data.token);
    localStorage.setItem('dialer_role',     data.role);
    localStorage.setItem('dialer_username', data.username);

    window.location.href = data.role === 'superadmin' ? '/admin' : '/';
  } catch (err) {
    errEl.textContent   = '✗ ' + err.message;
    errEl.style.display = 'block';
    btn.disabled        = false;
    btn.textContent     = 'Sign In';
  }
});

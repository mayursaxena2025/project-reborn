const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const stageNames = { new: 'New', qualified: 'Qualified', nurture: 'Nurture', appointment: 'Appointment', under_contract: 'Under contract', closed: 'Closed', disqualified: 'Disqualified' };
const stageOrder = ['new', 'qualified', 'nurture', 'appointment', 'under_contract', 'closed', 'disqualified'];
const state = { leads: [], dashboard: null, settings: null, toastTimer: null, actionLeadId: null, actionKind: null, user: null, csrfToken: null, resetToken: null };
let workspaceEventsBound = false;

async function api(path, options = {}) {
  const endpoint = new URL(path.replace(/^\/+/, ''), document.baseURI);
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers };
  if (options.method && options.method.toUpperCase() !== 'GET' && state.csrfToken) {
    headers['X-CSRF-Token'] = state.csrfToken;
  }
  const response = await fetch(endpoint, {
    ...options,
    credentials: 'same-origin',
    headers,
  });
  const data = await response.json();
  if (response.status === 401 && state.user && !path.startsWith('/api/auth/')) {
    state.user = null;
    state.csrfToken = null;
    showAuthGate();
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function setAuthFeedback(message, isError = false) {
  const element = $('#auth-feedback');
  element.textContent = message;
  element.classList.toggle('error', isError);
}

function setAuthMode(mode) {
  const forms = {
    login: $('#login-form'),
    register: $('#register-form'),
    reset: $('#reset-request-form'),
    'reset-complete': $('#reset-complete-form'),
  };
  if (!forms[mode]) mode = 'login';
  Object.entries(forms).forEach(([name, form]) => form.classList.toggle('hidden', name !== mode));
  $('#auth-title').textContent = ({
    login: 'Sign in to OpenLead',
    register: 'Create your private workspace',
    reset: 'Reset your password',
    'reset-complete': 'Choose a new password',
  })[mode];
  $('#auth-copy').textContent = ({
    login: 'Access your private leads and integrations.',
    register: 'One private workspace for your leads and connections.',
    reset: 'We’ll email a one-time password reset link if the account exists.',
    'reset-complete': 'Choose a strong password of at least 14 characters.',
  })[mode];
  $('#auth-switch').classList.toggle('hidden', mode === 'reset' || mode === 'reset-complete');
  $('#show-reset').classList.toggle('hidden', mode !== 'login');
  $('#auth-switch').innerHTML = mode === 'register'
    ? 'Already have an account? <button class="auth-link" data-auth-mode="login" type="button">Sign in</button>'
    : 'New to OpenLead? <button class="auth-link" data-auth-mode="register" type="button">Create an account</button>';
  setAuthFeedback('');
}

function showAuthGate() {
  $('#app-shell').classList.add('hidden');
  $('#auth-gate').classList.remove('hidden');
}

function showWorkspace(user) {
  state.user = user;
  $('#auth-gate').classList.add('hidden');
  $('#app-shell').classList.remove('hidden');
  $('#profile-name').textContent = user.display_name;
  $('#profile-email').textContent = user.email;
  $('#profile-initials').textContent = initials(user.display_name) || 'OL';
  if (!workspaceEventsBound) {
    bindEvents();
    workspaceEventsBound = true;
  }
  setDates();
  setView(location.hash.slice(1) || 'overview');
  loadAll();
}

async function completeAuthLink() {
  const params = new URLSearchParams(location.search);
  const verificationToken = params.get('verify');
  const resetToken = params.get('reset');
  const authError = params.get('auth_error');
  if (authError) {
    history.replaceState(null, '', location.pathname);
    setAuthMode('login');
    setAuthFeedback('Google sign-in could not be completed. Try again or use email and password.', true);
  } else if (verificationToken) {
    history.replaceState(null, '', location.pathname);
    try {
      const result = await api('/api/auth/verify', {
        method: 'POST',
        body: JSON.stringify({ token: verificationToken }),
      });
      setAuthMode('login');
      setAuthFeedback(result.message);
    } catch (error) {
      setAuthMode('login');
      setAuthFeedback(error.message, true);
    }
  } else if (resetToken) {
    history.replaceState(null, '', location.pathname);
    state.resetToken = resetToken;
    setAuthMode('reset-complete');
  }
}

async function initializeAuth() {
  showAuthGate();
  bindAuthEvents();
  await completeAuthLink();
  try {
    const result = await api('/api/auth/me');
    if (result.user && result.csrf_token) {
      state.csrfToken = result.csrf_token;
      showWorkspace(result.user);
      return;
    }
  } catch (error) {
    setAuthFeedback(error.message, true);
  }
  try {
    const config = await api('/api/auth/config');
    if (!config.password_signup_available) {
      $('#register-form button[type="submit"]').disabled = true;
      $('#auth-copy').textContent = 'Account signup is temporarily unavailable while secure email and database services are configured.';
    }
    $('#google-signin').classList.toggle('hidden', !config.google_signin_available);
  } catch (error) {
    setAuthFeedback(error.message, true);
  }
}

function bindAuthEvents() {
  $('#auth-gate').addEventListener('click', (event) => {
    const modeButton = event.target.closest('[data-auth-mode]');
    if (modeButton) setAuthMode(modeButton.dataset.authMode);
  });
  $('#show-reset').addEventListener('click', () => setAuthMode('reset'));
  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    try {
      const result = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(data) });
      state.csrfToken = result.csrf_token;
      showWorkspace(result.user);
    } catch (error) {
      setAuthFeedback(error.message, true);
    }
  });
  $('#register-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try {
      const result = await api('/api/auth/register', { method: 'POST', body: JSON.stringify(data) });
      setAuthMode('login');
      setAuthFeedback(result.message);
    } catch (error) {
      setAuthFeedback(error.message, true);
    }
  });
  $('#reset-request-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try {
      const result = await api('/api/auth/password-reset/request', { method: 'POST', body: JSON.stringify(data) });
      setAuthFeedback(result.message);
    } catch (error) {
      setAuthFeedback(error.message, true);
    }
  });
  $('#reset-complete-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    data.token = state.resetToken;
    try {
      const result = await api('/api/auth/password-reset/complete', { method: 'POST', body: JSON.stringify(data) });
      state.resetToken = null;
      setAuthMode('login');
      setAuthFeedback(result.message);
    } catch (error) {
      setAuthFeedback(error.message, true);
    }
  });
}

function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

function toast(message, isError = false) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('error', isError);
  element.classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => element.classList.remove('visible'), 3500);
}

function formatDate(value, options = { month: 'short', day: 'numeric' }) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, options).format(date);
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date);
}

function relativeTime(value) {
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
  if (!Number.isFinite(minutes)) return '';
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

function initials(name = '') {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase() || '').join('');
}

function leadOptions(leads) {
  return leads.map((lead) => `<option value="${lead.id}">${escapeHtml(lead.name)} · ${escapeHtml(lead.phone || lead.email)}</option>`).join('');
}

function renderMetrics(dashboard) {
  const counts = dashboard.counts || {};
  const open = ['new', 'qualified', 'nurture', 'appointment', 'under_contract'].reduce((sum, stage) => sum + (counts[stage] || 0), 0);
  $('#metric-open').textContent = open;
  $('#metric-new').textContent = counts.new || 0;
  $('#metric-qualified').textContent = counts.qualified || 0;
  $('#metric-appointments').textContent = (dashboard.appointments || []).length;
  $('#nav-lead-count').textContent = dashboard.lead_count || 0;
  $('#context-leads').textContent = dashboard.lead_count || 0;

  const active = ['new', 'qualified', 'nurture', 'appointment', 'under_contract'];
  const activeCount = active.reduce((sum, stage) => sum + (counts[stage] || 0), 0);
  $('#pipeline-summary').innerHTML = [
    ['Active opportunities', activeCount], ['Qualified', counts.qualified || 0], ['Under contract', counts.under_contract || 0],
  ].map(([label, count]) => `<div class="pipeline-summary-item"><strong>${count}</strong>${label}</div>`).join('');

  const maxCount = Math.max(1, ...active.map((stage) => counts[stage] || 0));
  $('#stage-bars').innerHTML = active.map((stage) => {
    const count = counts[stage] || 0;
    return `<div class="stage-line"><span>${stageNames[stage]}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(0, (count / maxCount) * 100)}%"></div></div><strong>${count}</strong></div>`;
  }).join('');

  const appointments = dashboard.appointments || [];
  $('#appointment-list').innerHTML = appointments.length ? appointments.map((appointment) => {
    const date = new Date(appointment.starts_at);
    return `<div class="appointment-item"><div class="appointment-date"><strong>${Number.isNaN(date.getTime()) ? '—' : date.getDate()}</strong><small>${Number.isNaN(date.getTime()) ? '' : date.toLocaleString(undefined, { month: 'short' })}</small></div><div><strong>${escapeHtml(appointment.lead_name)}</strong><small>${escapeHtml(appointment.kind)}</small></div><span class="appointment-time">${escapeHtml(formatTime(appointment.starts_at))}</span></div>`;
  }).join('') : '<div class="empty-inline">No appointments on the calendar yet.</div>';
}

function renderLeads(leads = state.leads) {
  state.leads = leads;
  $('#nav-lead-count').textContent = state.dashboard?.lead_count ?? leads.length;
  $('#results-count').textContent = `${leads.length} ${leads.length === 1 ? 'lead' : 'leads'}`;
  $('#empty-leads').classList.toggle('hidden', leads.length > 0);
  $('#lead-rows').innerHTML = leads.map((lead) => {
    let qualification = {};
    try { qualification = JSON.parse(lead.qualification || '{}'); } catch { /* Keep the row renderable if old data is malformed. */ }
    const options = stageOrder.map((stage) => `<option value="${stage}" ${lead.stage === stage ? 'selected' : ''}>${stageNames[stage]}</option>`).join('');
    const contact = [lead.phone, lead.email].filter(Boolean).map(escapeHtml).join(' · ');
    const score = lead.score == null ? 'Not qualified' : `<span class="score-badge"><i class="score-dot ${lead.score >= 65 ? 'high' : ''}"></i>${lead.score} / 100</span>`;
    return `<tr data-lead-id="${lead.id}"><td><div class="lead-person"><span class="lead-avatar">${escapeHtml(initials(lead.name))}</span><span><strong>${escapeHtml(lead.name)}</strong><small>${contact || 'No contact details'}</small></span></div></td><td>${escapeHtml(lead.source)}</td><td>${score}</td><td><select class="stage-select" data-stage-for="${lead.id}" aria-label="Pipeline stage for ${escapeHtml(lead.name)}">${options}</select></td><td>${escapeHtml(formatDate(lead.created_at))}</td><td><div class="table-actions"><button class="small-action" data-qualify="${lead.id}">Qualify</button><button class="small-action" data-sms="${lead.id}" ${lead.consent_sms ? '' : 'disabled'} title="${lead.consent_sms ? 'Send SMS' : 'SMS consent required'}">SMS</button><button class="small-action" data-call="${lead.id}" ${lead.consent_call ? '' : 'disabled'} title="${lead.consent_call ? 'Call' : 'Call consent required'}">Call</button></div></td></tr>`;
  }).join('');
  $('#appointment-lead').innerHTML = leadOptions(leads);
  $('#appointment-lead').disabled = leads.length === 0;
}

function renderActivities(activities) {
  const icons = { lead: '+', qualification: '✳', sms: '↗', call: '☏', appointment: '◷', stage: '→', warning: '!', crm: '↔' };
  $('#activity-list').innerHTML = activities.length ? activities.map((activity) => {
    const person = activity.lead_name ? `<strong>${escapeHtml(activity.lead_name)}:</strong> ` : '';
    return `<div class="activity-item"><span class="activity-icon ${escapeHtml(activity.kind)}">${icons[activity.kind] || '·'}</span><span class="activity-copy">${person}${escapeHtml(activity.detail)}</span><time class="activity-time">${escapeHtml(relativeTime(activity.created_at))}</time></div>`;
  }).join('') : '<div class="empty-inline">Lead intake and outreach activity will appear here.</div>';
}

function statusDisplay(settings) {
  const configured = Boolean(settings.ai_provider && settings.ai_model && settings.ai_api_key_configured);
  const providerName = ({ openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Gemini' })[settings.ai_provider] || '';
  $('#llm-dot').classList.toggle('good', configured);
  $('#llm-dot').classList.toggle('bad', !configured);
  $('#llm-status').textContent = configured ? `${providerName} configured` : 'Not configured';
  $('#chat-dot').classList.toggle('good', configured);
  $('#chat-dot').classList.toggle('bad', !configured);
  $('#model-label').textContent = configured ? `${providerName} · ${settings.ai_model}` : 'LLM · not configured';
  $('#context-provider').textContent = configured ? `${providerName} · ${settings.ai_model}` : 'Rules only';
  $('#auto-followup-toggle').checked = settings.auto_followup_enabled;
}

async function loadSettings() {
  const settings = await api('/api/settings');
  state.settings = settings;
  statusDisplay(settings);
  const form = $('#settings-form');
  for (const key of ['crm_leads_url', 'sms_webhook_url', 'voice_webhook_url', 'calendar_webhook_url', 'ai_provider', 'ai_model']) {
    form.elements[key].value = settings[key] || '';
  }
  form.elements.crm_token.placeholder = settings.crm_token_configured ? 'Token saved; enter a new token to replace' : 'Leave blank to keep existing token';
  form.elements.webhook_key.placeholder = settings.webhook_key_configured ? 'Key saved; enter a new key to replace' : 'Optional shared secret';
  form.elements.ai_api_key.placeholder = settings.ai_api_key_configured ? 'Key saved; enter a new key to replace' : 'Paste your provider API key';
}

async function loadAll() {
  try {
    const [dashboard, leads, activities, settings] = await Promise.all([
      api('/api/dashboard'), api('/api/leads'), api('/api/activities'), api('/api/settings'),
    ]);
    state.dashboard = dashboard;
    state.settings = settings;
    renderMetrics(dashboard);
    renderLeads(leads);
    renderActivities(activities);
    statusDisplay(settings);
    const form = $('#settings-form');
    for (const key of ['crm_leads_url', 'sms_webhook_url', 'voice_webhook_url', 'calendar_webhook_url', 'ai_provider', 'ai_model']) form.elements[key].value = settings[key] || '';
    form.elements.crm_token.placeholder = settings.crm_token_configured ? 'Token saved; enter a new token to replace' : 'Leave blank to keep existing token';
    form.elements.webhook_key.placeholder = settings.webhook_key_configured ? 'Key saved; enter a new key to replace' : 'Optional shared secret';
    form.elements.ai_api_key.placeholder = settings.ai_api_key_configured ? 'Key saved; enter a new key to replace' : 'Paste your provider API key';
  } catch (error) {
    toast(error.message, true);
  }
}

function setView(view) {
  const name = document.getElementById(`view-${view}`) ? view : 'overview';
  $$('.view').forEach((element) => element.classList.toggle('active', element.id === `view-${name}`));
  $$('.nav-link').forEach((link) => link.classList.toggle('active', link.dataset.view === name));
  $('#page-label').textContent = ({ overview: 'Overview', pipeline: 'Pipeline', inbox: 'AI assistant', automations: 'Automations', settings: 'Connections' })[name];
  $('#sidebar').classList.remove('open');
  history.replaceState(null, '', `#${name}`);
}

function openLeadModal() {
  $('#lead-form').reset();
  $('#lead-modal').showModal();
  $('#lead-form').elements.name.focus();
}

function openAppointmentModal() {
  if (!state.leads.length) return toast('Add a lead before scheduling an appointment.', true);
  $('#appointment-form').reset();
  $('#appointment-modal').showModal();
}

async function handleLeadSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.consent_sms = form.elements.consent_sms.checked;
  data.consent_call = form.elements.consent_call.checked;
  try {
    const result = await api('/api/leads', { method: 'POST', body: JSON.stringify(data) });
    $('#lead-modal').close();
    toast(result.crm_sync && result.crm_sync !== 'CRM sync not configured.' ? `Lead added. CRM: ${result.crm_sync}` : 'Lead added to pipeline.');
    await loadAll();
    setView('pipeline');
  } catch (error) { toast(error.message, true); }
}

async function handleAppointmentSubmit(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api('/api/appointments', { method: 'POST', body: JSON.stringify(data) });
    $('#appointment-modal').close();
    toast('Appointment scheduled.');
    await loadAll();
  } catch (error) { toast(error.message, true); }
}

async function handleSettingsSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.auto_followup_enabled = $('#auto-followup-toggle').checked;
  for (const key of ['crm_token', 'webhook_key', 'ai_api_key']) if (!data[key]) delete data[key];
  try {
    const settings = await api('/api/settings', { method: 'POST', body: JSON.stringify(data) });
    state.settings = settings;
    statusDisplay(settings);
    form.elements.crm_token.value = '';
    form.elements.webhook_key.value = '';
    form.elements.ai_api_key.value = '';
    $('#settings-feedback').textContent = 'Connections saved.';
    setTimeout(() => { $('#settings-feedback').textContent = ''; }, 3000);
    toast('Connections saved locally.');
    await loadSettings();
  } catch (error) { toast(error.message, true); }
}

async function handleChatSubmit(event) {
  event.preventDefault();
  const input = $('#chat-input');
  const message = input.value.trim();
  if (!message) return;
  const messages = $('#chat-messages');
  messages.insertAdjacentHTML('beforeend', `<div class="message user-message"><div class="message-body"><p>${escapeHtml(message)}</p></div><span class="message-avatar">AT</span></div>`);
  input.value = '';
  messages.scrollTop = messages.scrollHeight;
  const pending = document.createElement('div');
  pending.className = 'message assistant-message';
  pending.innerHTML = '<span class="message-avatar">✳</span><div class="message-body"><p>Reviewing the latest CRM snapshot…</p></div>';
  messages.append(pending);
  messages.scrollTop = messages.scrollHeight;
  try {
    const result = await api('/api/agent/chat', { method: 'POST', body: JSON.stringify({ message }) });
    pending.querySelector('.message-body').innerHTML = `<p>${escapeHtml(result.answer).replace(/\n/g, '<br>')}</p>`;
  } catch (error) {
    pending.querySelector('.message-body').innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
  messages.scrollTop = messages.scrollHeight;
}

async function qualifyLead(id) {
  const button = $(`[data-qualify="${id}"]`);
  if (button) { button.disabled = true; button.textContent = 'Scoring…'; }
  try {
    const result = await api(`/api/leads/${id}/qualify`, { method: 'POST', body: '{}' });
    toast(`Lead qualified at ${result.qualification.score}/100 · moved to ${stageNames[result.lead.stage]}.`);
    await loadAll();
  } catch (error) { toast(error.message, true); }
}

function openAction(id, kind) {
  const lead = state.leads.find((item) => item.id === Number(id));
  if (!lead) return;
  state.actionLeadId = lead.id;
  state.actionKind = kind;
  $('#action-title').textContent = kind === 'sms' ? 'Send an SMS' : 'Start an outbound call';
  $('#action-eyebrow').textContent = kind === 'sms' ? 'SMS · MANUAL APPROVAL' : 'VOICE · MANUAL APPROVAL';
  $('#action-recipient').textContent = `${lead.name} · ${kind === 'sms' ? lead.phone : lead.phone}`;
  $('#action-message-label').style.display = kind === 'call' ? 'none' : 'grid';
  $('#action-submit').textContent = kind === 'sms' ? 'Send SMS' : 'Start call';
  $('#action-modal').showModal();
}

async function handleActionSubmit(event) {
  event.preventDefault();
  const message = event.currentTarget.elements.message.value;
  const kind = state.actionKind;
  try {
    const result = await api(`/api/leads/${state.actionLeadId}/${kind}`, { method: 'POST', body: JSON.stringify({ message }) });
    $('#action-modal').close();
    toast(kind === 'sms' ? 'SMS request sent to the configured webhook.' : 'Call request sent to the configured webhook.');
    if (!result.ok) toast(result.detail || 'Provider rejected request.', true);
    await loadAll();
  } catch (error) { toast(error.message, true); }
}

function bindEvents() {
  $('#logout-button').addEventListener('click', async () => {
    try {
      await api('/api/auth/logout', { method: 'POST', body: '{}' });
      state.user = null;
      state.csrfToken = null;
      showAuthGate();
      setAuthMode('login');
    } catch (error) {
      toast(error.message, true);
    }
  });
  $$('.nav-link, .text-link[data-view]').forEach((link) => link.addEventListener('click', (event) => {
    const view = link.dataset.view;
    if (!view) return;
    event.preventDefault();
    setView(view);
  }));
  $('#new-lead').addEventListener('click', openLeadModal);
  $('#pipeline-new-lead').addEventListener('click', openLeadModal);
  $('#empty-new-lead').addEventListener('click', openLeadModal);
  $('#schedule-from-automation').addEventListener('click', openAppointmentModal);
  $('#lead-form').addEventListener('submit', handleLeadSubmit);
  $('#appointment-form').addEventListener('submit', handleAppointmentSubmit);
  $('#settings-form').addEventListener('submit', handleSettingsSubmit);
  $('#chat-form').addEventListener('submit', handleChatSubmit);
  $('#action-form').addEventListener('submit', handleActionSubmit);
  $$('.close-modal').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  $('#menu-toggle').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  $('#refresh-status').addEventListener('click', () => loadSettings().then(() => toast('Connection settings refreshed.')).catch((error) => toast(error.message, true)));
  $('#auto-followup-toggle').addEventListener('change', async (event) => {
    try {
      const settings = await api('/api/settings', { method: 'POST', body: JSON.stringify({ auto_followup_enabled: event.target.checked }) });
      state.settings = settings;
      toast(`Automatic SMS follow-up ${event.target.checked ? 'enabled' : 'disabled'}.`);
    } catch (error) { event.target.checked = !event.target.checked; toast(error.message, true); }
  });
  $('#lead-search').addEventListener('input', async (event) => {
    try { renderLeads(await api(`/api/leads?q=${encodeURIComponent(event.target.value.trim())}`)); }
    catch (error) { toast(error.message, true); }
  });
  $('#lead-rows').addEventListener('click', (event) => {
    const qualify = event.target.closest('[data-qualify]');
    const sms = event.target.closest('[data-sms]');
    const call = event.target.closest('[data-call]');
    if (qualify) qualifyLead(qualify.dataset.qualify);
    if (sms) openAction(sms.dataset.sms, 'sms');
    if (call) openAction(call.dataset.call, 'call');
  });
  $('#lead-rows').addEventListener('change', async (event) => {
    const select = event.target.closest('[data-stage-for]');
    if (!select) return;
    try {
      await api(`/api/leads/${select.dataset.stageFor}/stage`, { method: 'POST', body: JSON.stringify({ stage: select.value }) });
      toast(`Lead moved to ${stageNames[select.value]}.`);
      await loadAll();
    } catch (error) { toast(error.message, true); await loadAll(); }
  });
  $('#chat-messages').addEventListener('click', (event) => {
    const suggestion = event.target.closest('.suggestion');
    if (suggestion) { $('#chat-input').value = suggestion.textContent; $('#chat-form').requestSubmit(); }
  });
  window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
}

function setDates() {
  const now = new Date();
  $('#today-label').textContent = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' }).format(now);
  $('#full-date').textContent = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' }).format(now);
}

initializeAuth();
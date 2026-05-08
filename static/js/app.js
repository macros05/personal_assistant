/** App entry point: event listeners, app state, and orchestration. */

import {
  postChat, postQuickAction, getResumen, getVuelos,
  getCalendarEvents, getHistory, deleteHistory,
  getContexto, putContexto, deleteContexto,
  getAuthStatus, revokeCalendar, consumeSSE,
  getTrackedFlights, deleteTrackedFlight, updateTrackedFlights,
  getTradingStatus,
  seoAudit, seoLaunchCampaign, getSeoStatus, getSeoProspects,
} from './api.js?v=4';

import {
  showToast, hideEmpty, scrollToBottom, setSendDisabled,
  autoResize, removeElement, updateTimestamp,
  renderMessage, appendTyping, renderFlights,
  renderCalendar, renderContexto, renderTodayEvents,
  renderTrackedFlights, renderTradingBot,
} from './ui.js?v=4';

// ── State ─────────────────────────────────────────────────────────────────────
let isStreaming       = false;
let calState          = { authenticated: false, credentials_present: false };
let _seoBusinessType  = 'local';

// ── Sidebar ───────────────────────────────────────────────────────────────────
const sidebar        = document.getElementById('sidebar');
const overlay        = document.getElementById('sidebar-overlay');
const sidebarToggle  = document.getElementById('sidebar-toggle');
const isMobile       = () => window.matchMedia('(max-width: 767px)').matches;

function openSidebar() {
  sidebar.classList.add('open');
  sidebar.classList.remove('collapsed');
  if (isMobile()) overlay.classList.add('visible');
}

function closeSidebar() {
  if (isMobile()) {
    sidebar.classList.remove('open');
    overlay.classList.remove('visible');
  } else {
    sidebar.classList.add('collapsed');
    overlay.classList.remove('visible');
  }
}

function toggleSidebar() {
  const mobileOpen   = isMobile() && sidebar.classList.contains('open');
  const desktopOpen  = !isMobile() && !sidebar.classList.contains('collapsed');
  if (mobileOpen || desktopOpen) closeSidebar(); else openSidebar();
}

// Close sidebar on mobile when user triggers an action
function maybeCloseSidebar() {
  if (isMobile()) closeSidebar();
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadHistory();
  loadContexto();
  loadCalendarStatus();
  loadTodayEvents();
  loadFlights();
  loadTrackedFlights();
  loadTradingBot();

  sidebarToggle.addEventListener('click', toggleSidebar);
  overlay.addEventListener('click', closeSidebar);

  document.getElementById('tb-refresh-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('tb-refresh-btn');
    if (btn) btn.style.opacity = '0.4';
    try {
      const data = await getTradingStatus();
      renderTradingBot(data);
    } catch { showToast('Error al actualizar trading bot'); }
    finally { if (btn) btn.style.opacity = ''; }
  });

  document.getElementById('fl-refresh-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('fl-refresh-btn');
    if (btn) btn.style.opacity = '0.4';
    try {
      const data = await updateTrackedFlights();
      renderTrackedFlights(data.flights || [], removeTrackedFlight);
      showToast('✓ Vuelos actualizados');
    } catch { showToast('Error al actualizar vuelos'); }
    finally { if (btn) btn.style.opacity = ''; }
  });

  document.getElementById('send-btn').addEventListener('click', sendMessage);
  document.getElementById('clear-history-btn').addEventListener('click', confirmClearHistory);
  document.getElementById('ctx-add-btn').addEventListener('click', () => openCtxModal(null, null));
  document.getElementById('ctx-modal-cancel').addEventListener('click', closeCtxModal);
  document.getElementById('ctx-modal-save').addEventListener('click', saveCtx);

  document.getElementById('ctx-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeCtxModal();
  });

  const input = document.getElementById('msg-input');
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  input.addEventListener('input', () => autoResize(input));

  // SEO Bot buttons
  document.getElementById('seo-audit-btn')?.addEventListener('click', () => {
    _showSeoPanel('seo-audit-view', 'Nueva Auditoría', '🔍');
  });
  document.getElementById('seo-campaign-btn')?.addEventListener('click', () => {
    _showSeoPanel('seo-campaign-view', 'Nueva Campaña', '🚀');
  });
  document.getElementById('seo-status-btn')?.addEventListener('click', () => {
    _showSeoPanel('seo-status-view', 'Estado Campaña', '📊');
    _loadSeoStatus();
  });
  document.getElementById('seo-prospects-btn')?.addEventListener('click', () => {
    _showSeoPanel('seo-prospects-view', 'Prospectos', '📋');
    _loadSeoProspects();
  });
  document.getElementById('seo-close-btn')?.addEventListener('click', _hideSeoPanel);
  document.getElementById('seo-audit-submit')?.addEventListener('click', _runSeoAudit);
  document.getElementById('seo-campaign-submit')?.addEventListener('click', _runSeoCampaign);
  document.getElementById('seo-status-refresh')?.addEventListener('click', _loadSeoStatus);
  document.getElementById('seo-prospects-refresh')?.addEventListener('click', _loadSeoProspects);
  document.getElementById('seo-limit')?.addEventListener('input', e => {
    document.getElementById('seo-limit-val').textContent = e.target.value;
  });
  document.querySelectorAll('.seo-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.seo-toggle').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _seoBusinessType = btn.dataset.type;
    });
  });

  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      maybeCloseSidebar();
      if (action === 'resumen') triggerResumen();
      else quickAction(action);
    });
  });

  const h = new Date().getHours();
  if (h >= 6 && h < 10) setTimeout(triggerResumen, 800);
});

// ── Morning summary ───────────────────────────────────────────────────────────
function triggerResumen() {
  if (isStreaming) return;
  hideEmpty();
  renderMessage('user', '📋 Resumen del día');
  scrollToBottom();
  startStreamingGet(getResumen);
}

// ── Quick actions ─────────────────────────────────────────────────────────────
function quickAction(action) {
  if (isStreaming) return;
  const labels = {
    resumen:  '📋 Resumen del día',
    week:     '📅 Mi semana',
    finances: '💰 Mis finanzas',
    wroclaw:  '✈️ Días hasta Wrocław',
    focus:    '🎯 ¿En qué enfocarme hoy?',
    trading:  '📈 Estado del trading bot',
  };
  hideEmpty();
  renderMessage('user', labels[action] || action);
  scrollToBottom();
  startStreaming(() => postQuickAction(action));
}

// ── Send message ──────────────────────────────────────────────────────────────
function sendMessage() {
  const input = document.getElementById('msg-input');
  const text  = input.value.trim();
  if (!text || isStreaming) return;
  input.value = '';
  autoResize(input);
  hideEmpty();
  renderMessage('user', text);
  scrollToBottom();
  maybeCloseSidebar();
  startStreaming(() => postChat(text));
}

// ── SSE streaming helpers ─────────────────────────────────────────────────────
async function startStreaming(fetchFn) {
  isStreaming = true;
  setSendDisabled(true);
  const typingId = appendTyping();
  scrollToBottom();
  try {
    const response = await fetchFn();
    removeElement(typingId);
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      renderMessage('assistant', `⚠️ Error: ${err.detail || response.statusText}`);
      return;
    }
    const aiId   = renderMessage('assistant', '', null, false);
    const bubble = document.querySelector(`#${aiId} .msg-bubble`);
    await consumeSSE(response, bubble, scrollToBottom);
    updateTimestamp(aiId);
  } catch (err) {
    removeElement(typingId);
    renderMessage('assistant', `⚠️ Error de conexión: ${err.message}`);
  } finally {
    isStreaming = false;
    setSendDisabled(false);
    document.getElementById('msg-input').focus();
  }
}

async function startStreamingGet(fetchFn) {
  isStreaming = true;
  setSendDisabled(true);
  const typingId = appendTyping();
  scrollToBottom();
  try {
    const response = await fetchFn();
    removeElement(typingId);
    if (!response.ok) {
      renderMessage('assistant', '⚠️ Error al generar el resumen.');
      return;
    }
    const aiId   = renderMessage('assistant', '', null, false);
    const bubble = document.querySelector(`#${aiId} .msg-bubble`);
    await consumeSSE(response, bubble, scrollToBottom);
    updateTimestamp(aiId);
  } catch (err) {
    removeElement(typingId);
    renderMessage('assistant', `⚠️ Error: ${err.message}`);
  } finally {
    isStreaming = false;
    setSendDisabled(false);
    document.getElementById('msg-input').focus();
  }
}

// ── Data loaders ──────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const data = await getHistory();
    if (data.messages?.length) {
      hideEmpty();
      data.messages.forEach(m => renderMessage(m.role, m.content, m.timestamp, false));
      scrollToBottom();
    }
  } catch (e) { console.error('Error cargando historial:', e); }
}

async function loadContexto() {
  try {
    const data = await getContexto();
    renderContexto(data.contexto || [], openCtxModal, deleteCtx);
  } catch {
    document.getElementById('ctx-list').innerHTML = '<div class="ctx-empty">Error al cargar</div>';
  }
}

async function loadCalendarStatus() {
  if (new URLSearchParams(location.search).get('cal') === 'ok') {
    history.replaceState({}, '', '/');
    showToast('✓ Google Calendar conectado');
    loadTodayEvents();
  }
  try {
    calState = await getAuthStatus();
  } catch {
    calState = { authenticated: false, credentials_present: false };
  }
  renderCalendar(calState, disconnectCalendar);
}

async function loadTodayEvents() {
  try {
    const data = await getCalendarEvents(1);
    if (!data.authenticated) return;
    renderTodayEvents(data.events || []);
    document.getElementById('today-section').style.display = 'flex';
  } catch { /* silent — calendar may not be connected */ }
}

async function loadTrackedFlights() {
  try {
    const data = await getTrackedFlights();
    renderTrackedFlights(data.flights || [], removeTrackedFlight);
  } catch {
    const p = document.getElementById('tracked-flights-panel');
    if (p) p.innerHTML = '<div class="fl-empty">Error al cargar</div>';
  }
}

async function removeTrackedFlight(id) {
  if (!confirm('¿Dejar de seguir este vuelo?')) return;
  try {
    const res = await deleteTrackedFlight(id);
    if (res.ok) { loadTrackedFlights(); showToast('🗑️ Vuelo eliminado'); }
    else { showToast('Error al eliminar'); }
  } catch { showToast('Error de conexión'); }
}

async function loadTradingBot() {
  try {
    const data = await getTradingStatus();
    renderTradingBot(data);
  } catch {
    const p = document.getElementById('trading-bot-panel');
    if (p) p.innerHTML = '<div class="fl-empty">Bot no disponible</div>';
  }
}

async function loadFlights() {
  const panel = document.getElementById('flight-panel');
  try {
    const data = await getVuelos(30);
    if (data.error && !data.flights?.length) {
      panel.innerHTML = `<div class="flight-card"><div class="flight-error">No disponible</div></div>`;
      return;
    }
    renderFlights(data);
  } catch {
    panel.innerHTML = `<div class="flight-card"><div class="flight-error">Error al cargar vuelos</div></div>`;
  }
}

// ── SEO Panel ─────────────────────────────────────────────────────────────────
function _showSeoPanel(viewId, title, icon) {
  document.getElementById('chat-area').style.display = 'none';
  document.getElementById('seo-panel').style.display = 'flex';
  document.getElementById('seo-panel-title').textContent = title;
  document.getElementById('seo-panel-icon').textContent  = icon;
  document.querySelectorAll('.seo-view').forEach(v => { v.style.display = 'none'; });
  const view = document.getElementById(viewId);
  if (view) view.style.display = '';
  maybeCloseSidebar();
}

function _hideSeoPanel() {
  document.getElementById('seo-panel').style.display  = 'none';
  document.getElementById('chat-area').style.display  = '';
}

// Audit
async function _runSeoAudit() {
  const url = document.getElementById('seo-url').value.trim();
  if (!url) { showToast('Introduce una URL válida'); return; }
  const btn = document.getElementById('seo-audit-submit');
  btn.disabled    = true;
  btn.textContent = '⏳ Auditando…';
  document.getElementById('seo-audit-results').style.display = 'none';
  try {
    const biz  = document.getElementById('seo-biz').value.trim() || undefined;
    const data = await seoAudit(url, biz);
    _renderAuditResults(data);
  } catch (e) {
    showToast('Error al auditar: ' + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = '🔍 Auditar';
  }
}

function _renderAuditResults(data) {
  if (data.detail && !data.score && !data.overall_score) {
    showToast(data.detail || 'Error en la auditoría'); return;
  }
  const score = data.score ?? data.overall_score ?? 0;
  const color = score >= 75 ? 'var(--green)' : score >= 50 ? 'var(--amber)' : 'var(--danger)';
  const badge = document.getElementById('seo-score-badge');
  badge.textContent        = score;
  badge.style.borderColor  = color;
  badge.style.color        = color;
  document.getElementById('seo-score-desc').textContent =
    score >= 75 ? 'Buen SEO — mejoras menores posibles.' :
    score >= 50 ? 'SEO mejorable — hay oportunidades claras.' :
                  'SEO deficiente — se recomienda acción urgente.';
  const link = document.getElementById('seo-proposal-link');
  if (data.proposal_url) { link.href = data.proposal_url; link.style.display = 'inline-flex'; }
  else { link.style.display = 'none'; }
  const issues   = data.issues || data.top_issues || [];
  const issuesEl = document.getElementById('seo-issues-list');
  if (issues.length) {
    issuesEl.innerHTML = issues.slice(0, 3).map(iss => {
      const text = typeof iss === 'string' ? iss : (iss.description || iss.message || iss.title || JSON.stringify(iss));
      const sev  = (iss.severity || '').toLowerCase();
      const cls  = sev === 'critical' ? 'critical' : sev === 'warning' ? 'warning' : 'info';
      const icon = cls === 'critical' ? '🔴' : cls === 'warning' ? '🟡' : '🔵';
      return `<div class="seo-issue ${cls}"><span class="seo-issue-icon">${icon}</span><span class="seo-issue-text">${_esc(text)}</span></div>`;
    }).join('');
  } else {
    issuesEl.innerHTML = '<div class="fl-empty">No se detectaron problemas graves.</div>';
  }
  document.getElementById('seo-audit-results').style.display = 'flex';
}

// Campaign
async function _runSeoCampaign() {
  const query    = document.getElementById('seo-query').value.trim();
  const location = document.getElementById('seo-location').value.trim();
  if (!query)    { showToast('Introduce el tipo de negocio'); return; }
  if (!location) { showToast('Introduce la ubicación'); return; }
  const btn = document.getElementById('seo-campaign-submit');
  btn.disabled    = true;
  btn.textContent = '⏳ Lanzando…';
  const resultEl  = document.getElementById('seo-campaign-result');
  resultEl.style.display = 'none';
  try {
    const data = await seoLaunchCampaign({
      query,
      location,
      business_type: _seoBusinessType,
      limit:   parseInt(document.getElementById('seo-limit').value, 10),
      dry_run: document.getElementById('seo-dryrun').checked,
    });
    const err = data.detail || data.error;
    resultEl.innerHTML = err
      ? `<span style="color:var(--danger)">⚠️ ${_esc(err)}</span>`
      : `<span style="color:var(--green)">✓ ${_esc(data.message || data.status || 'Campaña lanzada correctamente.')}</span>`;
    resultEl.style.display = 'block';
  } catch (e) {
    showToast('Error al lanzar: ' + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = '🚀 Lanzar Campaña';
  }
}

// Status
async function _loadSeoStatus() {
  const el = document.getElementById('seo-status-content');
  el.innerHTML = '<div class="skeleton skeleton-block"></div><div class="skeleton skeleton-block"></div>';
  try {
    const data = await getSeoStatus();
    _renderSeoStatus(data, el);
  } catch (e) {
    el.innerHTML = `<div class="seo-empty">Error: ${_esc(e.message)}</div>`;
  }
}

function _renderSeoStatus(data, container) {
  const err = data.detail || data.error;
  if (err) { container.innerHTML = `<div class="seo-empty">⚠️ ${_esc(err)}</div>`; return; }
  const state   = (data.state || data.status || 'idle').toLowerCase();
  const pct     = Math.min(100, data.progress_pct ?? data.progress ?? 0);
  const found   = data.prospects_found   ?? data.found    ?? '—';
  const analyzed = data.prospects_analyzed ?? data.analyzed ?? '—';
  const emailed  = data.emails_sent       ?? data.emailed  ?? '—';
  const running  = state === 'running';
  const done     = state === 'done' || state === 'completed';
  const stateColor = running ? 'var(--accent)' : done ? 'var(--green)' : 'var(--text-dim)';
  const stateLabel = running ? 'En ejecución' : done ? 'Completado' : 'Inactivo';
  container.innerHTML = `
    <div class="seo-status-card">
      <div class="seo-stat-row">
        <span class="seo-stat-label">Estado</span>
        <span class="seo-stat-val" style="color:${stateColor}">${stateLabel}</span>
      </div>
      <div class="seo-progress-bar"><div class="seo-progress-fill" style="width:${pct}%"></div></div>
      <div style="font-size:10px;color:var(--text-muted);text-align:right">${pct}% completado</div>
      <div style="height:1px;background:var(--border);margin:2px 0"></div>
      <div class="seo-stat-row"><span class="seo-stat-label">Encontrados</span><span class="seo-stat-val">${_esc(String(found))}</span></div>
      <div class="seo-stat-row"><span class="seo-stat-label">Analizados</span><span class="seo-stat-val">${_esc(String(analyzed))}</span></div>
      <div class="seo-stat-row"><span class="seo-stat-label">Emails enviados</span><span class="seo-stat-val">${_esc(String(emailed))}</span></div>
    </div>`;
}

// Prospects
async function _loadSeoProspects() {
  const el = document.getElementById('seo-prospects-content');
  el.innerHTML = '<div class="skeleton skeleton-block"></div><div class="skeleton skeleton-block"></div>';
  try {
    const data = await getSeoProspects();
    _renderSeoProspects(data, el);
  } catch (e) {
    el.innerHTML = `<div class="seo-empty">Error: ${_esc(e.message)}</div>`;
  }
}

function _renderSeoProspects(data, container) {
  const err = data.detail || data.error;
  if (err) { container.innerHTML = `<div class="seo-empty">⚠️ ${_esc(err)}</div>`; return; }
  const prospects = data.prospects || data.results || [];
  if (!prospects.length) {
    container.innerHTML = '<div class="seo-empty">Sin prospectos registrados.<br><span style="font-size:11px">Lanza una campaña para empezar.</span></div>';
    return;
  }
  const rows = prospects.map(p => {
    const name   = _esc(p.name || p.business_name || '—');
    const domain = _esc(p.domain || p.url || '—');
    const score  = p.score ?? p.seo_score ?? '—';
    const sc     = typeof score === 'number'
      ? (score >= 75 ? 'var(--green)' : score >= 50 ? 'var(--amber)' : 'var(--danger)')
      : 'var(--text)';
    const sent = (p.email_sent || p.emailed)
      ? '<span style="color:var(--green)">✓</span>'
      : '<span style="color:var(--text-muted)">—</span>';
    return `<tr>
      <td style="font-weight:500">${name}</td>
      <td style="color:var(--text-dim);font-size:11px;font-family:var(--mono)">${domain}</td>
      <td style="color:${sc};font-weight:600;font-family:var(--mono)">${score}</td>
      <td style="text-align:center">${sent}</td>
    </tr>`;
  }).join('');
  container.innerHTML = `
    <div class="seo-table-wrap">
      <table class="seo-table">
        <thead><tr><th>Negocio</th><th>Dominio</th><th>Score</th><th>Email</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function _esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Context modal ─────────────────────────────────────────────────────────────
function openCtxModal(clave, valor) {
  const isEdit = clave !== null;
  document.getElementById('ctx-modal-title').textContent = isEdit ? 'Editar contexto' : 'Añadir contexto';
  document.getElementById('ctx-modal-sub').textContent   = isEdit ? `Editando "${clave}".` : 'Añade un nuevo dato a tu contexto personal.';
  const claveInput    = document.getElementById('ctx-clave');
  claveInput.value    = clave || '';
  claveInput.readOnly = isEdit;
  const valorInput    = document.getElementById('ctx-valor');
  valorInput.value    = valor || '';
  document.getElementById('ctx-modal').classList.add('open');
  setTimeout(() => (isEdit ? valorInput : claveInput).focus(), 60);
}

function closeCtxModal() {
  document.getElementById('ctx-modal').classList.remove('open');
  document.getElementById('ctx-clave').value = '';
  document.getElementById('ctx-valor').value = '';
}

async function saveCtx() {
  const clave = document.getElementById('ctx-clave').value.trim();
  const valor = document.getElementById('ctx-valor').value.trim();
  if (!clave) { showToast('La clave no puede estar vacía.'); return; }
  if (!valor) { showToast('El valor no puede estar vacío.'); return; }
  try {
    const res = await putContexto(clave, valor);
    if (res.ok) { closeCtxModal(); loadContexto(); showToast('✓ Contexto actualizado'); }
    else { const err = await res.json().catch(() => ({})); showToast(err.detail || 'Error al guardar'); }
  } catch { showToast('Error de conexión'); }
}

async function deleteCtx(clave) {
  if (!confirm(`¿Eliminar "${clave}"?`)) return;
  try {
    const res = await deleteContexto(clave);
    if (res.ok) { loadContexto(); showToast(`🗑️ "${clave}" eliminado`); }
    else { showToast('Error al eliminar'); }
  } catch { showToast('Error de conexión'); }
}

// ── Calendar ──────────────────────────────────────────────────────────────────
async function disconnectCalendar() {
  if (!confirm('¿Desconectar Google Calendar? Se eliminará el token local.')) return;
  try {
    await revokeCalendar();
    calState.authenticated = false;
    renderCalendar(calState, disconnectCalendar);
    document.getElementById('today-section').style.display = 'none';
    showToast('🗑️ Calendario desconectado');
  } catch { showToast('Error al desconectar'); }
}

// ── History ───────────────────────────────────────────────────────────────────
async function confirmClearHistory() {
  if (!confirm('¿Borrar todo el historial de conversación?')) return;
  try {
    await deleteHistory();
    const messages = document.getElementById('messages');
    messages.innerHTML = `
      <div class="empty-state" id="empty-state">
        <div class="big-icon">🤖</div>
        <h2>Hola, Marcos</h2>
        <p>Tu asistente personal está listo. Usa las acciones rápidas o escribe lo que necesites.</p>
      </div>`;
    showToast('🗑️ Historial borrado');
  } catch { showToast('Error al borrar'); }
}

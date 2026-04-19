/** App entry point: event listeners, app state, and orchestration. */

import {
  postChat, postQuickAction, getResumen, getVuelos,
  getCalendarEvents, getHistory, deleteHistory,
  getContexto, putContexto, deleteContexto,
  getAuthStatus, revokeCalendar, consumeSSE,
} from './api.js';

import {
  showToast, hideEmpty, scrollToBottom, setSendDisabled,
  autoResize, removeElement, updateTimestamp,
  renderMessage, appendTyping, renderFlights,
  renderCalendar, renderContexto, renderTodayEvents,
} from './ui.js';

// ── State ─────────────────────────────────────────────────────────────────────
let isStreaming = false;
let calState    = { authenticated: false, credentials_present: false };

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadHistory();
  loadContexto();
  loadCalendarStatus();
  loadTodayEvents();
  loadFlights();

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

  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
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
    document.getElementById('today-section').style.display = 'block';
  } catch { /* silent — calendar may not be connected */ }
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

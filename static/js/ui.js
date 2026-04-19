/** DOM manipulation helpers. No fetch calls in this module. */

export function escapeHtml(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

export function formatFlightDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso + 'T12:00:00').toLocaleDateString('es-ES', { weekday: 'short', day: 'numeric', month: 'short' });
  } catch { return iso; }
}

export function sourceTag(sourceName) {
  if (!sourceName) return '';
  const primary = sourceName.split(' / ')[0].trim();
  const cls = primary === 'Ryanair'        ? 'ryanair'
            : primary === 'Vueling'        ? 'vueling'
            : primary === 'Google Flights' ? 'google'
            : primary === 'Skyscanner'     ? 'skyscanner'
            : '';
  return `<span class="flight-source-tag ${cls}">${escapeHtml(primary)}</span>`;
}

export function showToast(msg) {
  document.querySelector('.toast')?.remove();
  const t = document.createElement('div');
  t.className   = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

export function hideEmpty() {
  document.getElementById('empty-state')?.remove();
}

export function scrollToBottom() {
  const c = document.getElementById('messages');
  if (c) c.scrollTop = c.scrollHeight;
}

export function setSendDisabled(v) {
  const btn = document.getElementById('send-btn');
  if (btn) btn.disabled = v;
}

export function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

export function removeElement(id) {
  document.getElementById(id)?.remove();
}

export function updateTimestamp(id) {
  const el = document.querySelector(`#${id} .msg-time`);
  if (el) el.textContent = new Date().toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
}

/** Appends a chat message bubble. Returns the element id. */
export function renderMessage(role, content, timestamp, animate = true) {
  const container = document.getElementById('messages');
  const id  = `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const time = timestamp
    ? new Date(timestamp).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
    : new Date().toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
  const isUser = role === 'user';
  const div = document.createElement('div');
  div.className = `message ${isUser ? 'user' : 'assistant'}`;
  div.id = id;
  if (!animate) div.style.animation = 'none';
  div.innerHTML = `
    <div class="msg-avatar">${isUser ? 'M' : '🤖'}</div>
    <div class="msg-body">
      <div class="msg-bubble">${escapeHtml(content)}</div>
      <span class="msg-time">${time}</span>
    </div>
  `;
  container.appendChild(div);
  return id;
}

/** Appends the animated typing indicator. Returns the element id. */
export function appendTyping() {
  const container = document.getElementById('messages');
  const id  = `typing-${Date.now()}`;
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = id;
  div.innerHTML = `
    <div class="msg-avatar">🤖</div>
    <div class="msg-body">
      <div class="msg-bubble">
        <div class="typing-indicator">
          <div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>
        </div>
      </div>
    </div>
  `;
  container.appendChild(div);
  return id;
}

/** Renders the flight sidebar card from the /vuelos API response. */
export function renderFlights(data) {
  const panel    = document.getElementById('flight-panel');
  const cheapest = data.cheapest;
  const flights  = data.flights || [];
  const isAlert  = data.alert_low_price;
  const noSched  = data.no_schedule_match;

  if (!cheapest) {
    panel.innerHTML = '<div class="flight-card"><div class="flight-error">Sin vuelos disponibles en los próximos 30 días</div></div>';
    return;
  }

  const priceClass = isAlert ? 'cheap' : '';
  const cardClass  = isAlert ? 'cheap' : '';
  const isOptimal  = cheapest.optimal;

  let badges = '';
  if (isAlert)   badges += '<span class="flight-alert-badge">PRECIO BAJO</span>';
  if (isOptimal) badges += '<span class="flight-schedule-badge">Horario ideal</span>';

  let html = `
    <div class="flight-card ${cardClass}">
      <div class="flight-top">
        <span class="flight-route">AGP → WRO</span>
        <span style="display:flex;gap:4px;flex-wrap:wrap">${badges}</span>
      </div>
      <div class="flight-main">
        <span class="flight-price ${priceClass}">€${cheapest.price_eur}</span>
        <span class="flight-date">${formatFlightDate(cheapest.date)}</span>
      </div>
      <span class="flight-sub">${cheapest.departure_time ? 'Salida ' + cheapest.departure_time : ''} ${cheapest.flight_number || ''}</span>
      ${noSched ? '<div class="flight-no-schedule">⚠ Sin vuelos en horario ideal esta semana</div>' : ''}
  `;

  if (flights.length > 1) {
    html += '<div class="flight-list">';
    flights.slice(1, 5).forEach(f => {
      const cheap   = f.price_eur < 50;
      const optMark = f.optimal ? ' 🟦' : '';
      html += `
        <div class="flight-row">
          <span class="flight-row-date">${formatFlightDate(f.date)}${optMark}</span>
          <span style="display:flex;align-items:center;gap:4px">
            ${sourceTag(f.source)}
            <span class="flight-row-price ${cheap ? 'cheap' : ''}">€${f.price_eur}</span>
          </span>
        </div>`;
    });
    html += '</div>';
  }

  const ret = data.return_flights;
  if (ret && ret.cheapest) {
    const rc    = ret.cheapest;
    const rOpt  = rc.optimal;
    const rCheap = rc.price_eur < 50;
    html += `
      <div class="flight-return">
        <div class="flight-return-label">WRO → AGP (regreso)${rOpt ? ' · <span style="color:var(--accent)">Horario ideal</span>' : ''}</div>
        <div class="flight-row" style="background:none;padding:0">
          <span class="flight-row-date">${formatFlightDate(rc.date)} ${rc.departure_time || ''}</span>
          <span style="display:flex;align-items:center;gap:4px">
            ${sourceTag(rc.source)}
            <span class="flight-row-price ${rCheap ? 'cheap' : ''}">€${rc.price_eur}</span>
          </span>
        </div>
      </div>`;
  }

  const stats = data.source_stats || {};
  if (Object.keys(stats).length > 0) {
    const statParts = Object.entries(stats).map(([src, n]) => `${src}: ${n}`);
    html += `<div class="flight-sources-footer">${statParts.join(' · ')}</div>`;
  }

  html += '</div>';
  panel.innerHTML = html;
}

/** Renders Google Calendar auth status panel. Calls onDisconnect when button is clicked. */
export function renderCalendar(calState, onDisconnect) {
  const panel = document.getElementById('cal-status-panel');
  if (calState.authenticated) {
    panel.innerHTML = `
      <div class="cal-connected-row">
        <span class="cal-connected-label">● Conectado</span>
        <button class="cal-disconnect-btn" id="cal-disconnect-btn">Desconectar</button>
      </div>`;
    document.getElementById('cal-disconnect-btn').addEventListener('click', onDisconnect);
  } else if (calState.credentials_present) {
    panel.innerHTML = `<a href="/auth/google" class="cal-connect-btn"><span>🗓️</span> Conectar Google Calendar</a>`;
  } else {
    panel.innerHTML = `
      <div class="cal-notice">
        Añade <code>credentials.json</code> de Google Cloud Console para habilitar el calendario.
        <br><br>
        <a href="https://console.cloud.google.com/" target="_blank" style="color:var(--accent);font-size:10px;text-decoration:none;">
          Abrir Google Cloud Console ↗
        </a>
      </div>`;
  }
}

/** Renders the personal context key/value list. Calls onEdit(clave, valor) and onDelete(clave). */
export function renderContexto(rows, onEdit, onDelete) {
  const list = document.getElementById('ctx-list');
  if (!rows.length) { list.innerHTML = '<div class="ctx-empty">Sin datos aún</div>'; return; }
  list.innerHTML = '';
  rows.forEach(row => {
    const div = document.createElement('div');
    div.className = 'ctx-row';

    const content = document.createElement('div');
    content.className = 'ctx-row-content';
    content.innerHTML = `
      <span class="ctx-key">${escapeHtml(row.clave)}</span>
      <span class="ctx-val">${escapeHtml(row.valor)}</span>
    `;

    const btns = document.createElement('div');
    btns.className = 'ctx-row-btns';

    const editBtn = document.createElement('button');
    editBtn.className   = 'ctx-btn';
    editBtn.title       = 'Editar';
    editBtn.textContent = '✏️';
    editBtn.addEventListener('click', () => onEdit(row.clave, row.valor));

    const delBtn = document.createElement('button');
    delBtn.className   = 'ctx-btn del';
    delBtn.title       = 'Eliminar';
    delBtn.textContent = '×';
    delBtn.addEventListener('click', () => onDelete(row.clave));

    btns.appendChild(editBtn);
    btns.appendChild(delBtn);
    div.appendChild(content);
    div.appendChild(btns);
    list.appendChild(div);
  });
}

/** Renders today's calendar events in the sidebar. */
export function renderTodayEvents(events) {
  const list = document.getElementById('today-list');
  if (!events.length) {
    list.innerHTML = '<div class="today-empty">Sin eventos hoy</div>';
    return;
  }
  list.innerHTML = '';
  events.forEach(ev => {
    const div = document.createElement('div');
    div.className = 'today-event';
    const time = ev.start && ev.start.length > 10 ? ev.start.slice(11, 16) : 'Día';
    div.innerHTML = `
      <span class="today-time">${escapeHtml(time)}</span>
      <span class="today-title">${escapeHtml(ev.title)}</span>
    `;
    list.appendChild(div);
  });
}

/** All fetch/SSE calls to the backend. No DOM access in this module. */

export async function postChat(message) {
  return fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
}

export async function postQuickAction(action) {
  return fetch(`/quick-action/${action}`, { method: 'POST' });
}

export async function getResumen() {
  return fetch('/resumen');
}

export async function getVuelos(days = 30) {
  const res = await fetch(`/vuelos?days=${days}`);
  return res.json();
}

export async function getCalendarEvents(days = 1) {
  const res = await fetch(`/calendar/events?days=${days}`);
  return res.json();
}

export async function getHistory() {
  const res = await fetch('/history');
  return res.json();
}

export async function deleteHistory() {
  return fetch('/history', { method: 'DELETE' });
}

export async function getContexto() {
  const res = await fetch('/contexto');
  return res.json();
}

export async function putContexto(clave, valor) {
  return fetch('/contexto', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clave, valor }),
  });
}

export async function deleteContexto(clave) {
  return fetch(`/contexto/${encodeURIComponent(clave)}`, { method: 'DELETE' });
}

export async function getAuthStatus() {
  const res = await fetch('/auth/status');
  return res.json();
}

export async function revokeCalendar() {
  return fetch('/auth/google', { method: 'DELETE' });
}

/**
 * Reads an SSE stream from `response` and writes text into `bubble`.
 * Calls `onScroll()` after each text/status update.
 */
export async function consumeSSE(response, bubble, onScroll = () => {}) {
  const reader  = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer     = '';
  let fullText   = '';
  let streamDone = false;

  while (!streamDone) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try {
        const evt = JSON.parse(line.slice(6));
        if (evt.status) { bubble.textContent = evt.status; onScroll(); }
        if (evt.clear)  { bubble.textContent = ''; fullText = ''; }
        if (evt.text)   { fullText += evt.text; bubble.textContent = fullText; onScroll(); }
        if (evt.error)  { fullText = `⚠️ ${evt.error}`; bubble.textContent = fullText; onScroll(); }
        if (evt.done)   { streamDone = true; break; }
      } catch { /* incomplete chunk */ }
    }
  }
}

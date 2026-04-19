from datetime import date
from typing import Dict, List

# Seed data loaded into DB on first run. All values are plain strings.
DEFAULT_CONTEXT: Dict[str, str] = {
    "nombre":                     "Marcos",
    "edad":                       "21 años",
    "ubicacion":                  "Torremolinos, Málaga, España",
    "trabajo_empresa":            "Atico34 (legaltech)",
    "trabajo_puesto":             "AI Automation Engineer",
    "trabajo_inicio":             "Lunes 21 de abril de 2026",
    "horario_lunes":              "9:00 – 18:30",
    "horario_martes_jueves":      "8:00 – 17:00",
    "horario_viernes":            "8:00 – 14:00",
    "salario_actual":             "€21.000/año",
    "salario_en_2_meses":         "€23.000/año",
    "pareja":                     "Azafata, vive en Wrocław, Polonia",
    "proxima_visita_wroclaw":     "2026-05-28",
    "vuelos_frecuencia":          "Aproximadamente 2 veces al mes",
    "broker":                     "Trade Republic",
    "inversion_sp500":            "€200/mes",
    "inversion_global_defence":   "€100/mes",
    "inversion_europe_etf":       "€50/mes",
    "inversion_emerging_markets": "€50/mes",
    "inversion_bitcoin":          "€15/semana",
    "ahorros_liquidos":           "€5.300",
    "gasto_madre":                "€200/mes",
    "gasto_vuelos_wroclaw":       "~€300/mes",
    "gasto_varios":               "~€150/mes",
    "equipo_principal":           "MacBook Air M4",
    "github":                     "github.com/macros05",
}


def build_system_prompt(context_rows: List[Dict]) -> str:
    today = date.today()
    ctx   = {r["clave"]: r["valor"] for r in context_rows}
    nombre = ctx.get("nombre", "Marcos")

    wroclaw_note = ""
    wroclaw_raw  = ctx.get("proxima_visita_wroclaw", "")
    if wroclaw_raw:
        try:
            visit_date = date.fromisoformat(wroclaw_raw.strip().split()[0])
            days       = (visit_date - today).days
            wroclaw_note = f" → faltan {max(0, days)} días desde hoy"
        except Exception:
            pass

    lines = []
    for clave in sorted(ctx):
        suffix = wroclaw_note if clave == "proxima_visita_wroclaw" else ""
        lines.append(f"  - {clave}: {ctx[clave]}{suffix}")

    context_block = "\n".join(lines) if lines else "  (sin datos)"

    return f"""Eres el asistente personal de {nombre}, una herramienta de uso diario.

Sé práctico, directo y conciso. Responde SIEMPRE en español salvo que te pidan otro idioma explícitamente.

=== CONTEXTO PERSONAL ===
{context_block}

=== FECHA ACTUAL ===
Hoy es {today.strftime('%A, %d de %B de %Y')}.

=== COMPORTAMIENTO ===
- Usa el nombre "{nombre}" ocasionalmente, no en cada respuesta.
- Respuestas cortas y accionables salvo que la pregunta requiera detalle.
- Para finanzas, usa los números exactos del contexto.
- Para planificación, ten en cuenta el horario de trabajo exacto.
- Tienes acceso al historial reciente de la conversación.
- Si te preguntan algo no presente en el contexto, dilo claramente.
- Cuando uses una herramienta, presenta los resultados de forma clara y estructurada.
- La herramienta search_flights puede buscar vuelos entre CUALQUIER aeropuerto, no solo AGP↔WRO.
  Aeropuerto base del usuario: AGP (Málaga).
  Pareja: en KRK (Kraków) durante mayo; se muda a WRO (Wrocław) el 3 de junio.
  Si el usuario pide vuelos a Cracovia o Wrocław, usa el código correcto según la fecha.
"""


QUICK_ACTIONS: Dict[str, str] = {
    "resumen":  "Genera mi resumen matutino completo: eventos de hoy en el calendario, días hasta Wrocław, situación financiera y un foco concreto para el día.",
    "week":     "Resúmeme mi semana: trabajo, horario, y qué eventos o compromisos importantes tengo próximamente.",
    "finances": "Dame un resumen de mi situación financiera: inversiones, ahorros líquidos, gastos mensuales y si estoy en buena trayectoria.",
    "wroclaw":  "¿Cuántos días faltan para mi próxima visita a Wrocław? Dame también un recordatorio de los gastos que implica y si hay algo que deba preparar.",
    "focus":    "¿En qué debería enfocarme hoy? Ten en cuenta mi trabajo, finanzas, relación a distancia y cualquier contexto relevante para darme un plan concreto para el día.",
}

"""Financial snapshot assembled from the contexto SQLite table."""
from typing import Any

from database import get_all_contexto
from tools.base import Tool

_FINANCE_KEYS = {
    "ahorros_liquidos",
    "salario_actual",
    "salario_en_2_meses",
    "inversion_sp500",
    "inversion_global_defence",
    "inversion_europe_etf",
    "inversion_emerging_markets",
    "inversion_bitcoin",
    "gasto_madre",
    "gasto_vuelos_wroclaw",
    "gasto_varios",
    "broker",
}


def _parse_eur(value: str) -> int:
    """Extract integer euros from strings like '€200/mes' or '~€150/mes'."""
    try:
        digits = "".join(c for c in value.split("/")[0] if c.isdigit())
        return int(digits) if digits else 0
    except (ValueError, IndexError):
        return 0


class GetFinancesTool(Tool):
    name = "get_finances"
    description = (
        "Devuelve el resumen financiero completo de Marcos: ahorros, inversiones y gastos fijos. "
        "Úsala cuando el usuario pregunte por su situación económica, dinero o finanzas."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> dict[str, Any]:
        rows = await get_all_contexto()
        ctx  = {r["clave"]: r["valor"] for r in rows if r["clave"] in _FINANCE_KEYS}

        monthly_etf = sum(
            _parse_eur(ctx.get(k, ""))
            for k in ("inversion_sp500", "inversion_global_defence",
                      "inversion_europe_etf", "inversion_emerging_markets")
        )

        return {
            "ahorros_liquidos": ctx.get("ahorros_liquidos", "N/A"),
            "salario_actual":   ctx.get("salario_actual",   "N/A"),
            "salario_proximo":  ctx.get("salario_en_2_meses", "N/A"),
            "broker":           ctx.get("broker", "N/A"),
            "inversiones": {
                "sp500":            ctx.get("inversion_sp500",            "N/A"),
                "global_defence":   ctx.get("inversion_global_defence",   "N/A"),
                "europe_etf":       ctx.get("inversion_europe_etf",       "N/A"),
                "emerging_markets": ctx.get("inversion_emerging_markets", "N/A"),
                "bitcoin":          ctx.get("inversion_bitcoin",          "N/A"),
                "total_etf_mensual": f"€{monthly_etf}/mes",
            },
            "gastos_fijos": {
                "madre":          ctx.get("gasto_madre",          "N/A"),
                "vuelos_wroclaw": ctx.get("gasto_vuelos_wroclaw", "N/A"),
                "varios":         ctx.get("gasto_varios",         "N/A"),
            },
        }

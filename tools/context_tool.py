"""Tool that lets the model update a personal context key-value pair."""
from typing import Any

from database import upsert_contexto
from tools.base import Tool


class UpdateContextTool(Tool):
    name = "update_context"
    description = (
        "Actualiza un valor en el contexto personal de Marcos. "
        "Úsala cuando el usuario quiera guardar un nuevo dato: ahorros actualizados, "
        "próxima visita a Wrocław, cambio de salario, etc."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "Clave del contexto a actualizar."},
                "value": {"type": "string", "description": "Nuevo valor."},
            },
            "required": ["key", "value"],
        }

    async def execute(self, key: str, value: str, **_) -> dict[str, Any]:
        await upsert_contexto(key.strip(), value.strip())
        return {"ok": True, "key": key, "value": value}

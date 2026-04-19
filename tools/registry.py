"""Central registry — single source of truth for all agent tools."""
from tools.calendar import GetCalendarEventsTool, CreateCalendarEventTool
from tools.context_tool import UpdateContextTool
from tools.finances import GetFinancesTool
from tools.flights import SearchFlightsTool

_TOOLS = [
    GetCalendarEventsTool(),
    CreateCalendarEventTool(),
    SearchFlightsTool(),
    GetFinancesTool(),
    UpdateContextTool(),
]

_REGISTRY: dict = {t.name: t for t in _TOOLS}


def get_tool(name: str):
    return _REGISTRY.get(name)


def get_all_tools() -> list:
    """Return all registered tool instances."""
    return _TOOLS


def get_tool_schemas() -> list[dict]:
    return [t.to_openai_tool() for t in _TOOLS]

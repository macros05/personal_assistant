"""Central registry — single source of truth for all agent tools."""
from tools.calendar import GetCalendarEventsTool, CreateCalendarEventTool
from tools.context_tool import UpdateContextTool
from tools.finances import GetFinancesTool
from tools.fitness import GetFitnessDataTool
from tools.flight_tracker import TrackFlightTool
from tools.flights import SearchFlightsTool
from tools.polymarket import (
    GetPolymarketStatusTool,
    GetPolymarketTradesTool,
    GetPolymarketTopWalletsTool,
)
from tools.seo_bot import (
    AuditWebsiteTool,
    GetSEOCampaignStatusTool,
    GetSEOProspectsTool,
    StartSEOCampaignTool,
)
from tools.trading_bot import (
    GetTradingStatusTool,
    GetTradingLogsTool,
    GetTradingPnlTool,
    RestartTradingBotTool,
)

_TOOLS = [
    GetCalendarEventsTool(),
    CreateCalendarEventTool(),
    SearchFlightsTool(),
    GetFinancesTool(),
    GetFitnessDataTool(),
    UpdateContextTool(),
    TrackFlightTool(),
    GetTradingStatusTool(),
    GetTradingLogsTool(),
    GetTradingPnlTool(),
    RestartTradingBotTool(),
    GetPolymarketStatusTool(),
    GetPolymarketTradesTool(),
    GetPolymarketTopWalletsTool(),
    AuditWebsiteTool(),
    StartSEOCampaignTool(),
    GetSEOCampaignStatusTool(),
    GetSEOProspectsTool(),
]

_REGISTRY: dict = {t.name: t for t in _TOOLS}


def get_tool(name: str):
    return _REGISTRY.get(name)


def get_all_tools() -> list:
    """Return all registered tool instances."""
    return _TOOLS


def get_tool_schemas() -> list[dict]:
    return [t.to_openai_tool() for t in _TOOLS]

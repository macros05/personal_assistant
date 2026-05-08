import os

import httpx

from tools.base import Tool

# Both containers share the `root_marcos_network` docker network, so the
# personal_assistant container reaches seobot by its compose service name.
# Override via SEO_BOT_URL env var if running outside docker.
_SEO_BOT_URL = os.getenv("SEO_BOT_URL", "http://seobot:8002")


def _api_headers() -> dict:
    key = os.getenv("INTERNAL_SEO_API_KEY", "")
    return {"X-API-Key": key} if key else {}


class AuditWebsiteTool(Tool):
    name = "audit_website"
    description = (
        "Run a full SEO audit on any website. "
        "Use when user asks to analyze or audit a website."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Website URL to audit"},
                "business_name": {"type": "string", "description": "Business name (optional)"},
            },
            "required": ["url"],
        }

    async def execute(self, url: str, business_name: str = "", **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{_SEO_BOT_URL}/audit",
                json={"url": url, "business_name": business_name},
                headers=_api_headers(),
            )
            return r.json()


class StartSEOCampaignTool(Tool):
    name = "start_seo_campaign"
    description = (
        "Start an automated SEO outreach campaign scraping Google. "
        "Use when user says 'lanza campaña', 'empieza a mandar correos', 'busca clientes SEO'."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Type of business e.g. 'clínicas dentales'"},
                "location": {"type": "string", "description": "City e.g. 'Málaga'"},
                "limit": {"type": "integer", "description": "Number of prospects. Default 20"},
                "business_type": {"type": "string", "description": "local or online. Default: local"},
                "dry_run": {"type": "boolean", "description": "If true: analyze but don't send emails. Default: false"},
            },
            "required": ["query", "location"],
        }

    async def execute(
        self,
        query: str,
        location: str,
        limit: int = 20,
        business_type: str = "local",
        dry_run: bool = False,
        **kwargs,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_SEO_BOT_URL}/pipeline/run",
                json={
                    "query": query,
                    "location": location,
                    "limit": limit,
                    "business_type": business_type,
                    "dry_run": dry_run,
                },
                headers=_api_headers(),
            )
            return r.json()


class GetSEOCampaignStatusTool(Tool):
    name = "get_seo_campaign_status"
    description = (
        "Get current SEO campaign status — emails sent, pending, qualified prospects."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_SEO_BOT_URL}/pipeline/status", headers=_api_headers())
            return r.json()


class GetSEOProspectsTool(Tool):
    name = "get_seo_prospects"
    description = "Get list of evaluated prospects and their SEO scores."

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_SEO_BOT_URL}/outreach/status", headers=_api_headers())
            return r.json()

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str
    description: str

    @property
    @abstractmethod
    def schema(self) -> dict:
        """JSON Schema for tool parameters (OpenAI function-calling format)."""
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> dict[str, Any]:
        """Run the tool and return a result dict."""
        ...

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }

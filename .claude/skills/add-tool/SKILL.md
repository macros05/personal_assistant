# Add Tool Skill

User command: /add-tool <name>

## 5-Step Ritual (never skip a step)

1. Create tools/<name>.py subclassing BaseTool:

from tools.base import BaseTool

class <Name>Tool(BaseTool):
    name = "<name>"
    description = "<when to use this tool>"
    schema = {
        "type": "object",
        "properties": {
            "param": {"type": "string", "description": "..."}
        },
        "required": ["param"]
    }

    async def execute(self, param: str, **kwargs) -> dict:
        try:
            # implementation
            return {"result": ...}
        except Exception as e:
            return {"error": "tool_failed", "details": str(e)}

2. Register in tools/registry.py:
   from tools.<name> import <Name>Tool
   Add to get_all_tools() list

3. Add to agent.py _STATUS_LABELS if tool has status display

4. Add to CLAUDE.md tools table:
   | <name> | <description> | <when used> |

5. Smoke test:
   venv/bin/python -c "from tools.<name> import <Name>Tool; print('OK')"

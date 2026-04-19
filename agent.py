"""Agent loop: orchestrates Gemini calls and multi-round tool execution."""
import json
import logging
from typing import AsyncIterator, Optional

from google import genai
from google.genai import types

from context import build_system_prompt
from database import get_all_contexto, get_recent_messages, log_tool_call, save_message
from tools.registry import get_all_tools, get_tool

log = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 5

_STATUS_LABELS: dict[str, str] = {
    "get_calendar_events":   "🗓️ Consultando calendario…",
    "create_calendar_event": "🗓️ Creando evento…",
    "search_flights":        "✈️ Buscando vuelos…",
    "get_finances":          "💰 Consultando finanzas…",
    "update_context":        "📝 Actualizando contexto…",
}

_TYPE_MAP = {
    "object":  "OBJECT",
    "array":   "ARRAY",
    "string":  "STRING",
    "integer": "INTEGER",
    "number":  "NUMBER",
    "boolean": "BOOLEAN",
}


def _schema_to_gemini(schema: dict) -> dict:
    """Recursively convert OpenAI-style lowercase types to Gemini uppercase."""
    result = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            result[k] = _TYPE_MAP.get(v, v.upper())
        elif isinstance(v, dict):
            result[k] = _schema_to_gemini(v)
        elif isinstance(v, list):
            result[k] = [_schema_to_gemini(i) if isinstance(i, dict) else i for i in v]
        else:
            result[k] = v
    return result


async def run_agent(
    user_message: str,
    client: genai.Client,
    model: str,
    save_label: Optional[str] = None,
) -> AsyncIterator[str]:
    """Agent loop with Gemini native function calling. Yields SSE-formatted strings."""
    full_response = ""
    try:
        context_rows  = await get_all_contexto()
        system_prompt = build_system_prompt(context_rows)

        recent: list[dict] = await get_recent_messages(limit=20)
        contents: list[types.Content] = []
        for m in recent:
            role = "user" if m["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
        contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        tool_declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=_schema_to_gemini(tool.schema),
            )
            for tool in get_all_tools()
        ]

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=tool_declarations)],
            temperature=0.7,
        )

        for _ in range(MAX_TOOL_ROUNDS):
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

            candidate = response.candidates[0]

            function_calls = [
                part for part in candidate.content.parts
                if part.function_call is not None
            ]

            if not function_calls:
                text = "".join(p.text for p in candidate.content.parts if p.text)
                full_response = text
                yield f"data: {json.dumps({'text': text})}\n\n"
                break

            # Execute all tool calls in this round
            tool_results: list[types.Part] = []
            for part in function_calls:
                fc = part.function_call
                status = _STATUS_LABELS.get(fc.name, f"⚙️ {fc.name}…")
                yield f"data: {json.dumps({'status': status})}\n\n"

                tool = get_tool(fc.name)
                if tool:
                    try:
                        result = await tool.execute(**dict(fc.args))
                    except Exception as e:
                        log.exception("Tool %s raised an exception", fc.name)
                        result = {"error": str(e)}
                else:
                    result = {"error": f"Herramienta desconocida: {fc.name}"}

                await log_tool_call(fc.name, dict(fc.args), result)

                tool_results.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                ))

            yield f"data: {json.dumps({'clear': True})}\n\n"

            # Append model turn + tool results before next round
            contents.append(candidate.content)
            contents.append(types.Content(role="user", parts=tool_results))

        await save_message("user", save_label if save_label is not None else user_message)
        await save_message("assistant", full_response)

    except Exception as exc:
        log.exception("Agent error")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    finally:
        yield f"data: {json.dumps({'done': True})}\n\n"


async def run_once(
    system: str,
    user_message: str,
    client: genai.Client,
    model: str,
) -> AsyncIterator[str]:
    """One-shot Gemini call: no history, no tool calling. Yields SSE strings."""
    try:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
        )
        response = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=user_message)])],
            config=config,
        )
        text = response.text or ""
        if text:
            yield f"data: {json.dumps({'text': text})}\n\n"
    except Exception as exc:
        log.exception("run_once error")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
    finally:
        yield f"data: {json.dumps({'done': True})}\n\n"

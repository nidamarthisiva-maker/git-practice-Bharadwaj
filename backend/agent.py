"""Conversational incident agent built with plain LangChain (no LangGraph).

How it works (a classic tool-calling loop):

    user message
        |
        v
    Gemini (bound to the MCP tools)  --tool_calls?--> run MCP tool --> ToolMessage
        ^                                                                  |
        +------------------------------------------------------------------+
        |
        v  (no more tool calls)
    final natural-language answer

The LLM itself decides the intent: answering/asking questions, calling
``get_incident`` / ``list_incidents`` (read) or ``create_incident`` (write).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

logger = logging.getLogger("agent")

SYSTEM_PROMPT = """You are a helpful ServiceNow incident assistant chatting with an end user.

You can use these tools (they call the real ServiceNow instance):
- get_incident(number): details of ONE incident.
- list_incidents(state, priority, keyword, assignment_group, limit): search/list incidents.
- create_incident(short_description, description, urgency, impact, category, caller): create an incident.

Decide the user's intent yourself:
1. GET / SUMMARISE: the user asks about an incident number, status, or wants a summary
   -> call get_incident (one number) or list_incidents (several / filters), then summarise.
2. CREATE: the user wants to log, raise, open or create an incident
   -> call create_incident.
3. Anything else (greetings, how-to, unclear) -> answer briefly and ask what they need.

Rules:
- NEVER invent incident numbers or data. Only report what tools returned.
- To create an incident a short description is mandatory. If it is missing, ask for it.
  Optionally ask (in one message) for details, urgency and impact. If the user already
  gave enough information or says to just create it, create it immediately with defaults
  (urgency=3, impact=3) and tell the user which defaults were used.
- Call create_incident at most ONCE per user request. After success, report the incident
  number and key fields. If it fails, explain the error plainly - do not retry blindly.
- Urgency/impact: 1=High, 2=Medium, 3=Low. Map words like "critical/urgent/outage" to 1,
  "important" to 2.
- When summarising, be concise: number, short description, state, priority, assignee/group,
  opened date, and any resolution notes. For lists, use a short bullet list or table and
  add one sentence about the overall picture (e.g. counts by priority/state).
- If a tool returns found=false or an error, say so clearly and suggest a next step.
"""


@dataclass
class AgentResult:
    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def content_to_text(content: Any, sep: str = "") -> str:
    """Flatten LangChain message content (str | list of blocks) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return sep.join(parts)
    return str(content)


class IncidentAgent:
    def __init__(
        self,
        llm: BaseChatModel,
        tools: list[BaseTool],
        max_steps: int = 6,
        max_history_messages: int = 20,
    ) -> None:
        self.tools: dict[str, BaseTool] = {t.name: t for t in tools}
        self.llm = llm.bind_tools(tools)
        self.max_steps = max_steps
        self.max_history_messages = max_history_messages
        # session_id -> list[HumanMessage | AIMessage(final text)]
        self._histories: dict[str, list[BaseMessage]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    def reset(self, session_id: str) -> None:
        self._histories.pop(session_id, None)
        self._locks.pop(session_id, None)

    async def _run_tool(self, call: dict[str, Any]) -> ToolMessage:
        name = call["name"]
        call_id = call.get("id") or str(uuid.uuid4())
        tool = self.tools.get(name)
        if tool is None:
            return ToolMessage(
                content=json.dumps({"success": False, "error": f"Unknown tool '{name}'"}),
                tool_call_id=call_id,
                name=name,
            )
        try:
            result = await tool.ainvoke(
                {"name": name, "args": call.get("args") or {}, "id": call_id, "type": "tool_call"}
            )
            raw = result.content if isinstance(result, ToolMessage) else result
            text = content_to_text(raw, sep="\n") if not isinstance(raw, dict) else json.dumps(raw)
        except Exception as exc:  # noqa: BLE001 - report any tool failure to the LLM
            logger.exception("Tool %s failed", name)
            text = json.dumps({"success": False, "error": f"Tool execution failed: {exc}"})
        return ToolMessage(content=text, tool_call_id=call_id, name=name)

    # ------------------------------------------------------------------ #
    async def achat(self, session_id: str, user_message: str) -> AgentResult:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            history = self._histories.setdefault(session_id, [])
            history_window = history[-self.max_history_messages :]
            # window must start with a human turn
            while history_window and not isinstance(history_window[0], HumanMessage):
                history_window = history_window[1:]

            human = HumanMessage(content=user_message)
            messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT), *history_window, human]
            executed: list[dict[str, Any]] = []
            final_text = ""

            for step in range(self.max_steps):
                ai: AIMessage = await self.llm.ainvoke(messages)
                messages.append(ai)
                if not ai.tool_calls:
                    final_text = content_to_text(ai.content).strip()
                    break

                logger.info("step %d: model requested %s", step, [c["name"] for c in ai.tool_calls])
                for call in ai.tool_calls:
                    tool_msg = await self._run_tool(call)
                    messages.append(tool_msg)
                    executed.append(
                        {"name": call["name"], "args": call.get("args") or {}, "result": tool_msg.content}
                    )
            else:
                final_text = (
                    "I could not finish this request within the allowed number of steps. "
                    "Please try again or rephrase."
                )

            if not final_text:
                final_text = "I did not get a response from the model. Please try again."

            # keep only clean turns in memory (tool exchanges stay local to this turn)
            history.extend([human, AIMessage(content=final_text)])
            if len(history) > self.max_history_messages * 2:
                del history[: len(history) - self.max_history_messages * 2]

            return AgentResult(reply=final_text, tool_calls=executed)

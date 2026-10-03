"""Offline end-to-end test (no Gemini, no real ServiceNow needed).

Chain under test:
    FastAPI /chat -> IncidentAgent loop -> MCP client -> MCP server (stdio subprocess)
    -> ServiceNow REST client -> mock ServiceNow (started by the fixture)

The LLM is replaced by a tiny scripted model that "decides" to call tools based on
keywords, which verifies the whole plumbing except Gemini's own reasoning.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

ROOT = Path(__file__).resolve().parents[1]


class ScriptedLLM(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        last = messages[-1]
        if isinstance(last, ToolMessage):
            msg = AIMessage(content=f"TOOL_RESULT {last.name}: {last.content}")
        else:
            text = str(last.content).lower()
            if "create" in text:
                call = {"name": "create_incident",
                        "args": {"short_description": "Laptop cannot connect to Wi-Fi", "urgency": 2, "impact": 2},
                        "id": "c1", "type": "tool_call"}
            elif "inc0010001" in text:
                call = {"name": "get_incident", "args": {"number": "INC0010001"}, "id": "c2", "type": "tool_call"}
            elif "open" in text:
                call = {"name": "list_incidents", "args": {"state": "open", "keyword": "vpn"}, "id": "c3", "type": "tool_call"}
            else:
                call = None
            msg = AIMessage(content="", tool_calls=[call]) if call else AIMessage(content="Hello! How can I help?")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def client():
    port = _free_port()
    mock = subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}/scripts'); import uvicorn, mock_servicenow; "
         f"uvicorn.run(mock_servicenow.app, host='127.0.0.1', port={port}, log_level='warning')"],
    )
    try:
        for _ in range(50):
            try:
                requests.get(f"http://127.0.0.1:{port}/api/now/table/incident", timeout=0.5)
                break
            except requests.RequestException:
                time.sleep(0.2)
        os.environ.update(
            SERVICENOW_INSTANCE_URL=f"http://127.0.0.1:{port}",
            SERVICENOW_USERNAME="admin",
            SERVICENOW_PASSWORD="admin",
        )
        from backend.main import create_app

        with TestClient(create_app(lambda settings: ScriptedLLM())) as c:
            yield c
    finally:
        mock.terminate()


def test_health_lists_mcp_tools(client):
    data = client.get("/health").json()
    assert set(data["tools"]) == {"get_incident", "list_incidents", "create_incident"}


def test_smalltalk_no_tool(client):
    r = client.post("/chat", json={"message": "hi"}).json()
    assert r["tool_calls"] == []
    assert "Hello" in r["reply"]


def test_get_incident(client):
    r = client.post("/chat", json={"message": "summarise INC0010001"}).json()
    assert r["tool_calls"][0]["name"] == "get_incident"
    payload = json.loads(r["tool_calls"][0]["result"])
    assert payload["found"] is True
    assert payload["incident"]["state"] == "In Progress"


def test_list_incidents_with_keyword(client):
    r = client.post("/chat", json={"message": "show open incidents"}).json()
    payload = json.loads(r["tool_calls"][0]["result"])
    assert payload["count"] == 1
    assert "VPN" in payload["incidents"][0]["short_description"]


def test_create_incident(client):
    r = client.post("/chat", json={"message": "please create an incident"}).json()
    assert r["tool_calls"][0]["name"] == "create_incident"
    payload = json.loads(r["tool_calls"][0]["result"])
    assert payload["created"] is True
    assert payload["incident"]["number"].startswith("INC")
    assert payload["incident"]["state"] == "New"


def test_validation_errors_are_returned_not_raised(client):
    # invalid incident number goes straight through the MCP tool -> error payload
    from mcp_server import server

    import asyncio

    out = json.loads(asyncio.run(server.get_incident("ABC")))
    assert out["success"] is False

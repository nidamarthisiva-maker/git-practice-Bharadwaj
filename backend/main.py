"""FastAPI backend: LangChain agent + Gemini (Vertex AI) + ServiceNow MCP tools."""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from backend.agent import IncidentAgent
from backend.config import MCP_SERVER_PATH, Settings, get_settings
from backend.llm import build_llm
from backend.schemas import ChatRequest, ChatResponse, ToolCallInfo

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [backend] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backend")


def create_app(llm_factory: Callable[[Settings], BaseChatModel] = build_llm) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mcp_client = MultiServerMCPClient(
            {
                "servicenow": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(MCP_SERVER_PATH)],
                    "env": dict(os.environ),  # pass ServiceNow creds etc. to the subprocess
                }
            }
        )
        # One long-lived MCP session (single server process) for the app lifetime.
        async with mcp_client.session("servicenow") as session:
            tools = await load_mcp_tools(session)
            logger.info("Loaded MCP tools: %s", [t.name for t in tools])
            app.state.agent = IncidentAgent(
                llm=llm_factory(settings),
                tools=tools,
                max_steps=settings.max_agent_steps,
                max_history_messages=settings.max_history_messages,
            )
            app.state.tool_names = [t.name for t in tools]
            yield
        logger.info("MCP session closed")

    app = FastAPI(title="ServiceNow Incident Assistant", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    async def health(request: Request):
        return {
            "status": "ok",
            "model": settings.gemini_model,
            "tools": getattr(request.app.state, "tool_names", []),
        }

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        agent: IncidentAgent = request.app.state.agent
        session_id = body.session_id or str(uuid.uuid4())
        try:
            result = await agent.achat(session_id, body.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat failed")
            raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc
        return ChatResponse(
            session_id=session_id,
            reply=result.reply,
            tool_calls=[ToolCallInfo(**c) for c in result.tool_calls],
        )

    @app.delete("/sessions/{session_id}")
    async def reset_session(session_id: str, request: Request):
        request.app.state.agent.reset(session_id)
        return {"status": "cleared", "session_id": session_id}

    return app


app = create_app()

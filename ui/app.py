"""Streamlit conversational UI for the ServiceNow incident assistant."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(page_title="ServiceNow Incident Assistant", page_icon="🎫", layout="centered")

WELCOME = (
    "Hi! I'm your ServiceNow assistant. I can **look up / summarise incidents** "
    "or **create a new one**. Just tell me what you need."
)
EXAMPLES = [
    "Summarise incident INC0010001",
    "Show my open high priority incidents",
    "List incidents about VPN",
    "Create an incident: laptop cannot connect to Wi-Fi, urgency medium",
]


def init_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content": WELCOME, "tools": []}]
    if "pending" not in st.session_state:
        st.session_state.pending = None


def backend_health() -> dict | None:
    try:
        r = requests.get(f"{BACKEND_URL}/health", timeout=3)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def call_backend(message: str) -> dict:
    r = requests.post(
        f"{BACKEND_URL}/chat",
        json={"session_id": st.session_state.session_id, "message": message},
        timeout=120,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Backend error ({r.status_code}): {detail}")
    return r.json()


def render_tools(tools: list[dict]) -> None:
    if not tools:
        return
    with st.expander(f"🔧 Tools used ({len(tools)})"):
        for t in tools:
            st.markdown(f"**{t['name']}**")
            st.code(json.dumps(t["args"], indent=2, ensure_ascii=False), language="json")
            result = t["result"]
            try:
                result = json.dumps(json.loads(result), indent=2, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
            st.code(result[:3000], language="json")


init_state()

# ----------------------------- sidebar ----------------------------------- #
with st.sidebar:
    st.header("🎫 Incident Assistant")
    health = backend_health()
    if health:
        st.success(f"Backend online\n\nModel: `{health['model']}`")
        st.caption("Tools: " + ", ".join(health.get("tools", [])))
    else:
        st.error(f"Backend not reachable at {BACKEND_URL}")

    if st.button("🗑️ New conversation", use_container_width=True):
        try:
            requests.delete(f"{BACKEND_URL}/sessions/{st.session_state.session_id}", timeout=5)
        except requests.RequestException:
            pass
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages = [{"role": "assistant", "content": WELCOME, "tools": []}]
        st.rerun()

    st.markdown("**Try asking:**")
    for i, example in enumerate(EXAMPLES):
        if st.button(example, key=f"ex{i}", use_container_width=True):
            st.session_state.pending = example

# ------------------------------ chat ------------------------------------- #
st.title("ServiceNow Incident Assistant")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        render_tools(msg.get("tools", []))

typed = st.chat_input("Ask about an incident or describe a problem to log...")
prompt = typed or st.session_state.pending
st.session_state.pending = None

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt, "tools": []})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                data = call_backend(prompt)
                reply, tools = data["reply"], data.get("tool_calls", [])
            except (requests.RequestException, RuntimeError) as exc:
                reply, tools = f"⚠️ {exc}", []
        st.markdown(reply)
        render_tools(tools)
    st.session_state.messages.append({"role": "assistant", "content": reply, "tools": tools})

# ServiceNow Incident Assistant — Specification & Guide

A conversational assistant where a user chats in a **Streamlit** UI, a **Gemini (Vertex AI)** model
running inside a **LangChain** tool-calling loop decides whether the user wants to *get / summarise*
or *create* an incident, and calls **ServiceNow REST APIs through an MCP server**. A **FastAPI**
backend glues everything together. **LangGraph is not used.**

---

## 1. Goals & Scope

| # | Requirement | How it is met |
|---|-------------|---------------|
| 1 | MCP server for the ServiceNow `incident` table | `mcp_server/server.py` (FastMCP, stdio) |
| 2 | Get / summarise incidents | Tools `get_incident`, `list_incidents` + LLM summarisation |
| 3 | Create incidents | Tool `create_incident` |
| 4 | LLM decides intent (get vs create) by reasoning | Gemini function calling, guided by the system prompt |
| 5 | Conversational chat UI (request / response) | Streamlit `st.chat_input` / `st.chat_message` |
| 6 | LangChain + FastAPI, **no LangGraph** | Manual tool-calling loop using `langchain-core` |
| 7 | Gemini via Vertex AI | `langchain-google-vertexai` → `ChatVertexAI` |

**Out of scope (can be added later):** update/resolve/close incidents, attachments, OAuth to ServiceNow,
persistent chat storage, user authentication to the API.

---

## 2. Architecture

```
┌────────────────┐  HTTP/JSON   ┌───────────────────────────────────────────────┐
│  Streamlit UI  │ ───────────► │ FastAPI backend (backend/main.py)             │
│  ui/app.py     │ ◄─────────── │   POST /chat  GET /health  DELETE /sessions   │
└────────────────┘              │                                               │
                                │  IncidentAgent (backend/agent.py)             │
                                │   LangChain messages + ChatVertexAI.bind_tools│
                                │        │ tool_calls?                          │
                                │        ▼                                      │
                                │  langchain-mcp-adapters (MCP client)          │
                                └───────────────┬───────────────────────────────┘
                                                │ MCP over stdio (sub-process)
                                ┌───────────────▼───────────────┐
                                │ MCP server (mcp_server/server.py)│
                                │  get_incident / list_incidents │
                                │  / create_incident             │
                                └───────────────┬───────────────┘
                                                │ HTTPS REST (Basic auth)
                                ┌───────────────▼───────────────┐
                                │ ServiceNow Table API           │
                                │ /api/now/table/incident        │
                                └────────────────────────────────┘
        Gemini on Vertex AI  ◄── called by the backend (ChatVertexAI) for reasoning
```

### Request flow (example: *"Summarise INC0010001"*)

1. UI sends `POST /chat {session_id, message}`.
2. Agent builds `[SystemMessage, …history, HumanMessage]` and calls Gemini (tools bound).
3. Gemini replies with a **tool call** `get_incident(number="INC0010001")` → this *is* the intent detection.
4. Agent executes the tool through the MCP client → MCP server → ServiceNow REST → JSON back.
5. Result is appended as a `ToolMessage`; Gemini is called again and writes a natural-language summary.
6. Backend returns `{reply, tool_calls}`; UI shows the reply and a "Tools used" expander.

For *"Create an incident: VPN is down"* Gemini chooses `create_incident(...)` instead. For small talk, or when
information is missing (e.g. no title), it answers / asks a question without calling any tool.

---

## 3. Technology & Tested Versions

| Component | Package | Version tested |
|-----------|---------|----------------|
| Python | | 3.12 (3.10+ required) |
| LLM framework | `langchain-core` | 1.6.6 |
| Gemini on Vertex | `langchain-google-vertexai` | 3.2.4 |
| MCP SDK | `mcp` | 1.30.0 |
| MCP ↔ LangChain | `langchain-mcp-adapters` | 0.3.2 |
| API | `fastapi`, `uvicorn` | 0.142 / 0.54 |
| UI | `streamlit` | 1.64 |

> **Why no `langchain` meta-package?** From LangChain 1.0 onward the `langchain` package depends on
> LangGraph (its `create_agent` is built on it). To honour "no LangGraph" the project uses only
> `langchain-core` (messages, tools, chat-model interface) and implements the small agent loop itself in
> `backend/agent.py`. `pip list | grep -i langgraph` returns nothing in the environment.

> **What was verified:** 8 automated tests pass (`pytest tests`): the full chain
> FastAPI → agent → MCP client → MCP server (real sub-process) → REST client → mock ServiceNow, MCP tool
> schema → Gemini function-declaration conversion, and headless Streamlit UI runs. The actual Gemini call
> requires your GCP credentials and was therefore replaced by a scripted model in tests.

---

## 4. Project Structure

```
servicenow-mcp-chat/
├── .env.example               # copy to .env and fill in
├── requirements.txt
├── SPECIFICATION.md           # this document
├── mcp_server/
│   ├── server.py              # MCP server (3 tools)
│   └── servicenow_client.py   # ServiceNow REST client
├── backend/
│   ├── main.py                # FastAPI app + MCP lifecycle
│   ├── agent.py               # LangChain tool-calling loop + system prompt
│   ├── llm.py                 # ChatVertexAI factory
│   ├── config.py              # env settings
│   └── schemas.py             # API models
├── ui/
│   └── app.py                 # Streamlit chat UI
├── scripts/
│   └── mock_servicenow.py     # fake ServiceNow for demos / tests
└── tests/
    ├── test_end_to_end.py
    └── test_ui.py
```

---

## 5. Prerequisites

### 5.1 ServiceNow
* A ServiceNow instance. Free option: a **Personal Developer Instance** at <https://developer.servicenow.com>.
* A user with REST access and rights to read/create `incident` (and read `sys_user` if you use the *caller*
  field). For a PDI the `admin` user works; for real systems create a dedicated integration user with
  roles such as `itil` (+ `rest_api_explorer` is useful while testing).
* Note the instance URL: `https://devXXXXX.service-now.com`.
* PDIs hibernate when idle – wake yours up before testing.

### 5.2 Google Cloud / Vertex AI
1. Create or choose a GCP project with billing enabled.
2. Enable the API: `gcloud services enable aiplatform.googleapis.com`
3. Authenticate (pick one):
   * Local development: `gcloud auth application-default login`
   * Service account: grant role **Vertex AI User** (`roles/aiplatform.user`), download the JSON key and set
     `GOOGLE_APPLICATION_CREDENTIALS=/abs/path/key.json`.
4. Confirm the Gemini model is available in your region (default `gemini-2.5-flash` in `us-central1`).
   You can change `GEMINI_MODEL` / `GOOGLE_CLOUD_LOCATION` in `.env` without code changes.

---

## 6. Setup & Execution Steps

### 6.1 Install
```bash
cd servicenow-mcp-chat
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
```

### 6.2 Configure `.env`
```
SERVICENOW_INSTANCE_URL=https://devXXXXX.service-now.com
SERVICENOW_USERNAME=...
SERVICENOW_PASSWORD=...
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash
BACKEND_URL=http://localhost:8000
```

### 6.3 Run (two terminals)

The MCP server is started automatically by the backend as a sub-process – you do **not** start it manually.

**Terminal 1 – backend**
```bash
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```
Expected log lines: `Loaded MCP tools: ['get_incident', 'list_incidents', 'create_incident']`.
Check: <http://localhost:8000/health> and the interactive docs at <http://localhost:8000/docs>.

**Terminal 2 – UI**
```bash
source .venv/bin/activate
streamlit run ui/app.py
```
Open <http://localhost:8501>.

### 6.4 Try it without ServiceNow (mock mode)
```bash
python scripts/mock_servicenow.py          # terminal 0, listens on :8081
```
Set in `.env`: `SERVICENOW_INSTANCE_URL=http://localhost:8081`, `SERVICENOW_USERNAME=admin`,
`SERVICENOW_PASSWORD=admin`. The mock contains INC0010001–INC0010004. Gemini is still real, so GCP
credentials are still needed.

### 6.5 Run tests (no GCP / ServiceNow needed)
```bash
python -m pytest -q tests
```

### 6.6 Debug the MCP server on its own (optional)
```bash
npx @modelcontextprotocol/inspector python mcp_server/server.py
```

---

## 7. Example Conversations

| User says | LLM intent | Tool(s) called |
|-----------|-----------|----------------|
| "Summarise INC0010001" | get | `get_incident` |
| "What's the status of INC0010002?" | get | `get_incident` |
| "Show open high priority incidents" | get (list) | `list_incidents(state=open, priority=high)` |
| "Any incidents about VPN?" | get (search) | `list_incidents(keyword=VPN)` |
| "Create an incident: laptop can't connect to Wi-Fi, urgency medium" | create | `create_incident` |
| "I want to raise a ticket" | create, info missing | none – asks for the short description |
| "hi" | chit-chat | none |

---

## 8. REST API (backend)

### `POST /chat`
Request
```json
{ "session_id": "optional-uuid", "message": "Summarise INC0010001" }
```
Response
```json
{
  "session_id": "6b0e...",
  "reply": "INC0010001 – VPN connection drops ... currently In Progress ...",
  "tool_calls": [
    { "name": "get_incident", "args": {"number": "INC0010001"}, "result": "{\"success\": true, ...}" }
  ]
}
```
Errors: `422` invalid body (empty / >4000 chars), `502` agent / Gemini / MCP failure with `detail`.

### `GET /health`
`{"status":"ok","model":"gemini-2.5-flash","tools":["get_incident","list_incidents","create_incident"]}`

### `DELETE /sessions/{session_id}` – clears conversation memory.

---

## 9. MCP Tool Specification

All tools return a **JSON string**. Errors are returned as `{"success": false, "error": "..."}` (not raised) so
the LLM can explain them to the user.

### `get_incident(number: str)`
* Validates `INC` + ≥4 digits (upper-cased automatically).
* Success: `{"success":true,"found":true,"incident":{...}}`; unknown number: `found:false`.
* Fields: number, short_description, description, state, priority, urgency, impact, category, subcategory,
  caller_id, assigned_to, assignment_group, opened_at, sys_updated_on, resolved_at, close_code, close_notes, sys_id
  (display values, e.g. state = "In Progress").

### `list_incidents(state="open", priority="", keyword="", assignment_group="", limit=10)`
* `state`: `open` (active=true), `new`, `in_progress`, `on_hold`, `resolved`, `closed`, `canceled`, `all`.
* `priority`: `1-5` or `critical|high|moderate|low|planning`.
* `keyword`: matches short description **or** description.
* `limit`: clamped to 1–25. Sorted newest first.
* Returns `{"success":true,"count":N,"incidents":[...]}`.

### `create_incident(short_description, description="", urgency=3, impact=3, category="", caller="")`
* `short_description` required (truncated to 160 chars). `urgency`/`impact`: 1 High, 2 Medium, 3 Low.
* `caller` (user name / e-mail / full name) is resolved to a `sys_user` sys_id; if not found an error is
  returned so the assistant can ask the user instead of silently creating a wrong record.
* ServiceNow computes **priority** from urgency × impact.
* Returns `{"success":true,"created":true,"incident":{...number...}}`.

---

## 10. Code Walk-through

### 10.1 `mcp_server/servicenow_client.py`
* `ServiceNowClient` wraps a `requests.Session` with Basic auth.
* **Retries only on GET** (429/502/503/504). POST is never retried → no duplicate incidents.
* `sysparm_display_value=true` + `sysparm_exclude_reference_link=true` → readable, flat values for the LLM.
* `clean_query_value()` strips `^` (ServiceNow's AND separator) from user text → blocks encoded-query injection.
* `_request()` converts every failure (network, 4xx/5xx, HTML from a hibernating PDI) into `ServiceNowError`
  with a clear message.

### 10.2 `mcp_server/server.py`
* `FastMCP("servicenow-incidents")` with three `@mcp.tool()` functions; parameter descriptions come from
  `Annotated[..., Field(description=...)]` and become the tool schema Gemini sees – good descriptions = good
  tool selection.
* Tools are `async` and call the blocking REST client with `asyncio.to_thread`.
* The ServiceNow client is created lazily, so the server starts even if credentials are missing and then
  reports a friendly error.
* Logging goes to **stderr** – stdout belongs to the stdio MCP protocol.
* Optional parameters use plain defaults (`""`, `3`) rather than `Optional[...]` to keep the JSON schema simple
  for Gemini.

### 10.3 `backend/agent.py` (the "brain", no LangGraph)
* `llm.bind_tools(tools)` makes Gemini able to emit function calls using the MCP tool schemas.
* `achat()` loop: invoke model → if `ai.tool_calls` run each tool via `tool.ainvoke(...)`, append
  `ToolMessage`s → invoke again → stop when there are no tool calls or after `MAX_AGENT_STEPS`.
* The whole `AIMessage` is appended inside the turn (keeps any model metadata such as thought signatures).
* **Memory:** per-session list of `HumanMessage` + final `AIMessage` text only. Tool exchanges stay local to the
  turn, so trimming history can never split a function-call/response pair (which Gemini rejects). History is
  capped at `MAX_HISTORY_MESSAGES`.
* A per-session `asyncio.Lock` prevents two simultaneous requests from interleaving the same conversation.
* `SYSTEM_PROMPT` defines intent rules: never invent data, ask for missing short description, create at most
  once, default urgency/impact = 3 and say so, summarise concisely.
* Tool failures are returned to the model as JSON errors instead of crashing the request.

### 10.4 `backend/main.py`
* `create_app(llm_factory)` builds the app (the factory parameter lets tests inject a fake LLM).
* **Lifespan:** starts the MCP server sub-process through `MultiServerMCPClient.session("servicenow")`, loads
  tools with `load_mcp_tools`, creates the agent, and shuts the MCP process down on exit. One long-lived
  session = one MCP process (not one per tool call).
* The full `os.environ` is passed to the MCP sub-process so `.env` values reach it.

### 10.5 `backend/llm.py`, `config.py`, `schemas.py`
* `ChatVertexAI(model_name, project, location, temperature=0, max_output_tokens=2048, max_retries=3)`.
* Settings are read from env (`GEMINI_MODEL`, `GOOGLE_CLOUD_PROJECT`, …).
* Pydantic models validate `/chat` input and shape the response.

### 10.6 `ui/app.py`
* `st.session_state` holds `session_id`, the message list and a pending example prompt.
* Chat history is re-rendered each run; `st.chat_input` + `st.chat_message` give the chat look.
* Backend call with a spinner; errors are shown as a chat message instead of a stack trace.
* "🔧 Tools used" expander shows which MCP tool ran with what arguments and the raw result – useful for
  trust and debugging.
* Sidebar: backend health, model and tools, *New conversation* (clears server memory), example prompts.

### 10.7 `scripts/mock_servicenow.py`
In-memory FastAPI fake of the Table API (`incident`, `sys_user`) supporting the query subset this project
uses (`=`, `LIKE`, `^OR`, `active`, `assignment_group.name`). Used for demos and automated tests.

---

## 11. Error Handling Matrix

| Situation | Behaviour |
|-----------|-----------|
| Wrong ServiceNow credentials / 401 | Tool returns `{"success":false,"error":"ServiceNow returned HTTP 401..."}`; assistant explains |
| PDI hibernating (HTML response) | Error message tells user to wake the instance |
| Unknown incident number | `found:false`; assistant says it doesn't exist |
| Caller not found on create | Error returned; assistant asks for a valid caller |
| Invalid urgency / impact / state / priority | Validation error returned by the tool |
| Gemini quota / auth error | Backend returns `502`; UI shows ⚠️ message |
| Model loops on tools | Stops after `MAX_AGENT_STEPS` with an apology message |
| Backend down | UI sidebar shows "Backend not reachable" |

---

## 12. Security Notes

* Never commit `.env` (it is in `.gitignore`). Use a **least-privilege** ServiceNow integration user.
* For production prefer ServiceNow **OAuth** and a secrets manager (Google Secret Manager) over Basic auth.
* The API has **no authentication** and CORS is `*` for local development. Put it behind an API gateway / IAP,
  restrict origins and add per-user auth before exposing it.
* Query-injection guard (`^` removal) is applied to user text used in encoded queries.
* The model can only call the three tools; there is no delete/update capability.
* Create is a write action: the prompt limits it to one call per request, but consider adding an explicit
  "Confirm?" step (or UI confirm button) for stricter environments.

---

## 13. Production Hardening Checklist

* Replace in-memory sessions with Redis / Firestore; set a TTL.
* Add auth (OIDC/JWT), rate limiting and request logging with correlation ids.
* Use streaming responses (`astream`) + `st.write_stream` for lower perceived latency.
* Containerise (one image for backend + MCP server, one for UI); run uvicorn with multiple workers only
  after moving sessions out of memory.
* Add retries/circuit-breaker metrics around Vertex AI and ServiceNow.
* Optionally run the MCP server as a separate service (streamable-HTTP transport) instead of a sub-process.

---

## 14. Extending

* **New tool** (e.g. `add_work_note`, `update_incident`, `resolve_incident`): add an `@mcp.tool()` function in
  `mcp_server/server.py` and a method in `ServiceNowClient`. The backend discovers it automatically; add a
  sentence to `SYSTEM_PROMPT` describing when to use it.
* **Other tables** (change, problem, request): generalise `INCIDENT_TABLE` or add new clients/tools.
* **Different Gemini model:** change `GEMINI_MODEL` in `.env`.

---

## 15. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GOOGLE_CLOUD_PROJECT is not set` on backend start | Fill `.env`; restart |
| `DefaultCredentialsError` | Run `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS` |
| `403 ... Vertex AI API has not been used` | `gcloud services enable aiplatform.googleapis.com` |
| `404 model not found` | Model/region mismatch – change `GEMINI_MODEL` or `GOOGLE_CLOUD_LOCATION` |
| `ServiceNow is not configured` | Set the three `SERVICENOW_*` variables |
| `HTTP 401` | Wrong user/password or user lacks REST access |
| Non-JSON response error | Wake the PDI; check the instance URL (no trailing path) |
| UI says backend not reachable | Start uvicorn; check `BACKEND_URL` |
| `ModuleNotFoundError: backend` | Run uvicorn from the project root (`servicenow-mcp-chat/`) |
| Windows: MCP sub-process fails to start | Run in a normal terminal (not an IDE terminal with restricted PATH); make sure the venv's python is used |

---

## 16. Quick Reference — Commands

```bash
pip install -r requirements.txt
uvicorn backend.main:app --port 8000          # backend (+ MCP server automatically)
streamlit run ui/app.py                        # UI
python scripts/mock_servicenow.py              # optional fake ServiceNow
python -m pytest -q tests                      # tests
```

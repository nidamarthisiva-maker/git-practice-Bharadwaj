# ServiceNow Incident Assistant — Code Walkthrough

A study guide that explains **every file, section by section**, in the order you should read them.
Read top to bottom: each part builds on the one before it.

---

## Part 0 — The Big Picture (read this first)

### 0.1 What each piece does

| Piece | File(s) | One-line job |
|-------|---------|--------------|
| **ServiceNow client** | `mcp_server/servicenow_client.py` | Talks to ServiceNow's REST API |
| **MCP server** | `mcp_server/server.py` | Exposes 3 "tools" the AI can call |
| **Config + LLM** | `backend/config.py`, `backend/llm.py` | Settings and the Gemini model |
| **API models** | `backend/schemas.py` | Shape of requests/responses |
| **Agent** | `backend/agent.py` | The "brain": loop of think → call tool → answer |
| **Backend API** | `backend/main.py` | FastAPI web server, starts MCP, exposes `/chat` |
| **UI** | `ui/app.py` | Streamlit chat screen |
| **Mock + tests** | `scripts/mock_servicenow.py`, `tests/` | Fake ServiceNow and automatic checks |

### 0.2 Key terms

| Term | Meaning |
|------|---------|
| **LLM** | The AI model (Gemini). It reads text and decides what to do. |
| **Tool** | A Python function the LLM is allowed to ask us to run (e.g. `get_incident`). |
| **Tool calling / function calling** | The LLM replies "please run `get_incident` with number=INC001" instead of plain text. Our code runs it and gives the result back. |
| **MCP (Model Context Protocol)** | A standard way to package tools in a separate program (the *MCP server*) so any AI app (the *MCP client*) can use them. |
| **stdio transport** | The MCP client starts the MCP server as a child process and they talk through stdin/stdout pipes. |
| **LangChain** | Library with common building blocks: message types, tool wrapper, chat-model interface. |
| **FastAPI** | Python web framework for the backend REST API. |
| **Streamlit** | Python library that turns a script into a web UI. |
| **Session** | One conversation, identified by a `session_id`, so the AI remembers earlier messages. |

### 0.3 The journey of one message

Example: the user types **"Summarise INC0010001"**.

```
 1. ui/app.py            user types text -> POST /chat
 2. backend/main.py      /chat receives it -> calls agent.achat()
 3. backend/agent.py     builds [system prompt + history + new message]
 4. backend/llm.py       Gemini receives it (with the 3 tools described)
 5. Gemini               answers: "call get_incident(number='INC0010001')"
 6. backend/agent.py     runs that tool through the MCP client
 7. mcp_server/server.py get_incident() runs
 8. servicenow_client.py REST call to ServiceNow, JSON comes back
 9. backend/agent.py     gives the JSON to Gemini as a ToolMessage
10. Gemini               writes a friendly summary
11. backend/main.py      returns {reply, tool_calls}
12. ui/app.py            shows the reply + "Tools used" panel
```

**Why is the intent detected correctly?** Nobody wrote `if "create" in text`. Gemini
reads the tool descriptions and the system prompt and *chooses* the tool itself. That is the
"LLM reasoning" part of your requirement.

### 0.4 Folder layout (and the reading order)

```
servicenow-mcp-chat/
├── .env                        <- Part 1  (settings)
├── requirements.txt            <- Part 1  (libraries)
├── mcp_server/
│   ├── servicenow_client.py    <- Part 2  (REST client)
│   └── server.py               <- Part 3  (MCP server)
├── backend/
│   ├── config.py               <- Part 4
│   ├── llm.py                  <- Part 5
│   ├── schemas.py              <- Part 6
│   ├── agent.py                <- Part 7  (most important)
│   └── main.py                 <- Part 8
├── ui/app.py                   <- Part 9
├── scripts/mock_servicenow.py  <- Part 10
└── tests/                      <- Part 11
```

---

## Part 1 — `.env` and `requirements.txt`

### `.env` (you create it from `.env.example`)

Plain `KEY=value` lines. Python reads them with `python-dotenv` and they become environment
variables (`os.getenv("KEY")`).

| Variable | Used by | Meaning |
|----------|---------|---------|
| `SERVICENOW_INSTANCE_URL` | MCP server | e.g. `https://dev382441.service-now.com` |
| `SERVICENOW_USERNAME` / `SERVICENOW_PASSWORD` | MCP server | Basic-auth login for REST |
| `SERVICENOW_TIMEOUT` | MCP server | Seconds before a REST call gives up |
| `GOOGLE_CLOUD_PROJECT` | backend | Your GCP project id |
| `GOOGLE_CLOUD_LOCATION` | backend | Region, e.g. `us-central1` |
| `GEMINI_MODEL` | backend | e.g. `gemini-2.5-flash` |
| `BACKEND_URL` | UI | Where the FastAPI server runs |
| `LOG_LEVEL` | all | `INFO` or `DEBUG` |
| `MAX_AGENT_STEPS`, `MAX_HISTORY_MESSAGES`, `LLM_TEMPERATURE` | backend | Agent tuning |

> **Tip:** `load_dotenv(path, override=True)` makes `.env` win over variables already set in
> your terminal/Windows. Without `override=True`, an old exported variable silently beats `.env`.
> This was the cause of the 401 confusion earlier.

### `requirements.txt`

| Library | Why |
|---------|-----|
| `mcp` | Official MCP SDK (`FastMCP` server) |
| `langchain-mcp-adapters` | Lets LangChain use MCP tools |
| `langchain-core` | Messages, tools, chat model interface (**no LangGraph**) |
| `langchain-google-vertexai` | `ChatVertexAI` = Gemini on Vertex AI |
| `fastapi`, `uvicorn` | Web API and the server that runs it |
| `pydantic` | Data validation |
| `requests` | HTTP calls to ServiceNow |
| `python-dotenv` | Reads `.env` |
| `streamlit` | Chat UI |
| `pytest`, `httpx` | Tests |

---

## Part 2 — `mcp_server/servicenow_client.py`

**Purpose:** a small class that knows how to call ServiceNow. It knows nothing about AI or MCP —
it is plain Python, so it is easy to test and reuse.

### 2.1 Imports and constants

```python
logger = logging.getLogger("servicenow")

INCIDENT_TABLE = "/api/now/table/incident"
USER_TABLE = "/api/now/table/sys_user"
```
ServiceNow exposes every table at `/api/now/table/<table_name>`. We use `incident` and
`sys_user` (to look up callers).

```python
INCIDENT_FIELDS = ["number", "short_description", "description", "state", ...]
```
The list of columns we ask for (`sysparm_fields`). Asking only for what we need keeps responses
small, which means fewer tokens sent to the LLM.

```python
STATE_CODES = {"new": "1", "in_progress": "2", "on_hold": "3", "resolved": "6", "closed": "7", "canceled": "8"}
PRIORITY_CODES = {"critical": "1", "high": "2", "moderate": "3", "low": "4", "planning": "5"}
```
ServiceNow stores states/priorities as numbers. These dictionaries translate friendly words
(which the LLM and users use) into the numbers used in queries.

### 2.2 `ServiceNowError` and helper functions

```python
class ServiceNowError(Exception):
    """Raised for any configuration, network or API level problem."""
```
One custom exception type for *every* failure. Callers only need `except ServiceNowError`.

```python
def clean_query_value(value: str) -> str:
    return (value or "").replace("^", " ").replace("\n", " ").strip()
```
**Security.** In ServiceNow's "encoded query" language, `^` means AND. If a user typed
`vpn^active=false`, they could inject extra conditions. We replace `^` with a space.

```python
def _flatten(record):
    for key, value in record.items():
        if isinstance(value, dict):
            value = value.get("display_value") or value.get("value") or ""
```
Reference fields (like `assigned_to`) sometimes come back as `{"display_value": "Beth", "link": ...}`.
This turns them into plain strings so the LLM sees simple text.

### 2.3 `ServiceNowClient.__init__` — building the HTTP session

```python
if not (instance_url and username and password):
    raise ServiceNowError("ServiceNow is not configured. ...")
```
Fail early with a helpful message if `.env` is incomplete.

```python
self.session = requests.Session()
self.session.auth = (username, password)
self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
```
A `Session` reuses the TCP connection (faster) and attaches Basic-auth and JSON headers to every call.

```python
retry = Retry(total=3, backoff_factor=0.5,
              status_forcelist=(429, 502, 503, 504),
              allowed_methods=frozenset(["GET"]))
```
Automatic retries when ServiceNow is briefly overloaded. **Only GET is retried.** If a POST
(create) were retried after a timeout we could create duplicate incidents.

### 2.4 `_request()` — the one place that makes HTTP calls

Step by step:
1. Build the URL: `base_url + path`.
2. `self.session.request(...)` inside `try`. Network problems become `ServiceNowError("Network error ...")`.
3. If status >= 400 (like your 401), read ServiceNow's error message from the JSON
   (`{"error": {"message": "User is not authenticated"}}`) and raise
   `ServiceNowError("ServiceNow returned HTTP 401: ...")`. **This is the exact text you saw in the UI.**
4. Parse JSON. If the body is not JSON (a hibernating developer instance returns an HTML page)
   raise a friendly "wake up your instance" error.

Every other method calls `_request`, so error handling is written once.

### 2.5 The four public methods

| Method | REST call | Notes |
|--------|-----------|-------|
| `get_incident(number)` | `GET incident?sysparm_query=number=INC...&sysparm_limit=1` | Returns one dict or `None` |
| `list_incidents(encoded_query, limit)` | `GET incident?sysparm_query=...&sysparm_limit=N` | Returns list of dicts |
| `resolve_user_sys_id(identifier)` | `GET sys_user?sysparm_query=user_name=X^ORemail=X^ORname=X` | Turns a name/email into a `sys_id` |
| `create_incident(fields)` | `POST incident` with JSON body | Returns the created record |

Common query parameters:
* `sysparm_display_value=true` → "In Progress" instead of `2`.
* `sysparm_exclude_reference_link=true` → no extra `link` objects.
* `sysparm_fields=...` → only the listed columns.

**Why `resolve_user_sys_id`?** `caller_id` in ServiceNow is a *reference* to the user table, so it
needs the user's `sys_id`, not a name. We look the user up first; if they don't exist we return an
error instead of creating a bad incident.

---

## Part 3 — `mcp_server/server.py`

**Purpose:** wraps the REST client into three **MCP tools**. The tool name, description and
parameter descriptions are what Gemini reads to decide what to call.

### 3.1 Top of file — path setup and imports

```python
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```
Adds the project folder to Python's import path so `from mcp_server.servicenow_client import ...`
works even when the backend starts this file as `python mcp_server/server.py`.

```python
load_dotenv(ROOT / ".env")
logging.basicConfig(level=..., stream=sys.stderr, ...)
```
* Loads your credentials.
* **Logs to `stderr`, never `stdout`.** With stdio transport, stdout is the private pipe for MCP
  messages. A stray `print()` would corrupt the protocol.

```python
mcp = FastMCP("servicenow-incidents")
```
Creates the MCP server object. `@mcp.tool()` registers functions on it.

### 3.2 `_get_client()` — lazy creation

```python
_client = None
def _get_client():
    global _client
    if _client is None:
        _client = ServiceNowClient(instance_url=os.getenv(...), ...)
    return _client
```
The client is created on the **first tool call**, not at startup. Result: the server always starts,
and if credentials are missing the user gets a readable error instead of a crash. It is created once and reused.

### 3.3 `_json()` and `_error()`

```python
def _error(message):
    return _json({"success": False, "error": message})
```
Tools **return** errors as JSON text instead of raising exceptions. That way Gemini receives the
error as normal information and can explain it ("I couldn't connect because...") instead of the whole request failing.

### 3.4 Tool 1 — `get_incident`

```python
@mcp.tool()
async def get_incident(number: Annotated[str, Field(description="ServiceNow incident number, for example INC0010001")]) -> str:
    """Fetch the full details of a single ServiceNow incident by its number. ..."""
```
* `@mcp.tool()` registers it. The **docstring** becomes the tool description; `Field(description=...)`
  becomes the parameter description. These are the instructions Gemini uses. **Better text → better tool choice.**
* `async def` so the server isn't blocked while waiting for ServiceNow.

Body:
1. `number = number.strip().upper()` then check with `INCIDENT_NUMBER_RE` (`^INC\d{4,}$`). Bad input returns an error immediately without calling ServiceNow.
2. `await asyncio.to_thread(_get_client().get_incident, number)` — the `requests` library is
   blocking, so it runs in a worker thread to keep the async server responsive.
3. `except ServiceNowError` → return `_error(...)`.
4. `None` → `{"found": false}`; otherwise `{"found": true, "incident": {...}}`.

### 3.5 Tool 2 — `list_incidents`

Parameters (all optional, simple types): `state="open"`, `priority=""`, `keyword=""`,
`assignment_group=""`, `limit=10`.

It **builds an encoded query** from the parameters:

| Input | Added condition |
|-------|-----------------|
| `state="open"` | `active=true` |
| `state="in_progress"` | `state=2` |
| `state="all"` | nothing |
| `priority="high"` | `priority=2` |
| `assignment_group="Network"` | `assignment_group.name=Network` |
| `keyword="vpn"` | `short_descriptionLIKEvpn^ORdescriptionLIKEvpn` |
| always | `ORDERBYDESCsys_created_on` (newest first) |

Conditions are joined with `^`. Example result:
`active=true^priority=2^short_descriptionLIKEvpn^ORdescriptionLIKEvpn^ORDERBYDESCsys_created_on`.

Other details:
* Unknown `state`/`priority` → error that lists valid values, so the LLM can correct itself.
* `limit` is clamped to 1–25 so one question can't pull thousands of rows.
* **Why not let the LLM write the raw query?** Safer and more reliable. The LLM picks simple
  parameters; our code builds the query.

### 3.6 Tool 3 — `create_incident`

Parameters: `short_description` (required), and optional `description`, `urgency=3`, `impact=3`, `category`, `caller`.

Flow:
1. Require a non-empty `short_description`.
2. Convert urgency/impact to `int`, check they are 1, 2 or 3.
3. Build the `fields` dict (`short_description` truncated to 160 chars, ServiceNow's limit).
4. If `caller` is given → `resolve_user_sys_id`; not found → return an error asking for a valid caller.
5. `client.create_incident(fields)` → return `{"success": true, "created": true, "incident": {...}}`.

The docstring says *"Never call it twice for the same request"* which discourages duplicate creation.
ServiceNow itself calculates `priority` from urgency × impact.

### 3.7 Bottom

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```
Starts the server, listening on stdin/stdout. You never run it by hand; the backend starts it for you.

---

## Part 4 — `backend/config.py`

```python
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
MCP_SERVER_PATH = ROOT / "mcp_server" / "server.py"
```
* `ROOT` = project folder. `.env` is always found regardless of where you launch from.
* `MCP_SERVER_PATH` = the file the backend launches as the MCP server.

```python
@dataclass(frozen=True)
class Settings:
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    max_agent_steps: int = int(os.getenv("MAX_AGENT_STEPS", "6"))
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
```
One immutable settings object with defaults. `get_settings()` returns it.
* `temperature=0` → the model is as deterministic as possible, good for tool calling.
* `max_agent_steps=6` → safety limit on how many tool rounds one question can take.
* `max_history_messages=20` → how many past messages are remembered.

---

## Part 5 — `backend/llm.py`

```python
def build_llm(settings):
    if not settings.gcp_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set. ...")
    from langchain_google_vertexai import ChatVertexAI
    return ChatVertexAI(model_name=..., project=..., location=...,
                        temperature=..., max_output_tokens=2048, max_retries=3)
```
* A **factory function**: it builds and returns the Gemini chat model.
* The import is inside the function so the module can be imported (e.g. in tests) without Google libraries being touched.
* Authentication is automatic through **Application Default Credentials** (`gcloud auth application-default login` or `GOOGLE_APPLICATION_CREDENTIALS`).
* `max_retries=3` retries on temporary Vertex errors such as rate limits.
* The `LangChainDeprecationWarning` you saw is only a notice that a newer class exists. It doesn't affect behaviour.

The reason it is a separate function: `main.py` accepts *any* factory, so tests can plug in a fake model.

---

## Part 6 — `backend/schemas.py`

Pydantic models define what the API accepts and returns, and FastAPI validates automatically.

```python
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=4000)
```
Input. Empty or huge messages are rejected with HTTP 422 before reaching the LLM.

```python
class ToolCallInfo(BaseModel):
    name: str
    args: dict[str, Any]
    result: str

class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ToolCallInfo] = []
```
Output. `tool_calls` feeds the "🔧 Tools used" panel in the UI.

---

## Part 7 — `backend/agent.py` (the brain — read slowly)

### 7.1 `SYSTEM_PROMPT`

Instructions given to Gemini on **every** request. It contains:
1. The list of tools and what they do.
2. **Intent rules:** GET/SUMMARISE → `get_incident`/`list_incidents`; CREATE → `create_incident`; otherwise chat.
3. **Rules:** never invent data; require a short description before creating; use defaults (urgency 3, impact 3) and say so; call create at most once; keep summaries concise.

To change the assistant's behaviour, edit this text. It is the cheapest and most powerful lever in the project.

### 7.2 `AgentResult` and `content_to_text`

```python
@dataclass
class AgentResult:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)
```
What `achat()` returns: the final text plus a record of every tool that ran.

```python
def content_to_text(content, sep=""):
```
A LangChain message's `content` can be a string **or** a list of blocks (text, thinking, ...).
This normalises it to plain text and ignores non-text blocks.

### 7.3 `IncidentAgent.__init__`

```python
self.tools = {t.name: t for t in tools}
self.llm = llm.bind_tools(tools)
self.max_steps = max_steps
self.max_history_messages = max_history_messages
self._histories = {}
self._locks = {}
```
* `self.tools` — look up a tool by name when Gemini asks for it.
* `llm.bind_tools(tools)` — tells Gemini about the three tools (their names, descriptions and parameter schemas). **This one line is what enables tool calling.**
* `_histories` — `{session_id: [messages]}` conversation memory, in RAM (lost on restart).
* `_locks` — one `asyncio.Lock` per session.

### 7.4 `reset(session_id)`

Deletes a session's history and lock. Called by `DELETE /sessions/{id}` (the UI's "New conversation").

### 7.5 `_run_tool(call)` — executing one tool request

`call` looks like `{"name": "get_incident", "args": {"number": "INC0010001"}, "id": "abc"}`.

1. Get the id (generate one if Gemini didn't).
2. Unknown tool name → return an error `ToolMessage` instead of crashing.
3. `await tool.ainvoke({...type: "tool_call"})` → goes through the MCP client → MCP server → ServiceNow.
4. Convert the result to plain text (`content_to_text(raw, sep="\n")`).
5. Any exception is caught and returned as `{"success": false, "error": "Tool execution failed: ..."}`.
6. Return `ToolMessage(content=text, tool_call_id=call_id, name=name)`.

A `ToolMessage` is LangChain's "here is the answer to your tool request" message. Its
`tool_call_id` must match the request id, or Gemini cannot pair them.

### 7.6 `achat()` — the main loop

```python
lock = self._locks.setdefault(session_id, asyncio.Lock())
async with lock:
```
Only one request at a time per conversation, so two fast clicks can't scramble the history.

**Prepare the context**
```python
history = self._histories.setdefault(session_id, [])
history_window = history[-self.max_history_messages:]
while history_window and not isinstance(history_window[0], HumanMessage):
    history_window = history_window[1:]
```
Take the last N messages; make sure the window starts with a user message (Gemini requires that).

```python
human = HumanMessage(content=user_message)
messages = [SystemMessage(content=SYSTEM_PROMPT), *history_window, human]
```
The final list sent to Gemini: **system prompt → past chat → new question**.

**The loop**
```python
for step in range(self.max_steps):
    ai = await self.llm.ainvoke(messages)       # ask Gemini
    messages.append(ai)
    if not ai.tool_calls:                        # Gemini gave a final text answer
        final_text = content_to_text(ai.content).strip()
        break
    for call in ai.tool_calls:                   # Gemini wants tools run
        tool_msg = await self._run_tool(call)
        messages.append(tool_msg)
        executed.append({...})
else:
    final_text = "I could not finish this request within the allowed number of steps..."
```
Think of it as a conversation with Gemini:

| Round | Gemini says | We do |
|-------|-------------|-------|
| 1 | "Run `get_incident('INC0010001')`" | Run it, append the result |
| 2 | "INC0010001 is In Progress, assigned to Beth..." | No tool calls → stop |

* `break` leaves the loop when there are no tool calls.
* The `for ... else` runs the `else` **only if the loop never hit `break`**, i.e. the model kept calling tools for `max_steps` rounds. That is the infinite-loop safety net.
* Several tool calls in one round (e.g. two incidents) are all executed before the next round.

**Wrap-up**
```python
if not final_text:
    final_text = "I did not get a response from the model. Please try again."

history.extend([human, AIMessage(content=final_text)])
if len(history) > self.max_history_messages * 2:
    del history[: len(history) - self.max_history_messages * 2]
```
* Guard against an empty model reply.
* **Memory stores only the user message and the final text answer**, not the tool messages. Because history is always whole user/assistant pairs, trimming can never cut between a "tool request" and its "tool result". A broken pair makes Gemini reject the request. The trade-off: raw tool JSON isn't remembered in later turns, but the assistant's own summary is, which is enough for follow-ups like "assign it to..." or "what's its priority again?"
* Old messages are trimmed so memory doesn't grow forever.

Finally it returns `AgentResult(reply=final_text, tool_calls=executed)`.

---

## Part 8 — `backend/main.py` (the web server)

### 8.1 Imports and logging

Brings in FastAPI, the MCP client classes (`MultiServerMCPClient`, `load_mcp_tools`), the agent, settings, LLM factory and schemas. `logging.basicConfig` sets the log format (`[backend] INFO ...`).

### 8.2 `create_app(llm_factory=build_llm)`

Everything lives inside a function that **builds and returns** the app. The `llm_factory` parameter lets
tests inject a fake model (`create_app(lambda s: ScriptedLLM())`) while production uses the real Gemini.

### 8.3 The `lifespan` function — startup and shutdown

```python
@asynccontextmanager
async def lifespan(app):
    mcp_client = MultiServerMCPClient({
        "servicenow": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(MCP_SERVER_PATH)],
            "env": dict(os.environ),
        }
    })
    async with mcp_client.session("servicenow") as session:
        tools = await load_mcp_tools(session)
        app.state.agent = IncidentAgent(llm=llm_factory(settings), tools=tools, ...)
        app.state.tool_names = [t.name for t in tools]
        yield
```
Line by line:
* `command: sys.executable` — the **same Python** (your venv) runs the MCP server.
* `args` — the script to run.
* `env: dict(os.environ)` — passes environment variables to the child process (by default MCP passes almost none).
* `mcp_client.session("servicenow")` — starts the MCP server process and opens **one** connection that stays open for the app's lifetime.
* `load_mcp_tools(session)` — asks the MCP server "which tools do you have?" and wraps each as a LangChain tool. This is the log line `Loaded MCP tools: ['get_incident', ...]`.
* The agent is created and stored in `app.state`, so request handlers can reach it.
* `yield` — **everything before = startup, everything after = shutdown.** The server runs while paused here.
* Leaving the `async with` closes the session and stops the MCP process (`MCP session closed` in the log).

### 8.4 The endpoints

| Endpoint | What it does |
|----------|--------------|
| `GET /health` | Returns status, model name and tool names. The UI sidebar uses it for the green "Backend online" box. |
| `POST /chat` | Gets or creates a `session_id` (`uuid4`), calls `agent.achat()`, returns a `ChatResponse`. Any exception becomes HTTP 502 with details. |
| `DELETE /sessions/{id}` | Clears that conversation's memory. |

`CORSMiddleware` allows browser calls from any origin. Fine for local use; restrict it in production.

### 8.5 Last line

```python
app = create_app()
```
Creates the app at import time. This is why `uvicorn backend.main:app` works: *module `backend.main`, variable `app`*.

> **About your port 8000 error:** `[Errno 10048]` happened because an earlier uvicorn was still
> listening on port 8000. The new process loaded fine (tools loaded) but couldn't bind the port, so it
> shut down. Your UI kept talking to the *old* process with the *old* credentials.

---

## Part 9 — `ui/app.py` (Streamlit)

**Key concept:** Streamlit re-runs the **entire script top to bottom on every interaction**. Anything
that must survive between runs lives in `st.session_state`.

### 9.1 Setup

```python
load_dotenv(...)
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
st.set_page_config(page_title=..., page_icon="🎫", layout="centered")
```
Reads the backend address and sets the page title/icon. `set_page_config` must be the first Streamlit call.

### 9.2 Constants

`WELCOME` is the first assistant message. `EXAMPLES` are the sidebar quick-prompt buttons.

### 9.3 Helper functions

| Function | Purpose |
|----------|---------|
| `init_state()` | First run only: creates `session_id` (uuid), the `messages` list (starting with the welcome message) and `pending` (a clicked example). |
| `backend_health()` | `GET /health` with a 3-second timeout; returns JSON or `None`. |
| `call_backend(message)` | `POST /chat` with `{session_id, message}`, 120-second timeout (LLM + tools can be slow). HTTP errors become `RuntimeError("Backend error (...)")`. |
| `render_tools(tools)` | Draws the "🔧 Tools used" expander showing each tool's name, arguments and pretty-printed JSON result (cut at 3000 characters). |

### 9.4 Sidebar (`with st.sidebar:`)

1. `backend_health()` → green "Backend online + model + tools" or red "Backend not reachable".
2. **New conversation** button: calls `DELETE /sessions/{id}`, makes a new `session_id`, resets the messages, `st.rerun()`.
3. **Example buttons**: clicking one stores its text in `st.session_state.pending`.

### 9.5 Main chat area

```python
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        render_tools(msg.get("tools", []))
```
Redraws the whole conversation each run, so the history stays visible.

```python
typed = st.chat_input("Ask about an incident or describe a problem to log...")
prompt = typed or st.session_state.pending
st.session_state.pending = None
```
Use what the user typed, or an example they clicked. Then clear `pending` so it isn't sent twice.

```python
if prompt:
    messages.append(user message); show it
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                data = call_backend(prompt)
                reply, tools = data["reply"], data.get("tool_calls", [])
            except (requests.RequestException, RuntimeError) as exc:
                reply, tools = f"⚠️ {exc}", []
        st.markdown(reply); render_tools(tools)
    messages.append(assistant message)
```
Send the question, show a spinner, display the answer, and store both messages. Errors appear as a
chat bubble with ⚠️ instead of a Python stack trace.

---

## Part 10 — `scripts/mock_servicenow.py` (optional)

A tiny fake ServiceNow built with FastAPI so you can test **without** a ServiceNow instance.
It runs on port 8081, keeps four sample incidents in a Python list and implements:
* `GET /api/now/table/incident` — filters by `=`, `LIKE`, `^OR`, `active`, `assignment_group.name`, sorts newest first.
* `POST /api/now/table/incident` — adds a new incident with a priority computed from urgency/impact.
* `GET /api/now/table/sys_user` — recognises one known user.

Helpers: `_seed()` builds an incident dict, `_view()` converts codes to display text ("2" → "In Progress"), `_matches()`/`_term()` evaluate the query. You don't need this now that your real instance works, but it is what makes the automatic tests possible.

---

## Part 11 — `tests/`

Run with `python -m pytest -q tests`. No Google Cloud and no real ServiceNow are needed.

### `tests/test_end_to_end.py`
* **`ScriptedLLM`** — a fake chat model that "decides" using keywords ("create" → `create_incident`, "inc0010001" → `get_incident`, "open" → `list_incidents`). It replaces Gemini only.
* **`client` fixture** — starts the mock ServiceNow on a free port, sets credentials in `os.environ`, builds the real app with the fake LLM and wraps it in `TestClient`. The real MCP server subprocess is started for real.
* **Tests:** health lists the 3 tools; small talk uses no tool; get incident; list with keyword; create incident; invalid number returns an error payload.

### `tests/test_ui.py`
Uses Streamlit's headless `AppTest`: (1) backend down → a ⚠️ message appears; (2) mocked successful reply → the answer is displayed.

Together they prove the whole chain works except Gemini's own reasoning.

---

## Part 12 — Putting it together: debugging cheat-sheet

| Symptom | Where to look |
|---------|---------------|
| Red "Backend not reachable" in UI | Backend not running, or `BACKEND_URL` is wrong (Part 9) |
| `Errno 10048` on startup | Port already in use (Part 8.5) |
| `HTTP 401` inside the "Tools used" panel | ServiceNow credentials in `.env` (Part 2.4, Part 1) |
| `found: false` | Incident number doesn't exist in your instance |
| `Tool execution failed` | MCP server crashed; check the uvicorn terminal for `[mcp-server]` lines |
| Model picks the wrong tool | Improve tool descriptions (Part 3) or the `SYSTEM_PROMPT` (Part 7.1) |
| Assistant forgets context | History limit (`MAX_HISTORY_MESSAGES`) or backend restarted (RAM memory) |
| Gemini auth/quota error → HTTP 502 | GCP project, region, or credentials (Part 5) |

### Logging tip
Set `LOG_LEVEL=DEBUG` in `.env` and restart. The uvicorn terminal shows both `[backend]` lines
(agent and tool selection) and `[mcp-server]` lines (every ServiceNow call with its parameters).

---

## Part 13 — Suggested study order

1. **Read the journey** in 0.3 until you can say it from memory.
2. `servicenow_client.py` — plain Python, easiest.
3. `server.py` — see how functions become tools.
4. `agent.py` — the loop (draw it on paper).
5. `main.py` — how the pieces connect at startup.
6. `ui/app.py` — how the screen talks to the backend.
7. Run the tests and read `test_end_to_end.py` to see the whole chain exercised.

**Exercise ideas:**
* Add a tool `add_work_note(number, note)` — touch `servicenow_client.py` (one `PATCH` method) and `server.py` (one `@mcp.tool()`), then add one line to the system prompt.
* Change the system prompt so the assistant always replies in a table.
* Set `LOG_LEVEL=DEBUG` and watch one full request in the terminals.

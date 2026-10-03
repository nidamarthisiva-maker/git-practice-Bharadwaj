"""MCP server exposing ServiceNow incident tools over stdio.

Tools
-----
* get_incident     - fetch ONE incident by number (INC0010001)
* list_incidents   - search / list incidents with simple filters
* create_incident  - create a new incident

Run standalone (for debugging with the MCP inspector):
    python mcp_server/server.py

NOTE: with the stdio transport stdout is reserved for the MCP protocol, so all
logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allows `python mcp_server/server.py`
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

from mcp_server.servicenow_client import (  # noqa: E402
    PRIORITY_CODES,
    STATE_CODES,
    ServiceNowClient,
    ServiceNowError,
    clean_query_value,
)

## load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s [mcp-server] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("servicenow-mcp")

mcp = FastMCP("servicenow-incidents")

_client: ServiceNowClient | None = None
INCIDENT_NUMBER_RE = re.compile(r"^INC\d{4,}$")


def _get_client() -> ServiceNowClient:
    """Lazy init so the server starts even if credentials are missing."""
    global _client
    if _client is None:
        _client = ServiceNowClient(
            instance_url=os.getenv("SERVICENOW_INSTANCE_URL", ""),
            username=os.getenv("SERVICENOW_USERNAME", ""),
            password=os.getenv("SERVICENOW_PASSWORD", ""),
            timeout=int(os.getenv("SERVICENOW_TIMEOUT", "30")),
        )
    return _client


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(message: str) -> str:
    logger.warning("tool error: %s", message)
    return _json({"success": False, "error": message})


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_incident(
    number: Annotated[
        str, Field(description="ServiceNow incident number, for example INC0010001")
    ],
) -> str:
    """Fetch the full details of a single ServiceNow incident by its number.

    Use this when the user asks about, wants a summary of, or wants the status of
    one specific incident.
    """
    number = (number or "").strip().upper()
    if not INCIDENT_NUMBER_RE.match(number):
        return _error(f"'{number}' is not a valid incident number (expected e.g. INC0010001).")
    try:
        incident = await asyncio.to_thread(_get_client().get_incident, number)
    except ServiceNowError as exc:
        return _error(str(exc))
    if incident is None:
        return _json({"success": True, "found": False, "message": f"No incident {number} exists."})
    return _json({"success": True, "found": True, "incident": incident})


@mcp.tool()
async def list_incidents(
    state: Annotated[
        str,
        Field(
            description=(
                "Filter by state: open (all active, default), new, in_progress, on_hold, "
                "resolved, closed, canceled or all"
            )
        ),
    ] = "open",
    priority: Annotated[
        str,
        Field(
            description=(
                "Optional priority filter: 1-5 or critical, high, moderate, low, planning. "
                "Empty string means any priority."
            )
        ),
    ] = "",
    keyword: Annotated[
        str,
        Field(description="Optional text searched in short description and description"),
    ] = "",
    assignment_group: Annotated[
        str, Field(description="Optional assignment group name, e.g. Network")
    ] = "",
    limit: Annotated[int, Field(description="Maximum rows to return, 1-25 (default 10)")] = 10,
) -> str:
    """List or search ServiceNow incidents, newest first.

    Use this when the user asks for several incidents, e.g. "show my open P1
    incidents", "incidents about VPN" or "summarise recent incidents".
    """
    conditions: list[str] = []

    state_key = (state or "open").strip().lower().replace(" ", "_")
    if state_key == "open":
        conditions.append("active=true")
    elif state_key == "all":
        pass
    elif state_key in STATE_CODES:
        conditions.append(f"state={STATE_CODES[state_key]}")
    else:
        return _error(
            f"Unknown state '{state}'. Use open, new, in_progress, on_hold, resolved, "
            "closed, canceled or all."
        )

    prio = (priority or "").strip().lower()
    if prio:
        prio = PRIORITY_CODES.get(prio, prio)
        if prio not in {"1", "2", "3", "4", "5"}:
            return _error(f"Unknown priority '{priority}'. Use 1-5 or critical/high/moderate/low/planning.")
        conditions.append(f"priority={prio}")

    group = clean_query_value(assignment_group)
    if group:
        conditions.append(f"assignment_group.name={group}")

    kw = clean_query_value(keyword)
    if kw:
        # ^OR joins with the previous condition -> (short LIKE kw OR description LIKE kw)
        conditions.append(f"short_descriptionLIKE{kw}^ORdescriptionLIKE{kw}")

    conditions.append("ORDERBYDESCsys_created_on")
    query = "^".join(conditions)

    try:
        limit = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit = 10

    try:
        incidents = await asyncio.to_thread(_get_client().list_incidents, query, limit)
    except ServiceNowError as exc:
        return _error(str(exc))
    return _json({"success": True, "count": len(incidents), "incidents": incidents})


@mcp.tool()
async def create_incident(
    short_description: Annotated[
        str, Field(description="One line title of the problem (required)")
    ],
    description: Annotated[
        str, Field(description="Detailed description of the problem. Empty if not provided.")
    ] = "",
    urgency: Annotated[
        int, Field(description="Urgency: 1=High, 2=Medium, 3=Low (default 3)")
    ] = 3,
    impact: Annotated[
        int, Field(description="Impact: 1=High, 2=Medium, 3=Low (default 3)")
    ] = 3,
    category: Annotated[
        str,
        Field(
            description="Optional category such as inquiry, software, hardware, network, database. Empty if unknown."
        ),
    ] = "",
    caller: Annotated[
        str,
        Field(description="Optional caller user name, e-mail or full name. Empty if unknown."),
    ] = "",
) -> str:
    """Create a NEW ServiceNow incident and return its number.

    Only call this when the user clearly wants to log/raise/create an incident and
    a short description is available. Never call it twice for the same request.
    """
    short_description = (short_description or "").strip()
    if not short_description:
        return _error("short_description is required to create an incident.")

    try:
        urgency, impact = int(urgency), int(impact)
    except (TypeError, ValueError):
        return _error("urgency and impact must be numbers between 1 and 3.")
    if urgency not in (1, 2, 3) or impact not in (1, 2, 3):
        return _error("urgency and impact must be 1 (High), 2 (Medium) or 3 (Low).")

    fields: dict[str, Any] = {
        "short_description": short_description[:160],
        "urgency": str(urgency),
        "impact": str(impact),
    }
    if description.strip():
        fields["description"] = description.strip()
    if category.strip():
        fields["category"] = category.strip().lower()

    try:
        client = _get_client()
        if caller.strip():
            sys_id = await asyncio.to_thread(client.resolve_user_sys_id, caller)
            if not sys_id:
                return _error(
                    f"Caller '{caller}' was not found in ServiceNow. Ask the user for a valid "
                    "user name or e-mail, or create the incident without a caller."
                )
            fields["caller_id"] = sys_id
        incident = await asyncio.to_thread(client.create_incident, fields)
    except ServiceNowError as exc:
        return _error(str(exc))

    return _json({"success": True, "created": True, "incident": incident})


if __name__ == "__main__":
    logger.info("Starting ServiceNow MCP server (stdio)")
    mcp.run(transport="stdio")

"""Tiny in-memory fake of the ServiceNow Table API (incident + sys_user).

Lets you run and demo the whole stack WITHOUT a ServiceNow instance:

    python scripts/mock_servicenow.py          # listens on :8081
    # .env -> SERVICENOW_INSTANCE_URL=http://localhost:8081
    #         SERVICENOW_USERNAME=admin  SERVICENOW_PASSWORD=admin

Supports only the small subset of encoded-query syntax used by this project.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="Mock ServiceNow")

STATES = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
LEVELS = {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"}
PRIORITIES = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seed(n, short, desc, state, urgency, impact, prio, group, assignee, category):
    return {
        "number": f"INC00{10000 + n}",
        "sys_id": f"sysid{n:04d}",
        "short_description": short,
        "description": desc,
        "state": state,
        "urgency": urgency,
        "impact": impact,
        "priority": prio,
        "category": category,
        "subcategory": "",
        "caller_id": "Abel Tuter",
        "assigned_to": assignee,
        "assignment_group": group,
        "opened_at": "2026-09-28 09:15:00",
        "sys_updated_on": "2026-09-29 11:00:00",
        "resolved_at": "",
        "close_code": "",
        "close_notes": "",
    }


INCIDENTS = [
    _seed(1, "VPN connection drops every 10 minutes", "Users in Hyderabad office lose VPN repeatedly.", "2", "2", "2", "3", "Network", "Beth Anglin", "network"),
    _seed(2, "Email server not responding", "Outlook shows disconnected for all finance users.", "1", "1", "1", "1", "Service Desk", "", "software"),
    _seed(3, "Printer on floor 3 is offline", "Printer HP-3F shows offline since morning.", "2", "3", "3", "5", "Hardware", "Fred Luddy", "hardware"),
    _seed(4, "Unable to login to HR portal", "Password reset link not working.", "6", "2", "3", "4", "Service Desk", "Beth Anglin", "software"),
]
INCIDENTS[3]["resolved_at"] = "2026-09-30 16:20:00"
INCIDENTS[3]["close_code"] = "Solved (Permanently)"
INCIDENTS[3]["close_notes"] = "Password reset flow fixed after SSO config update."


def _view(rec: dict) -> dict:
    out = dict(rec)
    out["state"] = STATES.get(rec["state"], rec["state"])
    out["urgency"] = LEVELS.get(rec["urgency"], rec["urgency"])
    out["impact"] = LEVELS.get(rec["impact"], rec["impact"])
    out["priority"] = PRIORITIES.get(rec["priority"], rec["priority"])
    return out


def _matches(rec: dict, query: str) -> bool:
    # split in AND-terms; "^OR" attaches a term to the previous one (OR-group)
    groups: list[list[str]] = []
    for term in query.split("^"):
        if not term or term.startswith("ORDERBY"):
            continue
        if term.startswith("OR") and groups:
            groups[-1].append(term[2:])
        else:
            groups.append([term])
    for group in groups:
        if not any(_term(rec, t) for t in group):
            return False
    return True


def _term(rec: dict, term: str) -> bool:
    m = re.match(r"^([\w.]+)LIKE(.*)$", term)
    if m:
        return m.group(2).lower() in str(rec.get(m.group(1), "")).lower()
    m = re.match(r"^([\w.]+)=(.*)$", term)
    if m:
        field, value = m.groups()
        if field == "active":
            return (rec["state"] not in ("6", "7", "8")) == (value == "true")
        if field == "assignment_group.name":
            return rec["assignment_group"].lower() == value.lower()
        return str(rec.get(field, "")).lower() == value.lower()
    return True


@app.get("/api/now/table/incident")
async def list_incidents(request: Request):
    q = request.query_params
    rows = [r for r in INCIDENTS if _matches(r, q.get("sysparm_query", ""))]
    rows = sorted(rows, key=lambda r: r["number"], reverse=True)[: int(q.get("sysparm_limit", 10))]
    return {"result": [_view(r) for r in rows]}


@app.post("/api/now/table/incident")
async def create_incident(request: Request):
    body = await request.json()
    n = len(INCIDENTS) + 1
    urgency, impact = body.get("urgency", "3"), body.get("impact", "3")
    prio = str(min(5, max(1, int(urgency) + int(impact) - 1)))
    rec = _seed(n, body.get("short_description", ""), body.get("description", ""), "1", urgency, impact, prio, "", "", body.get("category", ""))
    rec["opened_at"] = rec["sys_updated_on"] = _now()
    INCIDENTS.append(rec)
    return {"result": _view(rec)}


@app.get("/api/now/table/sys_user")
async def users(request: Request):
    query = request.query_params.get("sysparm_query", "")
    known = {"abel.tuter", "abel tuter", "abel.tuter@example.com"}
    values = {t.split("=", 1)[1].lower() for t in query.replace("^OR", "^").split("^") if "=" in t}
    return {"result": [{"sys_id": "user0001"}] if values & known else []}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081)

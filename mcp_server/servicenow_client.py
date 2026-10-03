"""Thin, dependency-light client for the ServiceNow Table API (incident table).

Design notes
------------
* Pure ``requests`` (sync). The MCP server wraps calls in ``asyncio.to_thread``.
* GET requests are retried on 429/502/503/504. POST is NEVER retried so we
  cannot create duplicate incidents by accident.
* ``sysparm_display_value=true`` is used so the LLM sees human readable values
  ("In Progress" instead of "2", "Network" instead of a sys_id, ...).
* All user supplied text that ends up inside an encoded query is stripped of the
  ``^`` character, which is ServiceNow's condition separator (query injection).
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("servicenow")

INCIDENT_TABLE = "/api/now/table/incident"
USER_TABLE = "/api/now/table/sys_user"

INCIDENT_FIELDS = [
    "number",
    "short_description",
    "description",
    "state",
    "priority",
    "urgency",
    "impact",
    "category",
    "subcategory",
    "caller_id",
    "assigned_to",
    "assignment_group",
    "opened_at",
    "sys_updated_on",
    "resolved_at",
    "close_code",
    "close_notes",
    "sys_id",
]

STATE_CODES = {
    "new": "1",
    "in_progress": "2",
    "on_hold": "3",
    "resolved": "6",
    "closed": "7",
    "canceled": "8",
}

PRIORITY_CODES = {
    "critical": "1",
    "high": "2",
    "moderate": "3",
    "low": "4",
    "planning": "5",
}


class ServiceNowError(Exception):
    """Raised for any configuration, network or API level problem."""


def clean_query_value(value: str) -> str:
    """Remove characters that could break out of an encoded-query condition."""
    return (value or "").replace("^", " ").replace("\n", " ").strip()


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    """Make sure every value is a plain string (reference fields can be dicts)."""
    flat: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            value = value.get("display_value") or value.get("value") or ""
        flat[key] = value
    return flat


class ServiceNowClient:
    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        if not (instance_url and username and password):
            raise ServiceNowError(
                "ServiceNow is not configured. Set SERVICENOW_INSTANCE_URL, "
                "SERVICENOW_USERNAME and SERVICENOW_PASSWORD in the .env file."
            )
        self.base_url = instance_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ #
    # low level
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        logger.info("ServiceNow %s %s params=%s", method, path, params)
        try:
            resp = self.session.request(
                method, url, params=params, json=json_body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ServiceNowError(f"Network error calling ServiceNow: {exc}") from exc

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except ValueError:
                detail = resp.text[:200]
            raise ServiceNowError(
                f"ServiceNow returned HTTP {resp.status_code}: {detail or resp.reason}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ServiceNowError(
                "ServiceNow returned a non-JSON response. If you use a personal "
                "developer instance (PDI) it may be hibernating - wake it up at "
                "developer.servicenow.com and retry."
            ) from exc

    # ------------------------------------------------------------------ #
    # incidents
    # ------------------------------------------------------------------ #
    def get_incident(self, number: str) -> dict[str, Any] | None:
        data = self._request(
            "GET",
            INCIDENT_TABLE,
            params={
                "sysparm_query": f"number={clean_query_value(number)}",
                "sysparm_limit": 1,
                "sysparm_display_value": "true",
                "sysparm_exclude_reference_link": "true",
                "sysparm_fields": ",".join(INCIDENT_FIELDS),
            },
        )
        results = data.get("result") or []
        return _flatten(results[0]) if results else None

    def list_incidents(self, encoded_query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            INCIDENT_TABLE,
            params={
                "sysparm_query": encoded_query,
                "sysparm_limit": limit,
                "sysparm_display_value": "true",
                "sysparm_exclude_reference_link": "true",
                "sysparm_fields": ",".join(INCIDENT_FIELDS),
            },
        )
        return [_flatten(r) for r in data.get("result") or []]

    def resolve_user_sys_id(self, identifier: str) -> str | None:
        """Find a user by user_name, e-mail or full name. Returns sys_id or None."""
        ident = clean_query_value(identifier)
        if not ident:
            return None
        data = self._request(
            "GET",
            USER_TABLE,
            params={
                "sysparm_query": f"user_name={ident}^ORemail={ident}^ORname={ident}",
                "sysparm_limit": 1,
                "sysparm_fields": "sys_id",
            },
        )
        results = data.get("result") or []
        return results[0].get("sys_id") if results else None

    def create_incident(self, fields: dict[str, Any]) -> dict[str, Any]:
        data = self._request(
            "POST",
            INCIDENT_TABLE,
            params={
                "sysparm_display_value": "true",
                "sysparm_exclude_reference_link": "true",
                "sysparm_fields": ",".join(INCIDENT_FIELDS),
            },
            json_body=fields,
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise ServiceNowError("Unexpected response while creating incident.")
        return _flatten(result)

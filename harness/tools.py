"""Tool layer.

Single source of truth for the tools the agent can use. Each tool has:
  - a JSON schema (what the LLM sees, OpenAI function-calling format)
  - an executor (an HTTP call to the Go tool server)

Both the LLM investigator and the deterministic baseline call tools through
the same ToolBox.call() entry point, so every investigation - human-scripted
or model-driven — produces the same auditable trace format.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config import settings


@dataclass
class ToolCallRecord:
    step: int
    tool: str
    args: dict
    ok: bool
    duration_ms: int
    result_summary: str
    result: Any = field(repr=False, default=None)


class ToolError(Exception):
    pass


class ToolBox:
    """Executes tools against the Go tool server for one incident and records a trace."""

    def __init__(self, incident_id: str, base_url: str | None = None):
        self.incident_id = incident_id
        self.base = (base_url or settings.toolserver_url).rstrip("/")
        self.trace: list[ToolCallRecord] = []
        self._step = 0
        self._session = requests.Session()

    # ------------------------------------------------------------------ http
    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}/api/incidents/{self.incident_id}{path}"
        last_err = None
        for attempt in range(3):  # bounded retry with backoff for transient failures
            try:
                r = self._session.get(url, params=params or {}, timeout=10)
                if r.status_code >= 500:
                    raise ToolError(f"server error {r.status_code}")
                if r.status_code >= 400:
                    # 4xx is the agent's mistake (bad service name etc.) — return it
                    # as an observation so the model can self-correct, don't retry.
                    return {"error": r.json().get("error", r.text)}
                return r.json()
            except (requests.ConnectionError, requests.Timeout, ToolError) as e:
                last_err = e
                time.sleep(0.3 * (attempt + 1))
        raise ToolError(f"tool server unreachable after retries: {last_err}")

    # ----------------------------------------------------------------- tools
    def get_incident(self) -> Any:
        """Incident summary and the alerts that fired."""
        return self._get("")

    def get_topology(self) -> Any:
        return self._get("/topology")

    def query_logs(self, service: str = "", level: str = "", grep: str = "",
                   since: str = "", until: str = "", limit: int = 40) -> Any:
        return self._get("/logs", {"service": service, "level": level, "grep": grep,
                                   "since": since, "until": until, "limit": limit})

    def query_metrics(self, service: str, name: str = "", since: str = "", until: str = "") -> Any:
        return self._get("/metrics", {"service": service, "name": name, "since": since, "until": until})

    def list_deploys(self, service: str = "", since: str = "") -> Any:
        return self._get("/deploys", {"service": service, "since": since})

    def list_config_changes(self, service: str = "", since: str = "") -> Any:
        return self._get("/config_changes", {"service": service, "since": since})

    # ------------------------------------------------------------- dispatch
    def call(self, tool: str, args: dict) -> Any:
        fn: Callable | None = {
            "get_incident": self.get_incident,
            "get_topology": self.get_topology,
            "query_logs": self.query_logs,
            "query_metrics": self.query_metrics,
            "list_deploys": self.list_deploys,
            "list_config_changes": self.list_config_changes,
        }.get(tool)
        self._step += 1
        t0 = time.monotonic()
        if fn is None:
            rec = ToolCallRecord(self._step, tool, args, False, 0, "unknown tool")
            self.trace.append(rec)
            return {"error": f"unknown tool {tool}"}
        try:
            result = fn(**args)
            ok = not (isinstance(result, dict) and "error" in result)
            summary = summarize_result(tool, result)
        except TypeError as e:  # bad args from the model — observable, recoverable
            result, ok, summary = {"error": f"bad arguments: {e}"}, False, f"bad arguments: {e}"
        dur = int((time.monotonic() - t0) * 1000)
        self.trace.append(ToolCallRecord(self._step, tool, args, ok, dur, summary, result))
        return result


def summarize_result(tool: str, result: Any) -> str:
    """One-line human summary of a tool result for trace rendering."""
    if isinstance(result, dict) and "error" in result:
        return f"error: {result['error']}"
    if tool == "query_logs":
        return f"{result.get('total_matched', 0)} matched, {result.get('returned', 0)} returned"
    if tool == "query_metrics":
        series = result.get("series", {})
        return ", ".join(f"{k} ({len(v)} pts)" for k, v in series.items()) or "no series"
    if tool == "list_deploys":
        return f"{len(result.get('deploys', []))} deploys"
    if tool == "list_config_changes":
        return f"{len(result.get('config_changes', []))} config changes"
    if tool == "get_topology":
        return f"{len(result.get('services', []))} services"
    if tool == "get_incident":
        inc = result.get("incident", {})
        return f"{inc.get('id')} {inc.get('severity')} on {inc.get('service')}"
    return "ok"


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_incident",
        "description": "Fetch the incident summary and all alerts that have fired. Call this first.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_topology",
        "description": "Service dependency graph: which services call which. Use it to walk downstream from the alerting service.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "query_logs",
        "description": "Search logs. Filter by service, level (ERROR/WARN/INFO), substring grep, and RFC3339 since/until. Returns the most recent matches up to limit, plus total_matched.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string"},
            "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", ""]},
            "grep": {"type": "string", "description": "case-insensitive substring"},
            "since": {"type": "string", "description": "RFC3339, e.g. 2026-07-14T14:00:00Z"},
            "until": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "query_metrics",
        "description": "Fetch metric time series for a service (1-minute resolution). Metrics: latency_p99_ms, error_rate_pct, rps. Omit name to get all three. Use since/until to limit the window.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string"},
            "name": {"type": "string", "enum": ["latency_p99_ms", "error_rate_pct", "rps", ""]},
            "since": {"type": "string"}, "until": {"type": "string"}},
            "required": ["service"]}}},
    {"type": "function", "function": {
        "name": "list_deploys",
        "description": "Recent deploys, optionally filtered by service and since-timestamp. Deploys near the incident start are prime suspects — but verify with logs/metrics before blaming one.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string"}, "since": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_config_changes",
        "description": "Recent configuration changes (feature flags, timeouts, pool sizes) with old/new values.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string"}, "since": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "submit_root_cause",
        "description": "Submit your final answer: a ranked list of root-cause hypotheses with evidence. Call this exactly once, when you are confident. Every hypothesis must cite concrete evidence you actually retrieved (log lines, metric onsets, deploy/config IDs).",
        "parameters": {"type": "object", "properties": {
            "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
                "rank": {"type": "integer"},
                "service": {"type": "string", "description": "service where the root cause lives"},
                "cause_type": {"type": "string",
                               "enum": ["bad_deploy", "config_change", "resource_exhaustion",
                                        "cert_expiry", "dependency_failure", "traffic_spike", "other"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "specific artifacts: deploy IDs, config IDs, quoted log lines, metric onset times"}},
                "required": ["rank", "service", "cause_type", "confidence", "summary", "evidence"]}},
            "recommended_action": {"type": "string"}},
            "required": ["hypotheses", "recommended_action"]}}},
]

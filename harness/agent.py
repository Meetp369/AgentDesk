"""The agent loop.

An investigation is a bounded loop:

    context -> LLM decides a tool call -> harness executes -> observation
    appended to context -> repeat -> agent calls submit_root_cause -> done.

Key engineering decisions:
  - The final answer is itself a tool call (`submit_root_cause`) with a JSON
    schema, not free text. This makes output structure enforceable and
    machine-checkable in eval.
  - Hard iteration budget + a "last chance" forcing turn so the agent can't
    loop forever on a free-tier key.
  - Tool observations are truncated before entering context: metric series are
    downsampled to summary stats + anomaly onset, logs capped. Context is the
    scarce resource in agentic systems; tools should compress, not dump.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import settings
from .llm import LLMClient, parse_tool_calls
from .tools import TOOL_SCHEMAS, ToolBox

SYSTEM_PROMPT = """You are AgentDesk, an SRE incident-triage agent. Your job: find the root cause of the incident using the provided tools, then submit a ranked hypothesis list with concrete evidence.

Investigation doctrine:
1. Start with get_incident, then get_topology. Root causes usually live DOWNSTREAM (in dependencies) of the alerting service.
2. Correlation windows matter: find WHEN the degradation started (query_metrics), then ask what changed just before that (list_deploys, list_config_changes).
3. A deploy near the incident is a suspect, not a verdict. Verify: do that service's ERROR logs and metrics actually implicate it? Innocent deploys near incidents are common.
4. Read error logs for causal signatures: panics/stack traces (bad deploy), pool/queue timeouts (resource exhaustion), x509/TLS (cert expiry), timeout values matching a config change.
5. Be economical: use filters (service, level=ERROR, since) instead of dumping everything. You have a limited call budget.
6. When confident, call submit_root_cause exactly once. Every hypothesis MUST cite evidence you actually retrieved (deploy IDs, config IDs, quoted log lines, metric onset times). Include a plausible second hypothesis with lower confidence unless the evidence is overwhelming.

Never invent evidence. If tools return errors, adjust your arguments and continue."""


@dataclass
class InvestigationResult:
    incident_id: str
    mode: str
    report: dict | None
    iterations: int
    tool_calls: int
    duration_s: float
    trace: list = field(default_factory=list)
    error: str | None = None


def _truncate_observation(tool: str, result: Any) -> Any:
    """Compress tool output before it enters LLM context."""
    if not isinstance(result, dict):
        return result
    if tool == "query_metrics" and "series" in result:
        out = {"service": result.get("service"), "series": {}}
        for name, pts in result["series"].items():
            out["series"][name] = _summarize_series(pts)
        return out
    if tool == "query_logs" and "lines" in result:
        lines = result["lines"][-25:]
        return {"total_matched": result.get("total_matched"),
                "showing_most_recent": len(lines),
                "lines": [f"{l['ts']} [{l['level']}] {l['service']}: {l['msg']}" for l in lines]}
    return result


def _summarize_series(pts: list[dict]) -> dict:
    """Downsample a series to stats + detected anomaly onset (simple z-score-ish step detection)."""
    if not pts:
        return {"points": 0}
    vals = [p["v"] for p in pts]
    n = len(vals)
    baseline = vals[: max(3, n // 3)]
    b_mean = sum(baseline) / len(baseline)
    b_spread = max(1e-6, max(baseline) - min(baseline))
    onset = None
    for p in pts[len(baseline):]:
        if abs(p["v"] - b_mean) > max(3 * b_spread, 0.5 * abs(b_mean) + 1):
            onset = p["ts"]
            break
    return {"points": n, "first_ts": pts[0]["ts"], "last_ts": pts[-1]["ts"],
            "baseline_mean": round(b_mean, 2), "min": round(min(vals), 2),
            "max": round(max(vals), 2), "last": round(vals[-1], 2),
            "anomaly_onset": onset}


def run_llm_investigation(incident_id: str,
                          on_event: Callable[[str, dict], None] | None = None,
                          client: LLMClient | None = None) -> InvestigationResult:
    """Drive the LLM through a bounded tool-calling loop for one incident."""
    import time as _time
    t_start = _time.monotonic()
    emit = on_event or (lambda kind, data: None)
    box = ToolBox(incident_id)
    llm = client or LLMClient()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Investigate incident {incident_id}. Begin."},
    ]
    report, error = None, None
    iterations = 0

    for i in range(settings.max_iterations):
        iterations = i + 1
        forcing = i == settings.max_iterations - 1
        if forcing:
            messages.append({"role": "user", "content":
                             "Budget exhausted. Call submit_root_cause NOW with your best ranked hypotheses "
                             "based on evidence gathered so far."})
        msg = llm.chat(messages, TOOL_SCHEMAS)
        messages.append({"role": "assistant",
                         "content": msg.get("content"),
                         **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
        calls = parse_tool_calls(msg)

        if not calls:
            # Model produced prose instead of a tool call - nudge it back on rails.
            emit("thought", {"text": (msg.get("content") or "").strip()})
            messages.append({"role": "user", "content":
                             "Respond only with tool calls. When finished, call submit_root_cause."})
            continue

        done = False
        for call_id, name, args in calls:
            if name == "submit_root_cause":
                report = _validate_report(args)
                if report is None:
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps({"error": "invalid report shape; re-submit matching the schema"})})
                    continue
                emit("final", {"report": report})
                done = True
                break
            if "_malformed" in args:
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps({"error": "arguments were not valid JSON; retry"})})
                emit("tool_error", {"tool": name})
                continue
            result = box.call(name, args)
            rec = box.trace[-1]
            emit("tool", {"step": rec.step, "tool": name, "args": args,
                          "summary": rec.result_summary, "ms": rec.duration_ms})
            observation = _truncate_observation(name, result)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(observation)})
        if done:
            break

    if report is None:
        error = "agent did not submit a root cause within the iteration budget"

    return InvestigationResult(incident_id=incident_id, mode="llm", report=report,
                               iterations=iterations, tool_calls=len(box.trace),
                               duration_s=round(_time.monotonic() - t_start, 2),
                               trace=box.trace, error=error)


def _validate_report(args: dict) -> dict | None:
    hyps = args.get("hypotheses")
    if not isinstance(hyps, list) or not hyps:
        return None
    cleaned = []
    for h in hyps:
        if not isinstance(h, dict) or not h.get("service") or not h.get("cause_type"):
            return None
        h.setdefault("evidence", [])
        h.setdefault("confidence", 0.5)
        cleaned.append(h)
    cleaned.sort(key=lambda h: h.get("rank", 99))
    return {"hypotheses": cleaned, "recommended_action": args.get("recommended_action", "")}

"""Deterministic baseline investigator.

A hand-written triage policy that uses the *same tools* as the LLM agent:

    alerts -> topology -> dependency closure -> deploys/config changes ->
    ERROR logs per suspect -> metric onset correlation -> scored hypotheses.

Why it exists (three reasons, all defensible in an interview):
  1. Demo reliability: runs offline, no API key, fully deterministic.
  2. Evaluation baseline: "is the LLM actually better than 200 lines of
     rules?" is the first question any serious reviewer asks of an agent.
  3. Harness testing: exercises the whole tool pipeline in CI without
     spending tokens.

Scoring model: each candidate cause gets points for (a) temporal proximity of
its change artifact to the measured anomaly onset, (b) density of implicating
ERROR logs, (c) topology proximity to the alerting service, (d) matching a
known causal log signature. Weights are hand-tuned, and that fragility is the
point of comparison with the LLM.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Callable

from .agent import InvestigationResult, _summarize_series
from .tools import ToolBox

SIGNATURES = [
    ("cert_expiry", re.compile(r"x509|certificate has expired|tls handshake", re.I)),
    ("resource_exhaustion", re.compile(r"pool|too many connections|queue full|waiters=", re.I)),
    ("bad_deploy", re.compile(r"panic|nil pointer|segfault|stack trace|NullPointer", re.I)),
    ("dependency_failure", re.compile(r"upstream .* (500|502|503|timeout)|upstream timeout", re.I)),
]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _dep_closure(topology: dict, root: str) -> dict[str, int]:
    """BFS depth of every service reachable from `root` via depends_on."""
    graph = {s["name"]: s["depends_on"] for s in topology["services"]}
    depth, frontier = {root: 0}, [root]
    while frontier:
        nxt = []
        for svc in frontier:
            for dep in graph.get(svc, []):
                if dep not in depth:
                    depth[dep] = depth[svc] + 1
                    nxt.append(dep)
        frontier = nxt
    return depth


def run_baseline_investigation(incident_id: str,
                               on_event: Callable[[str, dict], None] | None = None) -> InvestigationResult:
    t_start = time.monotonic()
    emit = on_event or (lambda kind, data: None)
    box = ToolBox(incident_id)

    def call(tool, **args):
        result = box.call(tool, args)
        rec = box.trace[-1]
        emit("tool", {"step": rec.step, "tool": tool, "args": args,
                      "summary": rec.result_summary, "ms": rec.duration_ms})
        return result

    # 1. Alert context
    inc = call("get_incident")
    alerting = inc["incident"]["service"]
    started = _parse(inc["incident"]["started_at"])
    lookback = (started - timedelta(minutes=45)).isoformat().replace("+00:00", "Z")
    day_back = (started - timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    # 2. Blast radius = dependency closure of the alerting service
    topo = call("get_topology")
    depth = _dep_closure(topo, alerting)

    # 3. What changed recently?
    deploys = call("list_deploys", since=day_back)["deploys"]
    cfgs = call("list_config_changes", since=day_back)["config_changes"]

    # 4. Where are the errors, and what do they say?
    svc_errors: dict[str, dict] = {}
    for svc in sorted(depth, key=depth.get):
        logs = call("query_logs", service=svc, level="ERROR", since=lookback, limit=60)
        if logs["total_matched"] == 0:
            continue
        sig_hits: dict[str, int] = {}
        for line in logs["lines"]:
            for cause, rx in SIGNATURES:
                if rx.search(line["msg"]):
                    sig_hits[cause] = sig_hits.get(cause, 0) + 1
        svc_errors[svc] = {"count": logs["total_matched"],
                           "signatures": sig_hits,
                           "samples": [f"{l['ts']} {l['msg']}" for l in logs["lines"][-3:]]}

    # 5. Anomaly onset per erroring service (from error_rate metric)
    onsets: dict[str, str | None] = {}
    for svc in svc_errors:
        m = call("query_metrics", service=svc, name="error_rate_pct")
        series = m.get("series", {}).get("error_rate_pct", [])
        onsets[svc] = _summarize_series(series).get("anomaly_onset")

    # 6. Score hypotheses ------------------------------------------------
    hypotheses = []

    def proximity_score(ts: str, onset: str | None) -> float:
        if onset is None:
            return 0.0
        gap = abs((_parse(onset) - _parse(ts)).total_seconds()) / 60
        if gap <= 3: return 3.0
        if gap <= 15: return 2.0
        if gap <= 60: return 1.0
        return 0.0

    # deepest service with a strong signature = primary suspect zone
    for svc, info in svc_errors.items():
        top_sig = max(info["signatures"], key=info["signatures"].get) if info["signatures"] else None
        base = min(3.0, info["count"] / 50) + depth.get(svc, 0) * 0.4

        if top_sig == "cert_expiry":
            # cert errors name the peer: attribute cause to the dependency being dialed
            target = svc
            m = re.search(r"to ([\w-]+)", info["samples"][-1])
            if m and m.group(1) in depth:
                target = m.group(1)
            hypotheses.append({"service": target, "cause_type": "cert_expiry",
                               "score": base + 3.0,
                               "summary": f"TLS certificate expiry on {target}; {svc} handshakes failing.",
                               "evidence": info["samples"][-2:]})
        elif top_sig == "resource_exhaustion":
            hypotheses.append({"service": svc, "cause_type": "resource_exhaustion",
                               "score": base + 2.5,
                               "summary": f"{svc} exhausting a bounded resource (connection pool/queue).",
                               "evidence": info["samples"][-2:]})
        elif top_sig == "bad_deploy":
            for d in deploys:
                if d["service"] == svc:
                    p = proximity_score(d["ts"], onsets.get(svc))
                    hypotheses.append({"service": svc, "cause_type": "bad_deploy",
                                       "score": base + 1.0 + p * 1.2,
                                       "summary": f"Deploy {d['id']} ({svc} {d['version']}) coincides with error onset; logs show crash signatures.",
                                       "evidence": [f"deploy {d['id']} at {d['ts']}: {d['summary']}",
                                                    f"error onset {onsets.get(svc)}"] + info["samples"][-1:]})

    # config changes scored against every erroring service's onset
    for c in cfgs:
        best_onset = next((o for o in onsets.values() if o), None)
        p = proximity_score(c["ts"], best_onset)
        if p > 0:
            hypotheses.append({"service": c["service"], "cause_type": "config_change",
                               "score": 2.0 + p * 1.4,
                               "summary": f"Config {c['id']} changed {c['key']} {c['old']}->{c['new']} right at error onset.",
                               "evidence": [f"config {c['id']} at {c['ts']} by {c['author']}",
                                            f"error onset {best_onset}"]})

    # deploys near onset with no log signature: weak fallback suspects (red herrings land here)
    for d in deploys:
        if any(h["cause_type"] == "bad_deploy" and h["service"] == d["service"] for h in hypotheses):
            continue
        best_onset = next((o for o in onsets.values() if o), None)
        p = proximity_score(d["ts"], best_onset)
        if p > 0:
            hypotheses.append({"service": d["service"], "cause_type": "bad_deploy",
                               "score": 0.5 + p * 0.6,
                               "summary": f"Deploy {d['id']} is temporally close but uncorroborated by logs (possible red herring).",
                               "evidence": [f"deploy {d['id']} at {d['ts']}: {d['summary']}"]})

    if not hypotheses:
        hypotheses.append({"service": alerting, "cause_type": "other", "score": 0.1,
                           "summary": "No causal signal found; escalate to on-call.", "evidence": []})

    hypotheses.sort(key=lambda h: -h["score"])
    top = max(h["score"] for h in hypotheses)
    report = {"hypotheses": [
        {"rank": i + 1, "service": h["service"], "cause_type": h["cause_type"],
         "confidence": round(min(0.97, h["score"] / (top + 1.5)), 2),
         "summary": h["summary"], "evidence": h["evidence"]}
        for i, h in enumerate(hypotheses[:3])],
        "recommended_action": _action(hypotheses[0])}
    emit("final", {"report": report})

    return InvestigationResult(incident_id=incident_id, mode="baseline", report=report,
                               iterations=1, tool_calls=len(box.trace),
                               duration_s=round(time.monotonic() - t_start, 2), trace=box.trace)


def _action(h: dict) -> str:
    return {
        "bad_deploy": f"Roll back the implicated deploy on {h['service']} and page its owning team.",
        "config_change": f"Revert the implicated config change on {h['service']}.",
        "resource_exhaustion": f"Raise the exhausted resource limit on {h['service']} and add load shedding.",
        "cert_expiry": f"Rotate/renew the TLS certificate on {h['service']} immediately.",
    }.get(h["cause_type"], f"Escalate to the {h['service']} on-call.")
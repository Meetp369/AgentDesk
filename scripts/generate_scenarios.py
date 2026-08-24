#!/usr/bin/env python3
"""Generate deterministic synthetic telemetry fixtures for AgentDesk incidents.

Each scenario directory contains:
  fixtures.json  - everything the tool server is allowed to serve
  truth.json     - ground truth labels, used ONLY by the eval harness (never served)

Design notes:
- All randomness is seeded per-incident so fixtures are reproducible.
- Telemetry is internally consistent: metric anomalies, error logs, and
  deploy/config timestamps all agree on a single causal story per incident,
  plus at least one red herring to keep the investigation non-trivial.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scenarios"

TOPOLOGY = {
    "services": [
        {"name": "api-gateway", "depends_on": ["checkout", "search", "accounts"]},
        {"name": "checkout", "depends_on": ["payments", "orders", "catalog"]},
        {"name": "payments", "depends_on": ["card-vault"]},
        {"name": "orders", "depends_on": ["orders-db"]},
        {"name": "search", "depends_on": ["catalog"]},
        {"name": "catalog", "depends_on": []},
        {"name": "accounts", "depends_on": []},
        {"name": "card-vault", "depends_on": []},
        {"name": "orders-db", "depends_on": []},
    ]
}

INFO_MSGS = [
    "request completed status=200 path={path} latency_ms={lat}",
    "cache hit key=sku:{n}",
    "health check ok",
    "processed batch size={n}",
    "gc pause 4ms",
]
PATHS = ["/v1/charge", "/v1/cart", "/v1/order", "/v1/lookup", "/v1/session"]


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def gen_baseline_logs(rng, service, start, end, per_min=4):
    logs = []
    t = start
    while t < end:
        for _ in range(rng.randint(max(1, per_min - 2), per_min + 2)):
            msg = rng.choice(INFO_MSGS).format(
                path=rng.choice(PATHS), lat=rng.randint(8, 90), n=rng.randint(100, 9999)
            )
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))),
                         "service": service, "level": "INFO", "msg": msg})
        t += timedelta(minutes=1)
    return logs


def gen_metric(rng, start, end, base, jitter, anomaly_from=None, anomaly_value=None, ramp_min=3):
    """1-minute resolution series; optional step/ramp anomaly starting at anomaly_from."""
    pts, t, i = [], start, 0
    while t <= end:
        v = base + rng.uniform(-jitter, jitter)
        if anomaly_from and t >= anomaly_from:
            k = min(1.0, ((t - anomaly_from).total_seconds() / 60) / max(1, ramp_min))
            v = base + (anomaly_value - base) * k + rng.uniform(-jitter, jitter)
        pts.append({"ts": iso(t), "v": round(max(0, v), 2)})
        t += timedelta(minutes=1)
        i += 1
    return pts


def base_world(rng, t0, window_min=90):
    """Common healthy-world telemetry; scenarios mutate on top."""
    start = t0 - timedelta(minutes=window_min)
    end = t0 + timedelta(minutes=15)
    logs, metrics = [], {}
    for svc in [s["name"] for s in TOPOLOGY["services"]]:
        logs.extend(gen_baseline_logs(rng, svc, start, end))
        metrics[svc] = {
            "latency_p99_ms": gen_metric(rng, start, end, base=rng.uniform(60, 140), jitter=12),
            "error_rate_pct": gen_metric(rng, start, end, base=rng.uniform(0.1, 0.5), jitter=0.15),
            "rps": gen_metric(rng, start, end, base=rng.uniform(80, 400), jitter=25),
        }
    return start, end, logs, metrics


def sort_write(path, incident, alerts, logs, metrics, deploys, config_changes, truth):
    logs.sort(key=lambda l: l["ts"])
    fixtures = {
        "incident": incident,
        "alerts": alerts,
        "topology": TOPOLOGY,
        "logs": logs,
        "metrics": metrics,
        "deploys": sorted(deploys, key=lambda d: d["ts"]),
        "config_changes": sorted(config_changes, key=lambda c: c["ts"]),
    }
    path.mkdir(parents=True, exist_ok=True)
    (path / "fixtures.json").write_text(json.dumps(fixtures, indent=1))
    (path / "truth.json").write_text(json.dumps(truth, indent=2))
    print(f"wrote {path.name}: {len(logs)} logs, {len(deploys)} deploys")


# ---------------------------------------------------------------- INC-1042
def inc_1042():
    """Bad deploy in payments -> checkout p99 latency alert (dependency cascade).
    Red herring: catalog deployed 3h earlier (benign)."""
    rng = random.Random(1042)
    t0 = datetime(2026, 7, 14, 14, 32, tzinfo=timezone.utc)  # alert fired
    fault = t0 - timedelta(minutes=8)                        # bad deploy landed
    start, end, logs, metrics = base_world(rng, t0)

    # payments degrades at fault time; checkout degrades ~1 min later (cascade)
    metrics["payments"]["error_rate_pct"] = gen_metric(rng, start, end, 0.3, 0.1, fault, 23.0, ramp_min=2)
    metrics["payments"]["latency_p99_ms"] = gen_metric(rng, start, end, 90, 10, fault, 2400, ramp_min=2)
    metrics["checkout"]["latency_p99_ms"] = gen_metric(rng, start, end, 120, 12, fault + timedelta(minutes=1), 3100, ramp_min=3)
    metrics["checkout"]["error_rate_pct"] = gen_metric(rng, start, end, 0.4, 0.1, fault + timedelta(minutes=1), 6.5, ramp_min=3)

    t = fault
    while t < end:
        for _ in range(rng.randint(5, 9)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "payments",
                         "level": "ERROR",
                         "msg": "panic recovered: nil pointer dereference in vault.LookupToken (charge.go:214) request_id=" + hex(rng.getrandbits(32))[2:]})
        for _ in range(rng.randint(2, 4)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "checkout",
                         "level": "ERROR",
                         "msg": "upstream payments returned 500 path=/v1/charge retry=2 giving up"})
        t += timedelta(minutes=1)

    deploys = [
        {"id": "d-9812", "service": "payments", "version": "v2.14.0", "ts": iso(fault),
         "author": "mkumar", "summary": "refactor card vault token lookup path"},
        {"id": "d-9807", "service": "catalog", "version": "v5.3.1", "ts": iso(t0 - timedelta(hours=3)),
         "author": "jchen", "summary": "add facet filters to product listings"},  # red herring
        {"id": "d-9791", "service": "accounts", "version": "v1.9.9", "ts": iso(t0 - timedelta(hours=26)),
         "author": "sofia", "summary": "bump base image"},
    ]
    incident = {"id": "INC-1042", "title": "Checkout p99 latency SLO breach",
                "service": "checkout", "started_at": iso(t0), "severity": "SEV2",
                "description": "PagerDuty alert: checkout p99 latency > 2000ms for 5m; error rate elevated."}
    alerts = [
        {"ts": iso(t0), "service": "checkout", "name": "latency_p99_slo_breach", "threshold": "2000ms", "observed": "3080ms"},
        {"ts": iso(t0 + timedelta(minutes=1)), "service": "payments", "name": "error_rate_high", "threshold": "5%", "observed": "22.7%"},
    ]
    truth = {"incident": "INC-1042", "service": "payments", "cause_type": "bad_deploy",
             "artifact": "d-9812",
             "summary": "Deploy d-9812 (payments v2.14.0) introduced a nil-pointer panic in vault.LookupToken, spiking payments error rate/latency and cascading into checkout p99."}
    sort_write(ROOT / "INC-1042", incident, alerts, logs, metrics, deploys, [], truth)


# ---------------------------------------------------------------- INC-1043
def inc_1043():
    """DB connection pool exhaustion in orders under a traffic ramp. No relevant deploys."""
    rng = random.Random(1043)
    t0 = datetime(2026, 7, 15, 9, 18, tzinfo=timezone.utc)
    fault = t0 - timedelta(minutes=12)
    start, end, logs, metrics = base_world(rng, t0)

    metrics["orders"]["rps"] = gen_metric(rng, start, end, 220, 20, fault - timedelta(minutes=10), 940, ramp_min=12)
    metrics["orders"]["latency_p99_ms"] = gen_metric(rng, start, end, 95, 10, fault, 5200, ramp_min=4)
    metrics["orders"]["error_rate_pct"] = gen_metric(rng, start, end, 0.2, 0.1, fault, 14.0, ramp_min=4)
    metrics["orders-db"]["latency_p99_ms"] = gen_metric(rng, start, end, 12, 3, fault, 18, ramp_min=5)  # db itself mostly fine

    t = fault
    while t < end:
        for _ in range(rng.randint(6, 10)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "orders",
                         "level": "ERROR",
                         "msg": "pgpool: timed out waiting for connection from pool (size=20, waiters=%d)" % rng.randint(40, 180)})
        logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "checkout",
                     "level": "WARN", "msg": "orders /v1/order slow, latency_ms=%d" % rng.randint(2500, 6000)})
        t += timedelta(minutes=1)

    deploys = [
        {"id": "d-9825", "service": "search", "version": "v3.2.0", "ts": iso(t0 - timedelta(hours=5)),
         "author": "jchen", "summary": "reindex pipeline tuning"},
    ]
    incident = {"id": "INC-1043", "title": "Orders API elevated errors and latency",
                "service": "orders", "started_at": iso(t0), "severity": "SEV2",
                "description": "Alert: orders error rate > 5% for 5m during morning traffic ramp."}
    alerts = [
        {"ts": iso(t0), "service": "orders", "name": "error_rate_high", "threshold": "5%", "observed": "13.8%"},
    ]
    truth = {"incident": "INC-1043", "service": "orders", "cause_type": "resource_exhaustion",
             "artifact": "pgpool size=20",
             "summary": "Morning traffic ramp (~4x RPS) exhausted orders' fixed DB connection pool (size 20); requests queued and timed out. No deploy involved."}
    sort_write(ROOT / "INC-1043", incident, alerts, logs, metrics, deploys, [], truth)


# ---------------------------------------------------------------- INC-1044
def inc_1044():
    """Config change on api-gateway lowered upstream timeout -> search 504s."""
    rng = random.Random(1044)
    t0 = datetime(2026, 7, 16, 17, 5, tzinfo=timezone.utc)
    fault = t0 - timedelta(minutes=6)
    start, end, logs, metrics = base_world(rng, t0)

    metrics["api-gateway"]["error_rate_pct"] = gen_metric(rng, start, end, 0.3, 0.1, fault, 9.0, ramp_min=2)
    metrics["search"]["latency_p99_ms"] = gen_metric(rng, start, end, 260, 30, None, None)  # search always slow-ish; that's the point

    t = fault
    while t < end:
        for _ in range(rng.randint(5, 8)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "api-gateway",
                         "level": "ERROR",
                         "msg": "upstream timeout after 300ms route=/v1/search upstream=search -> 504"})
        t += timedelta(minutes=1)

    config_changes = [
        {"id": "cfg-441", "service": "api-gateway", "ts": iso(fault), "author": "rpatel",
         "key": "upstream.search.timeout_ms", "old": "1500", "new": "300",
         "summary": "tighten gateway timeouts (latency project)"},
        {"id": "cfg-437", "service": "accounts", "ts": iso(t0 - timedelta(hours=9)), "author": "sofia",
         "key": "session.ttl_hours", "old": "24", "new": "12", "summary": "shorten session ttl"},
    ]
    deploys = []
    incident = {"id": "INC-1044", "title": "API gateway 5xx spike on /v1/search",
                "service": "api-gateway", "started_at": iso(t0), "severity": "SEV3",
                "description": "Alert: api-gateway 5xx rate > 5%; concentrated on search routes."}
    alerts = [
        {"ts": iso(t0), "service": "api-gateway", "name": "http_5xx_rate_high", "threshold": "5%", "observed": "8.9%"},
    ]
    truth = {"incident": "INC-1044", "service": "api-gateway", "cause_type": "config_change",
             "artifact": "cfg-441",
             "summary": "Config change cfg-441 dropped the gateway->search timeout from 1500ms to 300ms, below search's normal p99 (~260-300ms), turning routine requests into 504s."}
    sort_write(ROOT / "INC-1044", incident, alerts, logs, metrics, deploys, config_changes, truth)


# ---------------------------------------------------------------- INC-1045
def inc_1045():
    """Red herring: checkout deployed 20m before alert but is innocent.
    Real cause: TLS cert expiry on card-vault breaking payments."""
    rng = random.Random(1045)
    t0 = datetime(2026, 7, 17, 0, 3, tzinfo=timezone.utc)  # midnight cert expiry
    fault = t0 - timedelta(minutes=3)
    start, end, logs, metrics = base_world(rng, t0)

    metrics["payments"]["error_rate_pct"] = gen_metric(rng, start, end, 0.3, 0.1, fault, 96.0, ramp_min=1)
    metrics["checkout"]["error_rate_pct"] = gen_metric(rng, start, end, 0.4, 0.1, fault, 31.0, ramp_min=2)

    t = fault
    while t < end:
        for _ in range(rng.randint(6, 10)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "payments",
                         "level": "ERROR",
                         "msg": 'tls handshake to card-vault failed: x509: certificate has expired or is not yet valid (notAfter=2026-07-17T00:00:00Z)'})
        for _ in range(rng.randint(2, 4)):
            logs.append({"ts": iso(t + timedelta(seconds=rng.randint(0, 59))), "service": "checkout",
                         "level": "ERROR", "msg": "payment authorization failed: upstream 502 path=/v1/charge"})
        t += timedelta(minutes=1)

    deploys = [
        {"id": "d-9834", "service": "checkout", "version": "v7.1.0", "ts": iso(t0 - timedelta(minutes=20)),
         "author": "mkumar", "summary": "new promo banner + minor cart refactor"},  # innocent!
    ]
    incident = {"id": "INC-1045", "title": "Checkout payment failures spiking",
                "service": "checkout", "started_at": iso(t0), "severity": "SEV1",
                "description": "Alert: checkout error rate > 20%; customers cannot pay."}
    alerts = [
        {"ts": iso(t0), "service": "checkout", "name": "error_rate_high", "threshold": "20%", "observed": "29.4%"},
        {"ts": iso(t0), "service": "payments", "name": "error_rate_high", "threshold": "5%", "observed": "95.1%"},
    ]
    truth = {"incident": "INC-1045", "service": "card-vault", "cause_type": "cert_expiry",
             "artifact": "card-vault TLS cert notAfter=2026-07-17T00:00:00Z",
             "summary": "card-vault's TLS certificate expired at midnight UTC; payments' handshakes fail (x509), cascading to checkout. The checkout deploy 20m prior is a red herring."}
    sort_write(ROOT / "INC-1045", incident, alerts, logs, metrics, deploys, [], truth)


if __name__ == "__main__":
    inc_1042(); inc_1043(); inc_1044(); inc_1045()

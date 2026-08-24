# AgentDesk - AI Incident Triage Agent

An agentic harness where an LLM investigates production-style incidents through
iterative tool calls — querying logs, metrics, deploys, config changes, and the
service dependency graph — and surfaces a **ranked root-cause report with
cited evidence**.

```
 ┌────────────────────────┐   OpenAI-compatible    ┌─────────────────┐
 │  Python harness        │ ───────────────────▶   │  LLM provider   │
 │  agent loop · eval ·   │ ◀───tool calls──────   │  (Groq/Gemini/  │
 │  baseline · CLI        │                        │   any endpoint) │
 └───────────┬────────────┘                        └─────────────────┘
             │ HTTP tool calls
             ▼
 ┌────────────────────────┐
 │  Go tool server        │   narrow, filterable endpoints over incident
 │  (toolserver/)         │   telemetry: logs, metrics, deploys, config
 └───────────┬────────────┘   changes, topology, alerts
             │
             ▼
 ┌────────────────────────┐
 │  Seeded scenarios      │   4 internally-consistent synthetic incidents
 │  (scenarios/)          │   incl. a red-herring case; ground truth kept
 └────────────────────────┘   out of the server (eval-only)
```

## Why two languages

- **Go** owns the tool server: the long-running, concurrent piece that in a
  real deployment fronts telemetry backends (Loki, Prometheus, a deploy API).
- **Python** owns the agent: the loop, prompting, evidence handling, the
  deterministic baseline, and evaluation — the parts that change fastest.

## Two investigators, one tool interface

1. **LLM agent** (`harness/agent.py`) — a bounded tool-calling loop. The final
   answer is itself a schema-validated tool call (`submit_root_cause`), so
   output structure is enforced, not hoped for.
2. **Deterministic baseline** (`harness/baseline.py`) — a hand-written triage
   policy (topology walk → change correlation → log signatures → scoring)
   using the *same* tools. It serves as an offline demo mode, an eval
   baseline, and a zero-token harness test.

## Quickstart

Requires Go ≥1.21 and Python ≥3.10.

```bash
pip install -r requirements.txt
python3 scripts/generate_scenarios.py   # regenerate fixtures (deterministic)
make server                             # terminal 1: Go tool server on :8077
```

In a second terminal:

```bash
python3 -m harness list
python3 -m harness investigate INC-1042 --mode baseline   # no API key needed
python3 -m harness eval --mode baseline                   # score all incidents
python3 -m tests.test_agent_loop                          # agent-loop tests
```

### LLM mode (free API key)

Create a free key at https://console.groq.com (no credit card), then:

```bash
cp .env.example .env   # add your key
export $(grep -v '^#' .env | xargs)
python3 -m harness investigate INC-1045 --mode llm
python3 -m harness eval --mode llm
```

Any OpenAI-compatible endpoint works via `AGENTDESK_LLM_BASE_URL` /
`AGENTDESK_LLM_MODEL` (Google AI Studio, OpenRouter, local Ollama, ...).
Note: free tiers rate-limit tokens/minute; the client honors `Retry-After`
on 429s, so long investigations may pause briefly.

## Incidents

| ID | Story | Root cause |
|----|-------|-----------|
| INC-1042 | Checkout p99 SLO breach | Bad deploy in `payments` (nil-pointer panic), cascading upstream. Benign `catalog` deploy as decoy. |
| INC-1043 | Orders errors during morning ramp | DB connection-pool exhaustion under 4x traffic. No deploy involved. |
| INC-1044 | Gateway 504s on search routes | Config change dropped gateway→search timeout 1500ms→300ms, below search's normal p99. |
| INC-1045 | Midnight payment outage (SEV1) | `card-vault` TLS cert expiry. A `checkout` deploy 20 min earlier is an intentional red herring. |

Ground-truth labels live in `scenarios/*/truth.json` and are **never served**
by the tool server — the agent must earn its answer from telemetry; only the
eval harness reads labels.

## Measured results

On the 4 seeded scenarios: baseline achieves 4/4 top-1 service and cause
accuracy in ~10 tool calls and <0.1s per incident. LLM-mode accuracy and
latency depend on the model; run `python3 -m harness eval --mode llm` to
produce your own numbers. All performance claims for this project refer to
these seeded scenarios, not a production deployment.

## Layout

```
harness/        Python: agent loop, baseline, LLM client, tools, CLI, config
toolserver/     Go: telemetry tool server
scenarios/      generated fixtures + eval-only ground truth
scripts/        deterministic scenario generator
tests/          agent-loop tests (scripted fake LLM, no key needed)
```

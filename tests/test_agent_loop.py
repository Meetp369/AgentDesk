"""Tests for the LLM agent loop using a scripted fake client.

Validates the loop mechanics without any API key:
  - tool calls are executed and observations threaded back with tool_call_id
  - malformed tool arguments produce a recoverable error observation
  - submit_root_cause terminates the loop and validates shape
  - iteration budget forces finalization

Run: python -m tests.test_agent_loop   (tool server must be up)
"""
import json
import sys

sys.path.insert(0, ".")
from harness.agent import run_llm_investigation  # noqa: E402


class ScriptedLLM:
    """Plays back a fixed sequence of assistant messages; asserts on the context it receives."""

    def __init__(self, script):
        self.script = list(script)
        self.seen_contexts = []

    def chat(self, messages, tools, temperature=0.1):
        self.seen_contexts.append([m["role"] for m in messages])
        assert any(t["function"]["name"] == "submit_root_cause" for t in tools)
        if not self.script:
            raise AssertionError("script exhausted — loop did not terminate when expected")
        return self.script.pop(0)


def tc(name, args, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args) if isinstance(args, dict) else args}}


FINAL = {"hypotheses": [{"rank": 1, "service": "payments", "cause_type": "bad_deploy",
                         "confidence": 0.9, "summary": "d-9812 broke vault lookup",
                         "evidence": ["deploy d-9812", "panic in vault.LookupToken"]}],
         "recommended_action": "roll back d-9812"}


def test_happy_path():
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tc("get_incident", {})]},
        {"role": "assistant", "content": None, "tool_calls": [tc("query_logs", {"service": "payments", "level": "ERROR", "limit": 5}, "c2")]},
        {"role": "assistant", "content": None, "tool_calls": [tc("submit_root_cause", FINAL, "c3")]},
    ])
    r = run_llm_investigation("INC-1042", client=llm)
    assert r.error is None, r.error
    assert r.report["hypotheses"][0]["service"] == "payments"
    assert r.tool_calls == 2
    # every assistant tool call must be answered by a tool message next turn
    assert llm.seen_contexts[1][-1] == "tool"
    print("happy path OK")


def test_malformed_args_recoverable():
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tc("query_logs", "{not json", "c1")]},
        {"role": "assistant", "content": None, "tool_calls": [tc("submit_root_cause", FINAL, "c2")]},
    ])
    r = run_llm_investigation("INC-1042", client=llm)
    assert r.error is None
    print("malformed-args recovery OK")


def test_prose_nudge_and_budget():
    # model rambles in prose; loop should nudge, then accept a final answer
    llm = ScriptedLLM([
        {"role": "assistant", "content": "I think it might be payments..."},
        {"role": "assistant", "content": None, "tool_calls": [tc("submit_root_cause", FINAL)]},
    ])
    r = run_llm_investigation("INC-1042", client=llm)
    assert r.error is None and r.iterations == 2
    print("prose nudge OK")


def test_invalid_report_rejected_then_budget_forces():
    bad = {"hypotheses": [{"rank": 1}]}  # missing service/cause_type
    llm = ScriptedLLM(
        [{"role": "assistant", "content": None, "tool_calls": [tc("submit_root_cause", bad)]}] * 13
        + [{"role": "assistant", "content": None, "tool_calls": [tc("submit_root_cause", FINAL)]}]
    )
    r = run_llm_investigation("INC-1042", client=llm)
    assert r.error is None and r.iterations == 14  # last turn is the forcing turn
    print("invalid-report rejection + budget forcing OK")


if __name__ == "__main__":
    test_happy_path()
    test_malformed_args_recoverable()
    test_prose_nudge_and_budget()
    test_invalid_report_rejected_then_budget_forces()
    print("all agent-loop tests passed")

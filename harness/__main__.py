"""AgentDesk CLI.

  python -m harness list
  python -m harness investigate INC-1042 [--mode baseline|llm]
  python -m harness eval [--mode baseline|llm]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import settings

console = Console()
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def _render_event(kind: str, data: dict) -> None:
    if kind == "tool":
        args = ", ".join(f"{k}={v}" for k, v in data["args"].items() if v not in ("", None))
        console.print(f"  [cyan]step {data['step']:>2}[/cyan] [bold]{data['tool']}[/bold]({args}) "
                      f"[dim]-> {data['summary']} ({data['ms']}ms)[/dim]")
    elif kind == "thought" and data.get("text"):
        console.print(f"  [yellow]agent:[/yellow] [dim]{data['text'][:200]}[/dim]")
    elif kind == "tool_error":
        console.print(f"  [red]malformed tool call ({data['tool']}), asking agent to retry[/red]")


def _render_report(report: dict) -> None:
    table = Table(title="Ranked root-cause hypotheses", show_lines=True)
    table.add_column("#", width=3)
    table.add_column("Service", style="bold")
    table.add_column("Cause")
    table.add_column("Conf", justify="right")
    table.add_column("Summary / evidence", max_width=80)
    for h in report["hypotheses"]:
        ev = "\n".join(f"[dim]· {e}[/dim]" for e in h.get("evidence", [])[:3])
        table.add_row(str(h["rank"]), h["service"], h["cause_type"],
                      f"{h.get('confidence', 0):.2f}", f"{h['summary']}\n{ev}")
    console.print(table)
    console.print(Panel(report.get("recommended_action", ""), title="Recommended action",
                        border_style="green"))


def _check_server() -> None:
    try:
        requests.get(f"{settings.toolserver_url}/healthz", timeout=3)
    except requests.RequestException:
        console.print(f"[red]Tool server not reachable at {settings.toolserver_url}.[/red] "
                      "Start it with: [bold]make server[/bold]")
        sys.exit(1)


def cmd_list(_args) -> None:
    _check_server()
    r = requests.get(f"{settings.toolserver_url}/api/incidents", timeout=5).json()
    table = Table(title="Incidents")
    for col in ("id", "severity", "service", "title"):
        table.add_column(col)
    for inc in r["incidents"]:
        table.add_row(inc["id"], inc["severity"], inc["service"], inc["title"])
    console.print(table)


def _investigate(incident_id: str, mode: str):
    _check_server()
    console.print(Panel(f"[bold]Investigating {incident_id}[/bold]  mode=[cyan]{mode}[/cyan]",
                        border_style="cyan"))
    if mode == "llm":
        from .agent import run_llm_investigation
        result = run_llm_investigation(incident_id, on_event=_render_event)

    if result.error:
        console.print(f"[red]{result.error}[/red]")
    if result.report:
        _render_report(result.report)
    console.print(f"[dim]{result.tool_calls} tool calls, {result.iterations} LLM iterations, "
                  f"{result.duration_s}s wall time[/dim]")
    return result


def cmd_eval(args) -> None:
    _check_server()
    truths = {}
    for d in sorted(SCENARIOS_DIR.iterdir()):
        tf = d / "truth.json"
        if tf.exists():
            truths[d.name] = json.loads(tf.read_text())

    table = Table(title=f"AgentDesk eval — mode={args.mode}", show_lines=True)
    for col in ("incident", "truth", "top-1 answer", "svc", "cause", "calls", "time"):
        table.add_column(col)
    svc_ok = cause_ok = 0
    for inc_id, truth in truths.items():
        console.rule(inc_id)
        result = _investigate(inc_id, args.mode)
        top = (result.report or {}).get("hypotheses", [{}])[0]
        s_ok = top.get("service") == truth["service"]
        c_ok = top.get("cause_type") == truth["cause_type"]
        svc_ok += s_ok
        cause_ok += c_ok
        table.add_row(inc_id,
                      f"{truth['service']} / {truth['cause_type']}",
                      f"{top.get('service', '-')} / {top.get('cause_type', '-')}",
                      "[green]OK[/green]" if s_ok else "[red]X[/red]",
                      "[green]OK[/green]" if c_ok else "[red]X[/red]",
                      str(result.tool_calls), f"{result.duration_s}s")
    console.print(table)
    n = len(truths)
    console.print(Panel(f"top-1 service accuracy: [bold]{svc_ok}/{n}[/bold]   "
                        f"top-1 cause accuracy: [bold]{cause_ok}/{n}[/bold]",
                        border_style="green"))


def main() -> None:
    p = argparse.ArgumentParser(prog="agentdesk")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list available incidents")

    pi = sub.add_parser("investigate", help="triage one incident")
    pi.add_argument("incident_id")
    pi.add_argument("--mode", choices=["baseline", "llm"], default="baseline")

    pe = sub.add_parser("eval", help="score the agent against ground truth on all incidents")
    pe.add_argument("--mode", choices=["baseline", "llm"], default="baseline")

    args = p.parse_args()
    if args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "investigate":
        _investigate(args.incident_id, args.mode)
    elif args.cmd == "eval":
        cmd_eval(args)


if __name__ == "__main__":
    main()

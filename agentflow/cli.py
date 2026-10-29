import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import Engine
from .tools import warehouse_tools


def demo_plan():
    return {
        "steps": [
            {"id": "inspect", "tool": "inventory", "args": {"sku": "sensor-kit"}, "needs": []},
            {
                "id": "price",
                "tool": "quote",
                "args": {"sku": {"$ref": "inspect.sku"}, "quantity": 3},
                "needs": ["inspect"],
            },
            {
                "id": "hold",
                "tool": "reserve",
                "args": {"sku": {"$ref": "price.sku"}, "quantity": {"$ref": "price.quantity"}},
                "needs": ["price"],
            },
        ]
    }


def demo():
    with TemporaryDirectory() as tmp:
        tools = warehouse_tools(str(Path(tmp) / "warehouse.sqlite"))
        path = str(Path(tmp) / "runs.sqlite")
        engine = Engine(path, tools)
        run = engine.start(demo_plan(), "demo-run")
        before = engine.advance(run)
        engine.close()
        # Reopen to prove the approval checkpoint survives a process boundary.
        engine = Engine(path, tools)
        pending = next(
            s for s in engine.snapshot(run)["steps"] if s["status"] == "waiting_approval"
        )
        engine.approve(run, pending["id"], pending["fingerprint"], "demo-reviewer")
        after = engine.advance(run)
        replay = engine.advance(run)
        result = {
            "project": "AgentFlow",
            "mode": "deterministic-synthetic",
            "checkpoint": before["status"],
            "final_status": after["status"],
            "replay_adds_events": len(replay["events"]) - len(after["events"]),
            "steps": after["steps"],
            "events": after["events"],
        }
        engine.close()
        return result


def run_cli():
    parser = argparse.ArgumentParser(description="Durable synthetic tool workflows")
    parser.add_argument("--db", default=".runs/agentflow.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_command = sub.add_parser("demo")
    demo_command.add_argument("--html", type=Path)
    start = sub.add_parser("start")
    start.add_argument("plan", type=Path)
    for name in ("advance", "status", "recover"):
        command = sub.add_parser(name)
        command.add_argument("run")
    approve = sub.add_parser("approve")
    for arg in ("run", "step", "fingerprint", "actor"):
        approve.add_argument(arg)
    reconcile = sub.add_parser("reconcile")
    for arg in ("run", "step", "receipt", "actor"):
        reconcile.add_argument(arg)
    plan = sub.add_parser(
        "plan", help="Propose a plan using an explicitly configured model endpoint"
    )
    plan.add_argument("goal")
    args = parser.parse_args()
    if args.command == "demo":
        result = demo()
        if args.html:
            from .report import render

            render(result, args.html)
        print(json.dumps(result, indent=2))
        return
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    tools = warehouse_tools(args.db + ".warehouse.sqlite")
    if args.command == "plan":
        from .engine import validate_plan
        from .provider import ChatPlanner

        proposal = ChatPlanner.from_env().json(
            "Return only JSON {steps:[{id,tool,args,needs}]}. Allowed tools: "
            + json.dumps(
                [{k: getattr(t, k) for k in ("name", "description", "effect")} for t in tools]
            )
            + '. Use {"$ref":"step.field"} for prior outputs and list that step in needs. No other keys.',
            args.goal,
        )
        validate_plan(proposal, {t.name: t for t in tools})
        print(json.dumps(proposal, indent=2))
        return
    engine = Engine(args.db, tools)
    try:
        if args.command == "start":
            result = {"run_id": engine.start(json.loads(args.plan.read_text()))}
        elif args.command == "approve":
            engine.approve(args.run, args.step, args.fingerprint, args.actor)
            result = engine.snapshot(args.run)
        elif args.command == "reconcile":
            engine.reconcile(
                args.run, args.step, json.loads(Path(args.receipt).read_text()), args.actor
            )
            result = engine.snapshot(args.run)
        elif args.command == "recover":
            engine.recover(args.run)
            result = engine.snapshot(args.run)
        elif args.command == "advance":
            result = engine.advance(args.run)
        else:
            result = engine.snapshot(args.run)
        print(json.dumps(result, indent=2))
    finally:
        engine.close()


def main():
    import sqlite3
    import sys

    try:
        run_cli()
    except (ValueError, KeyError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None

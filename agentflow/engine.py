"""A small durable DAG executor. Model proposals never carry execution authority."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class PlanError(ValueError):
    pass


class TransientError(RuntimeError):
    """A read may be retried; a write requires reconciliation."""


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    effect: bool
    execute: Callable[[dict, str], dict]
    validate: Callable[[dict], None]
    version: str = "1"


def references(value: Any):
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
                raise PlanError("A reference must contain exactly one string $ref")
            yield value["$ref"]
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def validate_plan(plan: dict, tools: dict[str, Tool]) -> None:
    if not isinstance(plan, dict) or set(plan) != {"steps"}:
        raise PlanError("Plan must contain only steps")
    steps = plan["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise PlanError("A plan needs 1..32 steps")
    ids = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"id", "tool", "args", "needs"}:
            raise PlanError("Step keys: id, tool, args, needs")
        sid = step["id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", sid) or sid in ids:
            raise PlanError("Step IDs must be unique lowercase identifiers")
        ids.add(sid)
        if not isinstance(step["tool"], str) or step["tool"] not in tools:
            raise PlanError("Unknown tool")
        if not isinstance(step["args"], dict) or not isinstance(step["needs"], list):
            raise PlanError("args must be an object; needs must be a list")
        if any(not isinstance(dep, str) for dep in step["needs"]):
            raise PlanError("Dependencies must be strings")
        if len(set(step["needs"])) != len(step["needs"]):
            raise PlanError("Duplicate dependency")
    done = set()
    while len(done) < len(steps):
        ready = [s for s in steps if s["id"] not in done and set(s["needs"]) <= done]
        if not ready:
            raise PlanError("Cycle, self-reference, or missing dependency")
        done.update(s["id"] for s in ready)
    for step in steps:
        for ref in references(step["args"]):
            if ref.split(".")[0] not in step["needs"]:
                raise PlanError("Every referenced step must be an explicit dependency")
    if len(encode(plan)) > 64_000:
        raise PlanError("Plan is too large")


def resolve(value: Any, outputs: dict) -> Any:
    if isinstance(value, dict):
        if "$ref" in value:
            parts = value["$ref"].split(".")
            result = outputs[parts[0]]
            for key in parts[1:]:
                if not isinstance(result, dict) or key not in result:
                    raise PlanError("Reference does not point to an output field")
                result = result[key]
            return result
        return {k: resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs) for v in value]
    return value


class Engine:
    """Single-controller executor. SQLite arbitrates claims across processes.

    A claimed step remains running if its process dies. Recovery is explicit;
    effectful calls become uncertain and are never silently retried.
    """

    def __init__(self, path: str, tools: list[Tool], max_attempts: int = 3):
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be 1..10")
        self.max_attempts = max_attempts
        self.tools = {t.name: t for t in tools}
        if len(self.tools) != len(tools):
            raise ValueError("Duplicate tool name")
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, plan TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS steps(
          run_id TEXT, id TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          output TEXT, fingerprint TEXT, approval TEXT, error TEXT,
          PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step_id TEXT,
          kind TEXT, detail TEXT);
        """)

    def close(self):
        self.db.close()

    def _event(self, run: str, step: str, kind: str, detail: dict):
        self.db.execute(
            "INSERT INTO events(run_id,step_id,kind,detail) VALUES(?,?,?,?)",
            (run, step, kind, encode(detail)),
        )

    def _plan(self, run: str) -> dict:
        row = self.db.execute("SELECT plan FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run}")
        return json.loads(row[0])

    def start(self, plan: dict, run_id: str | None = None) -> str:
        validate_plan(plan, self.tools)
        run = run_id or str(uuid.uuid4())
        # Tool versions and effect policies are pinned into the durable plan.
        pinned = {
            **plan,
            "tools": {
                s["tool"]: {
                    "version": self.tools[s["tool"]].version,
                    "effect": self.tools[s["tool"]].effect,
                }
                for s in plan["steps"]
            },
            "max_attempts": self.max_attempts,
        }
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO runs VALUES(?,?)", (run, encode(pinned)))
            for s in plan["steps"]:
                self.db.execute(
                    "INSERT INTO steps(run_id,id,status) VALUES(?,?,?)", (run, s["id"], "pending")
                )
            self._event(run, "", "created", {"plan_hash": digest(pinned)})
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return run

    def snapshot(self, run: str) -> dict:
        plan = self._plan(run)
        rows = [
            dict(r)
            for r in self.db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY rowid", (run,))
        ]
        for row in rows:
            row.pop("run_id")
            row["output"] = json.loads(row["output"]) if row["output"] else None
        statuses = {r["status"] for r in rows}
        status = (
            "succeeded"
            if statuses == {"succeeded"}
            else "uncertain"
            if "uncertain" in statuses
            else "failed"
            if "failed" in statuses
            else "running"
            if "running" in statuses
            else "waiting_approval"
            if "waiting_approval" in statuses
            else "pending"
        )
        events = [
            {**dict(r), "detail": json.loads(r["detail"])}
            for r in self.db.execute(
                "SELECT seq,step_id,kind,detail FROM events WHERE run_id=? ORDER BY seq", (run,)
            )
        ]
        return {"run_id": run, "status": status, "plan": plan, "steps": rows, "events": events}

    def approve(self, run: str, step: str, fingerprint: str, actor: str):
        if not actor.strip():
            raise ValueError("An approval needs a reviewer label")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM steps WHERE run_id=? AND id=?", (run, step)
            ).fetchone()
            if (
                row is None
                or row["status"] != "waiting_approval"
                or row["fingerprint"] != fingerprint
            ):
                raise ValueError("Approval does not match the pending action")
            self.db.execute(
                "UPDATE steps SET status='pending',approval=? WHERE run_id=? AND id=?",
                (fingerprint, run, step),
            )
            self._event(run, step, "approved", {"actor": actor, "fingerprint": fingerprint})
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def recover(self, run: str):
        """Call only after the previous controller has stopped."""
        plan = self._plan(run)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for step in plan["steps"]:
                row = self.db.execute(
                    "SELECT * FROM steps WHERE run_id=? AND id=?", (run, step["id"])
                ).fetchone()
                if row["status"] == "running":
                    effect = plan["tools"][step["tool"]]["effect"]
                    status = (
                        "uncertain"
                        if effect
                        else ("pending" if row["attempts"] < plan["max_attempts"] else "failed")
                    )
                    self.db.execute(
                        "UPDATE steps SET status=? WHERE run_id=? AND id=?",
                        (status, run, step["id"]),
                    )
                    self._event(run, step["id"], "recovered", {"status": status})
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def reconcile(self, run: str, step: str, output: dict, actor: str):
        """Record a manually verified receipt. No effect is executed here."""
        if not actor.strip() or not isinstance(output, dict):
            raise ValueError("Reconciliation needs a reviewer and object receipt")
        serialized = encode(output)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE steps SET status='succeeded',output=? WHERE run_id=? AND id=? AND status='uncertain'",
                (serialized, run, step),
            )
            if cur.rowcount != 1:
                raise ValueError("Only an uncertain step can be reconciled")
            self._event(run, step, "reconciled", {"actor": actor, "receipt": output})
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def advance(self, run: str, budget: int = 64) -> dict:
        if not 1 <= budget <= 1000:
            raise ValueError("budget must be 1..1000")
        plan = self._plan(run)
        for name, policy in plan["tools"].items():
            tool = self.tools.get(name)
            if tool is None or {"version": tool.version, "effect": tool.effect} != policy:
                raise PlanError("Tool registry changed since planning")
        for _ in range(budget):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                rows = {
                    r["id"]: dict(r)
                    for r in self.db.execute("SELECT * FROM steps WHERE run_id=?", (run,))
                }
                if any(r["status"] in {"failed", "uncertain", "running"} for r in rows.values()):
                    self.db.commit()
                    break
                ready = [
                    s
                    for s in plan["steps"]
                    if rows[s["id"]]["status"] == "pending"
                    and all(rows[n]["status"] == "succeeded" for n in s["needs"])
                ]
                if not ready:
                    self.db.commit()
                    break
                step = ready[0]
                sid, tool = step["id"], self.tools[step["tool"]]
                try:
                    args = resolve(
                        step["args"],
                        {n: json.loads(r["output"]) for n, r in rows.items() if r["output"]},
                    )
                    tool.validate(args)
                except (ValueError, KeyError, TypeError) as exc:
                    self.db.execute(
                        "UPDATE steps SET status='failed',error=? WHERE run_id=? AND id=?",
                        (type(exc).__name__, run, sid),
                    )
                    self._event(run, sid, "invalid_arguments", {"error": type(exc).__name__})
                    self.db.commit()
                    break
                fp = digest(
                    {
                        "plan": digest(plan),
                        "step": sid,
                        "args": args,
                        "tool": tool.name,
                        "version": tool.version,
                    }
                )
                if tool.effect and rows[sid]["approval"] != fp:
                    self.db.execute(
                        "UPDATE steps SET status='waiting_approval',fingerprint=? WHERE run_id=? AND id=?",
                        (fp, run, sid),
                    )
                    self._event(
                        run,
                        sid,
                        "approval_requested",
                        {"fingerprint": fp, "tool": tool.name, "args": args},
                    )
                    self.db.commit()
                    continue
                attempts = rows[sid]["attempts"] + 1
                self.db.execute(
                    "UPDATE steps SET status='running',attempts=?,fingerprint=? WHERE run_id=? AND id=?",
                    (attempts, fp, run, sid),
                )
                self._event(run, sid, "started", {"attempt": attempts})
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            # A durable claim exists before a tool is called. Never hold a DB lock over I/O.
            try:
                output = tool.execute(args, f"{run}:{sid}:{fp}")
                if not isinstance(output, dict):
                    raise ValueError("Tool output must be a JSON object")
                serialized = encode(output)
                status, error = "succeeded", None
            except Exception as exc:
                serialized, error = None, type(exc).__name__
                status = (
                    "uncertain"
                    if tool.effect
                    else "pending"
                    if isinstance(exc, TransientError) and attempts < plan["max_attempts"]
                    else "failed"
                )
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "UPDATE steps SET status=?,output=?,error=? WHERE run_id=? AND id=?",
                    (status, serialized, error, run, sid),
                )
                self._event(run, sid, status, {"attempt": attempts, "error": error})
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return self.snapshot(run)

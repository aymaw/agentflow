import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agentflow.cli import demo, demo_plan
from agentflow.engine import Engine, PlanError, Tool, TransientError
from agentflow.tools import warehouse_tools


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "runs.sqlite")
        self.warehouse = self.path + ".warehouse"
        self.tools = warehouse_tools(self.warehouse)
        self.engine = Engine(self.path, self.tools)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_approval_restart_and_replay(self):
        run = self.engine.start(demo_plan())
        snap = self.engine.advance(run)
        self.assertEqual(snap["status"], "waiting_approval")
        with closing(sqlite3.connect(self.warehouse)) as db:
            self.assertEqual(
                db.execute("SELECT available FROM stock WHERE sku='sensor-kit'").fetchone()[0], 12
            )
        self.engine.close()
        self.engine = Engine(self.path, self.tools)
        step = self.engine.snapshot(run)["steps"][-1]
        self.engine.approve(run, "hold", step["fingerprint"], "test-reviewer")
        done = self.engine.advance(run)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done, self.engine.advance(run))
        with closing(sqlite3.connect(self.warehouse)) as db:
            self.assertEqual(
                db.execute("SELECT available FROM stock WHERE sku='sensor-kit'").fetchone()[0], 9
            )
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)

    def test_rejects_stale_approval(self):
        run = self.engine.start(demo_plan())
        self.engine.advance(run)
        with self.assertRaises(ValueError):
            self.engine.approve(run, "hold", "wrong-fingerprint", "reviewer")

    def test_cycle_missing_unknown_and_bad_ref(self):
        for mutate in ("cycle", "missing", "tool", "ref", "duplicate"):
            plan = demo_plan()
            if mutate == "cycle":
                plan["steps"][0]["needs"] = ["hold"]
            if mutate == "missing":
                plan["steps"][0]["needs"] = ["absent"]
            if mutate == "tool":
                plan["steps"][0]["tool"] = "shell"
            if mutate == "ref":
                plan["steps"][1]["args"]["sku"] = {"$ref": "hold.sku"}
            if mutate == "duplicate":
                plan["steps"][1]["id"] = "inspect"
            with self.subTest(mutate=mutate), self.assertRaises(PlanError):
                self.engine.start(plan)

    def test_invalid_resolved_args_block_effect(self):
        plan = demo_plan()
        plan["steps"][1]["args"]["quantity"] = True
        run = self.engine.start(plan)
        snap = self.engine.advance(run)
        self.assertEqual(snap["status"], "failed")
        self.assertEqual(snap["steps"][-1]["attempts"], 0)

    def test_read_retry_is_bounded(self):
        calls = []

        def flaky(args, key):
            calls.append(key)
            raise TransientError("temporary")

        tool = Tool("flaky", "read", False, flaky, lambda _: None)
        engine = Engine(":memory:", [tool], max_attempts=2)
        self.addCleanup(engine.close)
        run = engine.start({"steps": [{"id": "read", "tool": "flaky", "args": {}, "needs": []}]})
        self.assertEqual(engine.advance(run)["status"], "failed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_write_exception_becomes_uncertain(self):
        calls = []

        def unsafe(args, key):
            calls.append(key)
            raise TransientError("unknown outcome")

        engine = Engine(":memory:", [Tool("write", "effect", True, unsafe, lambda _: None)])
        self.addCleanup(engine.close)
        run = engine.start({"steps": [{"id": "effect", "tool": "write", "args": {}, "needs": []}]})
        step = engine.advance(run)["steps"][0]
        engine.approve(run, "effect", step["fingerprint"], "reviewer")
        self.assertEqual(engine.advance(run)["status"], "uncertain")
        engine.advance(run)
        self.assertEqual(len(calls), 1)
        engine.reconcile(run, "effect", {"verified_receipt": "synthetic"}, "reviewer")
        self.assertEqual(engine.advance(run)["status"], "succeeded")

    def test_crash_recovery_preserves_effect_uncertainty(self):
        run = self.engine.start(demo_plan())
        snap = self.engine.advance(run)
        step = snap["steps"][-1]
        self.engine.approve(run, "hold", step["fingerprint"], "reviewer")
        self.engine.db.execute(
            "UPDATE steps SET status='running',attempts=1 WHERE run_id=? AND id='hold'", (run,)
        )
        self.engine.recover(run)
        self.assertEqual(self.engine.snapshot(run)["status"], "uncertain")
        self.assertEqual(self.engine.advance(run)["steps"][-1]["attempts"], 1)

    def test_registry_policy_cannot_change(self):
        run = self.engine.start(demo_plan())
        original = self.engine.tools["reserve"]
        self.engine.tools["reserve"] = Tool(
            original.name, original.description, False, original.execute, original.validate
        )
        with self.assertRaises(PlanError):
            self.engine.advance(run)

    def test_two_controllers_do_not_repeat_running_step(self):
        run = self.engine.start(demo_plan())
        self.engine.db.execute(
            "UPDATE steps SET status='running',attempts=1 WHERE run_id=? AND id='inspect'", (run,)
        )
        # Close both controllers before tearDown removes the database on Windows.
        with closing(Engine(self.path, self.tools)) as second:
            self.assertEqual(second.advance(run)["status"], "running")
            self.assertEqual(second.snapshot(run)["steps"][0]["attempts"], 1)

    def test_idempotent_tool_receipt_and_conflict(self):
        reserve = next(t for t in self.tools if t.name == "reserve")
        args = {"sku": "sensor-kit", "quantity": 2}
        self.assertEqual(reserve.execute(args, "same"), reserve.execute(args, "same"))
        with self.assertRaises(ValueError):
            reserve.execute({**args, "quantity": 3}, "same")

    def test_unknown_run_and_budget(self):
        with self.assertRaises(KeyError):
            self.engine.snapshot("missing")
        run = self.engine.start(demo_plan())
        with self.assertRaises(ValueError):
            self.engine.advance(run, budget=0)
        self.assertEqual(self.engine.advance(run, budget=1)["steps"][0]["status"], "succeeded")

    def test_demo_evidence(self):
        result = demo()
        self.assertEqual(result["final_status"], "succeeded")
        self.assertEqual(result["replay_adds_events"], 0)


if __name__ == "__main__":
    unittest.main()

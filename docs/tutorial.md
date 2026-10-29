# AgentFlow walkthrough

Start with `python3 -m agentflow demo --html .runs/demo.html`. This creates a
fresh synthetic scenario, executes it, and prints the actual result. Open the
HTML artifact to inspect evidence and export JSON.

## Work with the CLI

```sh
python3 -m agentflow start examples/workflow.json
# Copy the printed run_id for these commands.
python3 -m agentflow advance RUN_ID
python3 -m agentflow status RUN_ID
# Copy the pending fingerprint shown in status.
python3 -m agentflow approve RUN_ID hold FINGERPRINT local-reviewer
python3 -m agentflow advance RUN_ID
```

`recover RUN_ID` is an operator action for a stopped controller. Never call it
while a controller is still executing. Interrupted reads become pending if retry
capacity remains; effects become uncertain. To reconcile an uncertain write,
inspect the tool's ledger independently, save its verified receipt as JSON, then
run `reconcile RUN_ID STEP receipt.json REVIEWER`. This records the supplied
receipt without executing or retrying a tool.

## Reproduce and inspect

```sh
python3 -m agentflow demo > demo.json
python3 -m unittest discover -s tests -v
```

`examples/demo.json` was captured from this implementation. Identifiers and
labels are synthetic. AgentBench timings vary with hardware and scheduling;
suite identity and policy outcomes remain reproducible.

## Expected failures

Invalid domain operations print a JSON error to stderr and exit 2. AgentBench's
regression gate exits 1 when a scenario regresses. Tests exercise invalid actions
and verify rejected or rolled-back state changes. No test needs external
credentials or a live model.

# Scope and evidence

AgentFlow separates a planner’s proposal from the authority to execute it. A plan becomes a validated DAG; reads can retry, writes pause for review, and every result survives a process restart in SQLite.

Source, synthetic fixtures, tests, installed commands, and demo artifacts are the
evidence. JSON and HTML examples record completed runs; they are not a hosted
service. Provider tests use mocked responses and do not establish the quality of
any specific running model.

## Limits

This is a synchronous, single-controller reference runtime. It has no
distributed queue, leases, cancellation, automatic backoff, or parallel scheduler.
SQLite is not a distributed transaction with arbitrary external services. Generic
tool writes do **not** have an exactly-once guarantee. Manual reconciliation
trusts an operator's supplied receipt. Reviewer labels are not authenticated
identities. The default planner is scripted; the opt-in model planner is a
separate, untrusted proposal source.

No production scale, vendor connectivity, clinical safety, business outcomes,
or model superiority is claimed. CI configuration and locally passing checks
are separate from a verified remote CI run.

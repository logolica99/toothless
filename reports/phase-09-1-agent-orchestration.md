# Phase 09.1 — Persistent agent orchestration

Status: NOT IMPLEMENTED / BLOCKED BY REQUIRED RUNTIME INSPECTION, LIVE SUPABASE FOUNDATION, AND OWNER POLICY APPROVAL. No task or model request was started for this phase.

Phase 09.0.2 implements and deploys username/password authentication routes, owner bootstrap, host-only DSN configuration, and Supabase auth-schema support. No live Supabase database is connected, so this does not establish the Phase 09.0 live acceptance gate. Persistent OpenCode orchestration remains unimplemented.

## Findings

- Existing `OpenCodeTaskRunner.execute()` is a bounded one-task/one-execution interface. It constructs a request from TaskStore, invokes the worker's fixed guest runner, and returns a terminal result. It is appropriate for existing Telegram task execution but does not establish a durable multi-step web conversation/session that can resume after owner approval.
- Repository search found no supported OpenCode session-resume contract for the installed guest. The Ubuntu VM was not started or queried, so the installed OpenCode version, CLI help, service API, persistent-session semantics, and true incremental streaming capabilities are UNKNOWN. No resume API or token streaming was invented.
- Host TaskStore, execution ledger, Telegram approval bindings, and worker cancellation remain authoritative for existing operations. The new Supabase schema provides run/event/approval record shapes only; it is not wired to worker dispatch and cannot authorize it.
- Web automatic start is hard-disabled (`automatic_low_risk_start: false`). Web-saved user messages get an explicit durable `stored_no_execution` status plus a system-visible note that no work started. A normal message cannot dispatch a worker, create an external resource, or weaken Telegram `/run` approval.

## Acceptance

- Actual multi-step OpenCode session through the web app: NOT RUN.
- Actual protected Ubuntu code creation/execution and verified CSV: NOT RUN.
- Genuine OpenCode incremental output, durable SSE replay, and reconnect: NOT RUN.
- Owner approval and same-run continuation: NOT RUN.
- Stop/cancel/reconcile/restart recovery: NOT RUN.
- Mock external-operation approval and replay tests: NOT RUN.
- No historical task was resubmitted; no VM, controller, worker, Tailscale route, or production service was changed.

## Gate to continue

Before implementing or enabling Phase 09.1, inspect the installed guest OpenCode version and its documented/actual session API through the protected worker, then design a durable state transition that records a single host execution intent before dispatch and reconciles uncertain outcomes without replay. The web policy must be reviewed and explicitly approved by the owner before low-risk automatic dispatch is enabled. External writes, deployments, account/resource creation, destructive work, privileged operations, and tool-scope expansion continue to require exact single-use approval.

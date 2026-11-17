# Phase 02.1 — Deterministic worker execution interface

Date: 2026-11-16 (America/Los_Angeles)

Status: IMPLEMENTED AND VERIFIED WITH THE FAKE BACKEND. This is a transport-neutral contract only. No SSH transport, real worker command execution, model tool registration, or autonomous execution is enabled. The interface is not yet ready for a controlled real-worker test; implement and review authenticated SSH transport first.

## Implementation

Added `nanobot.worker.interface` with typed `ExecuteRequest`, `ExecutionSnapshot`, execution status, file entry/stat structures, a `WorkerBackend` protocol, and `WorkerInterface`. The interface supports execute, poll, terminate, binary read/write, direct-child listing, and stat operations. Execution results carry an ID, status, exit code, bounded byte stdout/stderr, truncation flags, and start/end timestamps. Structured failures expose stable error codes and a serializable `as_dict()` result, including an explicit worker-unavailable error.

Execution IDs are generated when omitted and may also be supplied by the caller. Duplicate IDs are rejected before a second backend dispatch. IDs are tracked in process memory only at this checkpoint; persistence and reconnect reconciliation belong to the later durable task layer. If a dispatch raises after ID reservation, callers must poll/reconcile that ID and must not blindly resubmit the command.

Commands must be non-empty UTF-8 text no larger than 32 KiB; timeout must be greater than zero and at most 3600 seconds. Worker paths must be relative POSIX paths without `..`, absolute roots, NULs, or backslashes. File content is binary-safe and capped at 1 MiB by default. Each stdout/stderr stream is capped at 64 KiB by default; truncated streams are marked. These are controller-side contract limits, not a substitute for bounded streaming in a future SSH backend.

`FakeWorkerBackend` uses scripted command plans and an in-memory file tree. It does not parse or run command strings, spawn a process, access the host filesystem, or use SSH. Timeouts and cancellation are modeled as deterministic terminal states. The interface is not imported by the model tool registry and does not alter model reasoning or permissions. No fallback execution path exists.

## Verification results

| Test | Result | Evidence |
| --- | --- | --- |
| Successful command response | PASS | Typed success, exit code 0, output bytes, working directory, and execution ID asserted. |
| Nonzero exit | PASS | Exit code 1 and stderr returned in a failed execution snapshot rather than raised as a transport error. |
| Large stdout/stderr | PASS | Both streams clipped to configured limits and truncation flags set. |
| Timeout | PASS | Scripted runtime beyond request timeout returns `timed_out` with no exit code and a terminal timestamp. |
| Cancellation | PASS | Running execution polls as running, terminate returns cancelled, and later poll observes terminal state. |
| Malformed paths and requests | PASS | Absolute/traversal/backslash/NUL/empty paths, blank command, and zero timeout produce structured `invalid_request` failures before backend dispatch. |
| Duplicate execution ID | PASS | Second submission returns `duplicate_execution_id`; fake backend records exactly one dispatch. |
| Worker unavailable | PASS | Backend raises typed `worker_unavailable` with serializable error fields. |
| No host execution fallback | PASS | Unavailable-backend test submits a `touch <temporary-host-marker>` command and confirms no marker exists and fake backend accepted no dispatch. No subprocess/local execution module is present in `nanobot/worker/`. |
| File operations | PASS | Binary write/read round-trip, directory listing, and file/directory stat verified against fake-only memory. |
| Focused tests | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m pytest -q tests/personal_agent/test_worker_interface.py` — 15 passed, exit 0. |
| Ruff | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync ruff check nanobot/worker tests/personal_agent/test_worker_interface.py` — all checks passed, exit 0. |
| BasedPyright | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync basedpyright nanobot/worker tests/personal_agent/test_worker_interface.py` — 0 errors, warnings, or notes, exit 0. |
| Import boundaries | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m scripts.check_import_boundaries` — 13 rules, 218 files, exit 0. |
| Controlled real-worker execution | NOT RUN | No real transport exists yet, by design. |

## Security boundary and next step

The command field deliberately represents guest shell syntax for a future worker transport, but this fake backend treats it as inert data. Do not register these operations as model tools or connect them to OpenCode yet. The next implementation checkpoint may add a key-only SSH transport with pinned guest host keys, structured request encoding, process-group cancellation, bounded streaming, and fixed guest workspace paths. It must have no host subprocess fallback and must fail with `worker_unavailable` when the guest cannot be reached. Only after that backend is reviewed should a controlled real-worker test be considered. Phase 02.2 was not started.

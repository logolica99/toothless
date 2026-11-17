# Phase 02.3 — Persistent worker execution

Date: 2026-11-16 (America/Los_Angeles)

Status: VERIFIED FOR CONTROLLED OPERATOR-DRIVEN EXECUTION AND RECOVERY. Host execution intent, acceptance, snapshots, cancellation requests, and errors persist in SQLite. The real Ubuntu worker was used for process restart, accepted SSH interruption, cancellation, reset recovery, and stopped-worker tests. OpenCode worker tools remain unregistered and unattended execution remains disabled.

## Implementation

`SqliteExecutionStore` stores execution records in the host runtime `worker/executions.sqlite3` location, or at an explicitly supplied absolute path. The worker runtime directory is mode `0700` and the database file is mode `0600`. No host directory is mounted into the VM. Each record has the existing execution ID, working directory, timeout, dispatch state, execution status, stdout/stderr and truncation flags, exit code, created/accepted/started/finished/cancellation/update timestamps, and structured error code/message. It does not store the command text, SSH identity, host key, provider credentials, or other authentication secrets.

`WorkerInterface.execute` inserts and commits an `intent` row before calling the backend. `create_persistent_worker` wires the host store to the existing SSH backend. The guest emits its acceptance line only after it has handled the request and, for execute, started the guest runner. The backend invokes the host acceptance callback before waiting for the final response. If the SSH channel ends after acceptance, the host records `uncertain` and the caller must reconcile by ID; the command is never automatically submitted again. Known pre-dispatch SSH failures are recorded as `not_accepted` and return `worker_unavailable`.

`poll` and `reconcile` reconnect using the same strict pinned-key SSH backend. Terminal host records can be returned without guest access. Running or uncertain records are queried by the original ID. If a worker reset removed the guest journal, the host keeps the result `unknown`, records `execution_not_found`, and does not infer success. A persisted cancellation request is retried as an idempotent terminate operation after reconnect, never as a new command.

The worker helper is restored through a documented provisioning step rather than a baseline replacement. After reset, start only with `ops/worker/start.sh`, wait for the Softnet acknowledgement, and run `ops/worker/install-execution-agent.sh`. The installer pins the current guest host key through Tart's trusted guest-agent channel and installs the root-owned helper over key-only SSH. No baseline was changed in this checkpoint.

The SQLite record stores command output for result recovery. Command text and authentication credentials are omitted; stdout/stderr remain guest-produced user data and should not be used to print secrets. The database is host-only and permission restricted. Guest journal contents remain operational data, not security authority.

## Real-world verification

The SQLite test database was `/private/tmp/toothless-phase-02-03-executions.sqlite3`, outside the guest workspace and mounts. It was created with mode `0600`. The tests used fresh UUID execution IDs except for the named reset-history ID. All observed timestamps below are UTC.

| Test | Execution ID | Observed state transitions and result |
| --- | --- | --- |
| A — normal execution and guest-only file | `phase-02-03-0da192b4ab2247bab8b902667c0b167b` | `intent → accepted/running → succeeded`, exit 0, stdout `PERSISTED_OK\n`; the guest-only file contained the expected payload and was removed. The host record returned the same ID, accepted timestamp, and result. |
| B — controller process restart | `phase-02-03-restart-759dbd2f03bf42928e6a5310e96e6b0f` | Controller process 1 returned `running` and exited. A separate Python controller process reopened the same SQLite file, reconciled the same ID, and returned `succeeded`, exit 0, stdout `CONTROLLER_RESTART_DONE\n`. The guest marker contained exactly one `x`; the command was not submitted again. Guest start: `2026-11-17T04:21:45.987969+00:00`; finish: `2026-11-17T04:21:50.010157+00:00`. |
| C — SSH interruption after guest acceptance | `phase-02-03-interruption-5afade8eaad94ca980ed7ed3104b8bec` | The live SSH client was interrupted at the backend's post-acceptance hook. The first controller received `execution_unknown`; the host row was `uncertain` with `accepted_at`. A new connection reconciled the same ID to `succeeded`, exit 0, stdout `INTERRUPTION_DONE\n`; the guest marker contained exactly one `x`. Guest start: `2026-11-17T04:21:50.753653+00:00`; finish: `2026-11-17T04:21:52.771026+00:00`. |
| D — cancellation | `342654eb832d4e03abb3f20a296be11b` | Host row moved through `accepted/running`, then stored `cancel_requested_at` and `cancelled`. A guest-side `kill -0` check for the command PID failed as expected and returned `TERMINATED\n`. Cancellation time: `2026-11-17T04:21:53.734098+00:00`; terminal time: `2026-11-17T04:21:53.759416+00:00`. |
| E — reset removes guest journal | `phase-02-03-reset-missing-history` | Before reset, host row was `accepted/running` for bounded `sleep 300`. `printf 'RESET-WORKER\n' | ops/worker/reset.sh` reset only the fixed worker from the existing base; neither baseline was replaced. After protected startup and helper provisioning, reconciliation returned `unknown`, exit code null, dispatch state `history_missing`, error `execution_not_found` / `Guest execution history is missing`. It was not marked successful. A post-reset `printf 'AFTER_RESET_READY\n'` succeeded as `phase-02-03-after-reset-ready`, proving the documented provisioning step restored the helper. |
| F — stopped worker and host isolation | `phase-02-03-stopped-ae92466f54f24cc79216b50a0e848a3b` | With the VM stopped, execution returned `worker_unavailable`; the host record was `not_accepted` with error `worker_unavailable`. The submitted host-marker command did not create a file on macOS. The worker remained stopped. |

The Softnet launcher acknowledged its existing host-gateway policy on each protected startup and reported the DNS relay active at the current dynamically assigned gateway on port 55353. The post-reset helper install did not change networking policy. Phase 01's owned endpoint and direct port 53 test results remain the cited network-boundary evidence; those endpoint fixtures were not repeated here.

## Regression results

| Command | Result |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_execution_store.py tests/personal_agent/test_worker_interface.py` | PASS; 20 passed. |
| Protected-worker command below | PASS; 4 passed, 3 deselected. |
| Post-reset protected provisioning and direct Python adapter check | PASS; helper installed, missing-history result was unknown, post-reset READY command succeeded. |
| Stopped-worker command below | PASS; 1 passed, 6 deselected; host marker absent. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync ruff check nanobot/worker tests/personal_agent/test_worker_execution_store.py tests/personal_agent/test_worker_ssh_integration.py ops/worker/guest_worker_agent.py` | PASS; all checks passed. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync basedpyright nanobot/worker tests/personal_agent/test_worker_execution_store.py tests/personal_agent/test_worker_ssh_integration.py ops/worker/guest_worker_agent.py` | PASS; 0 errors, warnings, or notes. |
| `ops/worker/test-static.sh` | PASS; static worker safety checks passed. |

The exact test driver environment used the pinned host-key file `/private/tmp/toothless-phase-02-03-known-hosts`, identity `/Users/jubaerjami/.ssh/id_ed25519`, SSH user `admin`, and the host-only SQLite path above. The key pin and test database were removed after verification. `ops/worker/stop.sh` completed and `tart get toothless-worker` reported `stopped`.

Protected-worker integration command:

```bash
worker_ip="$(awk 'NR == 1 {print $1}' /private/tmp/toothless-phase-02-03-known-hosts)"
TOOTHLESS_WORKER_SSH_TEST_MODE=running TOOTHLESS_WORKER_HOST="$worker_ip" TOOTHLESS_WORKER_USER=admin TOOTHLESS_WORKER_IDENTITY_FILE="$HOME/.ssh/id_ed25519" TOOTHLESS_WORKER_KNOWN_HOSTS_FILE=/private/tmp/toothless-phase-02-03-known-hosts TOOTHLESS_EXECUTION_DB=/private/tmp/toothless-phase-02-03-executions.sqlite3 UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_ssh_integration.py -k 'persistent_normal or controller_process_restart or accepted_ssh_interruption or persistent_cancel'
```

Stopped-worker integration command:

```bash
worker_ip="$(awk 'NR == 1 {print $1}' /private/tmp/toothless-phase-02-03-known-hosts)"
TOOTHLESS_WORKER_SSH_TEST_MODE=stopped TOOTHLESS_WORKER_HOST="$worker_ip" TOOTHLESS_WORKER_USER=admin TOOTHLESS_WORKER_IDENTITY_FILE="$HOME/.ssh/id_ed25519" TOOTHLESS_WORKER_KNOWN_HOSTS_FILE=/private/tmp/toothless-phase-02-03-known-hosts TOOTHLESS_EXECUTION_DB=/private/tmp/toothless-phase-02-03-executions.sqlite3 TOOTHLESS_HOST_MARKER_PATH=/private/tmp/toothless-phase-02-03-host-marker UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_ssh_integration.py -k stopped_worker_is_unavailable
```

## Limits and next step

Host records survive controller restarts, but SQLite does not coordinate concurrent controllers beyond unique IDs and SQLite write transactions; only one active controller should dispatch work at a time. Guest output is stored as user data. The guest journal can be modified by guest `admin`; host records are authoritative for what the controller observed, but the guest journal is not trusted policy. A guest reset while an operation is active makes its final result irrecoverable; the correct host result is `unknown`. The helper must be restored by the documented provisioning command after every reset.

Phase 02.3 is complete for operator-driven remote execution recovery. Phase 03 has not started. Do not register these worker operations as OpenCode tools or enable unattended execution as part of this checkpoint.

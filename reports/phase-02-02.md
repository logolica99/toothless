# Phase 02.2 — Real worker execution

Date: 2026-11-16 (America/Los_Angeles)

Status: VERIFIED FOR CONTROLLED OPERATOR-DRIVEN REAL WORKER USE. The real Ubuntu VM returned the expected READY response through `WorkerInterface`; lifecycle, output bounds, file transfer, guest identity, and stopped-worker behavior passed. No model tool registration or autonomous execution was enabled.

## Implementation

`nanobot.worker.ssh_backend.SSHWorkerBackend` implements the existing `WorkerBackend` protocol with OpenSSH. It requires a literal IPv4 address, an Ed25519 identity file, and an explicit known-hosts file. OpenSSH uses strict host-key checking, public-key-only authentication, `IdentitiesOnly=yes`, `IdentityAgent=none`, no agent or port forwarding, no proxy/jump configuration, no TTY, and no user SSH configuration. The guest host key was read through Tart's trusted guest-agent channel and pinned for the current DHCP address. After a protected restart changed the guest address, strict verification refused the stale address/key entry; refreshing the pin through the same trusted channel restored the connection.

All operation parameters are JSON on SSH stdin. The SSH remote command is the constant `sudo -n /usr/local/libexec/toothless/worker-agent`; no command, path, or JSON value is interpolated into it. The root-owned guest helper drops to `admin`, validates operations, confines file operations to `/home/admin/workspace`, and runs commands as `/bin/sh -c` inside that workspace. OpenCode credentials and host filesystem mounts are not copied or exposed.

The helper returns an acceptance line before dispatch. If SSH fails before that line, the backend returns `worker_unavailable`. If it disconnects after accepting an execute request, the backend returns `execution_unknown` with the execution ID and tells the caller to poll rather than retry. There is no local command fallback and no automatic retry. Guest execution uses a new process group, bounded 64-KiB stdout and stderr buffers, timeout and cancellation signals, and a two-second post-termination drain bound. File transfers are binary-safe and capped at 1 MiB.

The guest helper was installed in the current worker with `ops/worker/install-execution-agent.sh`; it is not sealed into the baseline. A worker reset removes it, so the install script must be run after reset before using the SSH backend. The worker was left stopped.

## Files changed

- `nanobot/worker/ssh_backend.py` and `nanobot/worker/interface.py` implement strict SSH transport, acceptance-aware failure classification, overall request timeouts, fixed system SSH selection, and shared execution ID validation.
- `nanobot/worker/__init__.py` exports the backend and configuration type.
- `ops/worker/guest_worker_agent.py` implements the bounded JSON protocol and guest-side execution/file operations; `ops/worker/install-execution-agent.sh` installs it through protected key-only SSH.
- `tests/personal_agent/test_worker_ssh_integration.py` covers live worker lifecycle, file, network, and outage checks.
- `docs/architecture.md`, `docs/features.md`, `docs/status.md`, and this report record the Phase 02.2 design and evidence.

## Real READY execution

The exact command submitted through `WorkerInterface` was `printf 'TOOTHLESS_WORKER_READY\n'`.

| Field | Result |
| --- | --- |
| Execution ID | `7a32e59bda9a4cb7b78ab5ca4c49efcc` |
| Status | `succeeded` |
| Exit code | `0` |
| stdout | `TOOTHLESS_WORKER_READY\n` |
| stderr | empty |
| Started | `2026-11-17T03:55:09.399045+00:00` |
| Completed | `2026-11-17T03:55:09.420025+00:00` |

Guest-specific evidence returned `kernel=Linux`, `hostname=ubuntu`, `PRETTY_NAME="Ubuntu 24.04.5 LTS"`, and `/home/admin/workspace`. This confirms the operation ran in the Ubuntu VM.

## Verification matrix

| Check | Result | Evidence |
| --- | --- | --- |
| Protected VM startup | PASS | `ops/worker/start.sh` started the VM; Softnet acknowledged the gateway block and two allow rules and the trusted DNS relay reported active at the dynamically assigned gateway on port 55353. |
| Fixed helper installation | PASS | `ops/worker/install-execution-agent.sh` completed over key-only SSH with strict pinned host-key verification. |
| Exact READY command and execution metadata | PASS | Result and execution ID/timestamps shown above. |
| Ubuntu identity | PASS | Linux kernel, Ubuntu 24.04.5 LTS, guest hostname, and guest workspace path returned by guest execution. |
| Nonzero exit | PASS | `printf 'expected failure\n' >&2; exit 7` returned failed status, exit code 7, and expected stderr. |
| stdout and stderr capture | PASS | Separate expected stream markers were returned in their respective fields. |
| Large output | PASS | 70,000 bytes on each stream returned as 65,536 bytes with both truncation flags set. |
| Timeout | PASS | `sleep 30` with a one-second execution timeout returned `timed_out` with no exit code. |
| Cancellation and polling | PASS | A running `sleep 30` execution polled as running and terminated as `cancelled`. |
| Quoted Unicode command | PASS | `printf '%s\\n' 'café "toothless"'` returned `café "toothless"` followed by a newline. |
| Binary-safe file operations | PASS | Guest workspace write/read round-trip preserved NUL and non-UTF-8 bytes; list/stat worked; the test removed its file. |
| Public DNS and HTTPS | PASS | Live adapter test resolved `example.com` and returned an HTTPS 2xx status. |
| Host-initiated SSH | PASS | The installer used the selected Ed25519 key and strict pinned host key to run guest install commands. |
| Stopped worker unavailable | PASS | After `ops/worker/stop.sh`, the adapter returned `worker_unavailable`; a submitted `touch /private/tmp/toothless-phase-02-02-host-marker` did not create a Mac-side marker. |
| No model access / no host fallback | PASS | The backend is not registered in the OpenCode or Nanobot tool registry; stopped-worker marker test confirms no local command fallback. |
| Softnet and worker stopped state | PASS | Protected launcher acknowledged policy and reported the trusted DNS relay active with direct gateway port 53 blocked; fixed-target stop completed; `tart get toothless-worker` reported `stopped`. The direct port 53 packet denial itself was not repeated in this checkpoint; the current-profile live result remains the Phase 01 evidence. |

The first stopped-worker attempt exposed a transport classification error: writing request bytes to the local SSH process was incorrectly treated as proof of guest dispatch. The marker remained absent, but the adapter returned `execution_unknown`. The guest acceptance line corrected this distinction; the final stopped-worker test passed as `worker_unavailable`. An early Unicode fixture and hostname assertion also failed due to test quoting and the image's actual hostname (`ubuntu`); both test assumptions were corrected and the final live suite passed.

## Commands and results

All commands below ran from the repository root. The repository environment used `UV_CACHE_DIR=/private/tmp/toothless-uv-cache` to keep temporary package data outside the project.

| Command | Result |
| --- | --- |
| `ops/worker/start.sh` | PASS; ran for each live verification session and exited 0 after fixed-target stop. |
| `ops/worker/install-execution-agent.sh` | PASS; exit 0. |
| `TOOTHLESS_WORKER_SSH_TEST_MODE=running TOOTHLESS_WORKER_HOST="$(awk 'NR == 1 {print $1}' /private/tmp/toothless-phase-02-02-known-hosts)" TOOTHLESS_WORKER_USER=admin TOOTHLESS_WORKER_IDENTITY_FILE="$HOME/.ssh/id_ed25519" TOOTHLESS_WORKER_KNOWN_HOSTS_FILE=/private/tmp/toothless-phase-02-02-known-hosts UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_ssh_integration.py -k 'real_worker_executes or keeps_public_dns_and_https'` | PASS; 2 passed, 1 deselected on final code. Covers command lifecycle, guest files/evidence, public DNS, and HTTPS. |
| `ops/worker/stop.sh` followed by `tart get toothless-worker` | PASS; VM reported stopped. |
| `TOOTHLESS_WORKER_SSH_TEST_MODE=stopped TOOTHLESS_WORKER_HOST="$(awk 'NR == 1 {print $1}' /private/tmp/toothless-phase-02-02-known-hosts)" TOOTHLESS_WORKER_USER=admin TOOTHLESS_WORKER_IDENTITY_FILE="$HOME/.ssh/id_ed25519" TOOTHLESS_WORKER_KNOWN_HOSTS_FILE=/private/tmp/toothless-phase-02-02-known-hosts TOOTHLESS_HOST_MARKER_PATH=/private/tmp/toothless-phase-02-02-host-marker UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_ssh_integration.py -k stopped_worker_is_unavailable` | PASS; 1 passed, 2 deselected; host marker absent. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync pytest -q tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_ssh_integration.py tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py` | PASS; 29 passed, 3 live tests skipped in the no-VM test run. The three live tests were run separately as listed above. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync ruff check nanobot/worker ops/worker tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_ssh_integration.py tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py` | PASS; exit 0. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync basedpyright nanobot/worker ops/worker/guest_worker_agent.py tests/personal_agent/test_worker_interface.py tests/personal_agent/test_worker_ssh_integration.py` | PASS; 0 errors, warnings, or notes. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m scripts.check_import_boundaries` | PASS; 13 rules, 218 files. |
| `python3 -m py_compile ops/worker/guest_worker_agent.py && bash -n ops/worker/install-execution-agent.sh` | PASS; exit 0. |
| `ops/worker/test-static.sh` | PASS; static worker safety checks passed. |

## Limits and next step

The guest's execution journal is mutable by guest `admin` and remains operational data, not a security authority. Host-owned durable dispatch records, cross-restart reconciliation, paginated/streaming output, and explicit handling of a VM reset after acceptance are not implemented. The backend bounds each result rather than providing an unlimited output stream. Process-group cancellation cannot guarantee termination of a deliberately detached process that escapes its group. A future VM reset removes the installed helper until the operator runs the install script again.

Phase 02.3 is ready to define host-owned operation persistence and reconciliation while keeping execution operator-driven and unregistered from model tools. It is not ready for unrestricted OpenCode execution or unattended autonomous commands. Do not expose the SSH backend to the model until Phase 02.3 resolves durable dispatch state and fault reconciliation.

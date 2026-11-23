# Toothless — Hotfix 07.2.1: Browser Authorization Runtime Directory

Status: Runtime-directory fix deployed and worker-side lifecycle verification passed. Fresh owner-approved visible-browser acceptance was attempted as `task-8aa45ba7d7df7982e0a520f172509e46` / `opencode-01c5cc7ef4f34dbeac522fbbd26bd261` but failed with `output_limited`; this hotfix is not marked verified.

## Incident and cause

The owner-approved blog-category task `task-3e4153d5910844a20bb494cb5475b027` ended failed with “The isolated browser helper could not start.” It is terminal and was not resubmitted. Its execution was `opencode-b856a25fd35a43f887adc7a790b49a04`; evidence shows the failure occurred before OpenCode inference or browser navigation. The guest journal records `visible_browser_broker.py` raising `FileNotFoundError` while calling `lstat()` on `/run/toothless-browser-authorizations`. That directory had been created during installation, but `/run` is volatile and the directory was absent after VM boot. `toothless-desktop.service` was consequently restart-looping. The earlier blog task `task-9d15107e26a788fca29251f8ff1e66c1` is also terminal failed (`worker_unavailable`) and was not resubmitted.

## Narrow correction

The guest now has a root-owned systemd-tmpfiles rule that creates `/run/toothless-browser-authorizations` as `root:toothless-browser`, mode `0750`. A fixed one-shot `toothless-browser-auth-dir.service` invokes that rule and is a required, ordered dependency of `toothless-desktop.service`. The root-owned task runner also starts this fixed creator unit immediately before validating the directory, covering dispatches before the normal boot tmpfiles pass. The broker retains strict `lstat` validation and fails closed for absent, symlinked, incorrectly owned, or incorrectly permissioned paths. No credentials, authorization records, or guest secrets are created by the directory helper.

No VM reset/reboot, Softnet/DNS change, baseline change, new host mount, model/profile change, network-policy relaxation, or permission expansion was made. The fix restarts only the guest desktop service. The controller and memory explorer remained separate host processes.

## Preflight and deployment evidence

Before deployment, protected readiness passed: Tart reported `toothless-worker` running, the launcher heartbeat and Softnet policy were verified, guest DNS and the approved relay were ready, OpenCode authentication metadata was present, the execution agent was ready, and both existing profiles passed their readiness checks. The preflight found no task in `dispatching`, `running`, or `cancelling`; the worker execution database had no `running`, `unknown`, or uncertain-dispatch records; the schedule database contained no unresolved occurrence and no active schedule due within two minutes. No desktop-viewer listener was active. The failed incident task remained unchanged.

Deployment used the existing protected guest path and pinned SSH identity. The deployed file hashes matched the reviewed local files: runner `03fcc4a2ebea093762ee00e6f84b67f9a080d98f1dde91503c35119a8c2339c3`, broker `b98dd150a8ef694764f7c25dfc0c0592bf5a5c2736051b06660d48a0907fa4e6`, auth-directory unit `33a56a9b1b5aed315d128f7314e09a4151e3d7ac6db6a7fdd3fb51b0493f62d9`, desktop unit `ffac04ffa265b184e52534010060a05ee398bb1c77873405227d39c063d28970`, and tmpfiles rule `2782e8f543e0188ee8170cda29a33212d15b856912ab407aad884e829acd43f2`. Systemd dependency inspection confirmed the desktop unit both requires and starts after the creator unit.

The first deploy script run returned after its immediate active-state assertion raced the existing service restart loop. Follow-up inspection found the newly installed service active; the installer was then corrected to wait a bounded 30 seconds for the active service and broker socket before declaring success. A subsequent controlled lifecycle test stopped only the idle desktop service, removed only the verified-empty authorization directory with `rmdir`, then started the service. Systemd recreated the directory with the exact root/group/mode and the Unix broker socket appeared. The service remained `active/running`; its restart count stayed at zero after the unit reload. Recent journal entries contained no new authorization-directory `FileNotFoundError`; the older pre-fix traceback remains in historical logs.

Post-fix protected readiness again passed with every required field ready. No task or execution was dispatched during installation or lifecycle testing. No browser authorization records existed during the empty-directory recreation test.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Incident task not retried | PASS | TaskStore still records the original task as failed; no new execution was created for it. |
| Active/uncertain task and execution preflight | PASS | Read-only full-table checks found no active task and no active/unknown/uncertain worker execution. |
| Schedule conflict preflight | PASS | No unresolved occurrence and no active schedule due within the bounded deployment window. |
| Protected worker readiness before and after | PASS | Tart running, Softnet/DNS/auth/agent/profiles all ready. |
| Runtime directory owner/mode | PASS | Recreated as a real directory owned by root and `toothless-browser`, mode `0750`, with no authorization records. |
| Service dependency and broker socket | PASS | Systemd `Requires`/`After` include the creator; desktop service active and Unix socket present after simulated `/run` loss. |
| Installed content hashes | PASS | All five installed files matched the local reviewed files; hashes are recorded above. |
| Focused regression tests | PASS | 25 tests passed across worker VM contract, visible-browser broker, and guest policy. |
| Ruff and BasedPyright | PASS | Ruff clean; BasedPyright reported 0 errors, warnings, or notes. |
| Fresh visible-browser Telegram task | FAIL | `task-8aa45ba7d7df7982e0a520f172509e46` / `opencode-01c5cc7ef4f34dbeac522fbbd26bd261` ended `failed` with `output_limited: OpenCode event exceeded the configured limit.`; no retry was issued. |

## Remaining acceptance

The original helper-start failure and the subsequent `output_limited` execution both stay terminal and must not be retried. The latter task ran for about 115 seconds and its worker wrapper exited successfully after emitting a bounded JSON failure result; the task was marked failed and has exactly one execution. The saved result reports that one OpenCode event exceeded 65,536 bytes. The current parser returns this error without retaining event count, event type, or observed line size, and the raw OpenCode stream is not retained, so the specific oversized event cannot be identified from current evidence. This is a separate parser/output-size issue, not a recurrence of the missing `/run` directory. The owner reported watching browser activity before the failure, but host diagnostics do not prove which browser tool event crossed the limit or whether the model reached finalization. Do not retry either task. A separately approved diagnostic/code change is needed to preserve bounded size/type metadata for rejected events and determine the narrowest remedy before another live run.

The runtime-directory fix remains deployed and its lifecycle checks remain PASS; live visible research completion, result persistence, Telegram delivery, and browser cleanup for a new acceptance task remain FAIL/NOT VERIFIED because the new task terminated at the event-size guard.

## Follow-up: Phase 07.2.2

The historical failure above remains unchanged and its actual oversized event type/size remains unknown; the owner-observed browser navigation is not proof that browser text was the oversized payload. A subsequent reliability hotfix added bounded header-only diagnostics, host-side whitelist persistence, and an 8 KiB production UTF-8-safe browser-text result cap. These changes were deployed under protected worker readiness; see `reports/phase-07-2-2.md`. A fresh owner-approved research task is still required, and the hotfix is not yet verified.

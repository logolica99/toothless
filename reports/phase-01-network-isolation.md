# Phase 01.4 host-gateway isolation report

Date: 2026-11-16 (America/Los_Angeles)

Status: HISTORICAL PHASE 01.4 SNAPSHOT — host-gateway isolation PASS at that stage. The later Phase 01.6 results supersede its full-phase status; see [phase-01-final.md](phase-01-final.md).

## Root cause and implementation

Tart 2.41.1 with Softnet 0.25.0 creates a private vmnet network and, by default, permits the guest to initiate connections to Softnet's Mac-side `@host` gateway. This is a separate path from the Mac's Wi-Fi and VPN addresses, which explained why those owned listeners were blocked while the gateway listener had been reachable. An earlier CIDR-level deny also blocked replies to Mac-initiated SSH, so it was not suitable.

`ops/worker/softnet_launcher.py` starts the fixed Tart VM with the documented Softnet control file descriptor, requests the current `softnet.policy.get`, preserves the existing policy, adds the stateful directional `out @host` block and the two narrow DNS exceptions `out proto udp @host port 53` and `out proto tcp @host port 53`, applies them with `softnet.policy.set`, and reads the effective policy back. It starts headless, console, and serial modes through the same wrapper. Failure to communicate, apply, or verify the policy terminates the Tart process. The directional rule prevents new guest-initiated host flows; Softnet's stateful flow authorization permits return packets for a Mac-initiated SSH session. DNS is limited to the gateway's UDP/TCP port 53.

The rules are scoped to the Tart/Softnet process and use `@host`, so they remain correct when vmnet selects a different gateway address. They are reapplied and verified on each start and disappear on stop. No PF rules, global firewall settings, or guest firewall rules were changed. The current worker is stopped.

## Changed files

- `ops/worker/softnet_launcher.py`: control-socket policy application, read-back verification, and fail-closed process lifecycle.
- `ops/worker/start.sh`, `start-console.sh`, and `start-serial.sh`: route all start modes through the policy launcher.
- `ops/worker/test-static.sh`: require the wrapper and policy markers.
- `tests/personal_agent/test_worker_vm_contract.py`: deterministic policy-preservation and rule-application contract test.
- `docs/worker-vm.md`, `docs/architecture.md`, `docs/features.md`, and `docs/status.md`: current policy, operating boundary, remaining release blockers, and rollback steps.
- `reports/phase-01.md`: prior results retained and annotated with the Phase 01.4 correction.

## Test results

| Check | Result |
| --- | --- |
| Tart version and control interface | PASS: installed Tart 2.41.1 help exposes `--net-softnet-control-fd`; Softnet 0.25.0 control API used |
| Effective policy update/read-back | PASS: launcher acknowledged the applied rules on live starts; policy was reapplied after reset |
| Owned TCP listener at Softnet gateway | PASS: blocked across changing gateway addresses |
| Owned UDP fixture at Softnet gateway | PASS: blocked |
| Owned Mac Wi-Fi listener | PASS: blocked |
| Owned Mac VPN/CGNAT listener | PASS: blocked |
| Key-only host-initiated SSH | PASS: worked after policy application and after reset |
| DNS to gateway | PASS: guest resolver worked with only TCP/UDP port 53 exceptions |
| Public HTTPS and package install | PASS: HTTPS and `apt` package installation succeeded |
| UFW inactive | PASS: gateway TCP/UDP remained blocked with guest firewall inactive |
| Guest IPv6 state | PASS for observed profile: only link-local address, no global address/default route |
| Owned IPv6 link-local gateway listener | PASS: guest connection did not succeed |
| Emergency stop | PASS: host-side `stop.sh` halted a CPU-bound guest in 0.355 seconds |
| Reset and policy persistence | PASS: reset removed installed package and the next start acknowledged the policy again; worker left stopped |
| Static worker checks | PASS: `ops/worker/test-static.sh` — exit 0 |
| Worker contract tests | PASS: `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m pytest tests/personal_agent/test_worker_vm_contract.py -q` — exit 0, 5 passed |
| Ruff lint | PASS: `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check ops/worker/softnet_launcher.py tests/personal_agent/test_worker_vm_contract.py` — exit 0 |
| Type and import-boundary checks | NOT RUN: no Phase 01.4 type/import-boundary command was run |
| Owned DNS rebinding and redirect fixture | NOT RUN: no owned public domain/server fixture available |
| IPv4 metadata-shaped, remaining link-local, multicast fixtures | NOT RUN: no owned target contacted |
| IPv6 ULA, multicast, and IPv4-mapped fixtures | NOT RUN: no owned fixture available; public IPv6 also had no guest route |
| PF/system firewall inspection | NOT RUN: read-only `pfctl` inspection required sudo authentication; no PF policy was needed or changed |

The live checks were run against owned listeners only. No unowned private-network services were probed. The DNS exception is required for the current guest resolver and was confirmed alongside blocked arbitrary TCP/UDP gateway fixture traffic. Softnet's documented policy API is process-scoped and stateful; the test observed the packet behavior directly rather than treating policy syntax acceptance as proof.

## Install and rollback

The code change requires no privileged host installation. Use `ops/worker/start.sh`, `start-console.sh`, or `start-serial.sh`; do not run the worker directly through `tart run`, which bypasses policy installation. The launcher fails closed if Softnet does not acknowledge the required rules. To stop the worker, run `ops/worker/stop.sh`; it halts the fixed VM without guest cooperation. To revert the implementation, first stop the VM, then restore the reviewed launcher and start-script versions from version control. Keep unattended startup disabled unless an equivalent externally enforced policy is installed and tested. Since no host or PF configuration changed, rollback does not require altering system networking.

## Remaining risks and status

The specific confirmed gateway vulnerability is fixed and live-tested while authenticated SSH management remains usable. DNS resolution necessarily permits guest requests to the Mac-side gateway on port 53; that exception is protocol/port constrained, but a dedicated external resolver could remove this dependency in a future design. Softnet's bounded stateful authorization may age out idle management flows, so operators should reconnect if SSH idle sessions are interrupted. This test does not establish behavior for all private, metadata-shaped, multicast, DNS-rebinding, redirect, or IPv6 address classes.

Phase 01.4's final status was host-gateway isolation PASS with later fixtures not yet run. Phase 01.6 subsequently tested the owned link-local and metadata-shaped endpoints; current-profile verification and release recommendations are in [phase-01-final.md](phase-01-final.md).

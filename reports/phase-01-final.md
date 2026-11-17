# Phase 01 final isolation verification

Date: 2026-11-16 (America/Los_Angeles)

## Release status

**VERIFIED FOR THE CURRENT DEPLOYMENT PROFILE: YES.** The prior owned IPv4 host/LAN/link-local/metadata fixture checks passed, and Phase 01.8 sealed the DNS client into the baseline, reset the worker, and passed the required post-reset policy, resolver, DNS, HTTPS, direct port 53, and key-only SSH checks. One initial post-reset HTTPS attempt timed out during name resolution; a follow-up after A/AAAA and `getent` resolution succeeded with HTTP 200. Future IPv6-enabled profiles remain unverified.

**VERIFIED FOR UNATTENDED AUTONOMOUS EXECUTION: NO.** Phase 02 is NO-GO. The previous port 53 exception is superseded by the validated DNS relay on port 55353; DNS-based exfiltration and direct public DoH/DoT remain possible, and IPv6 ULA/global routes are absent from this profile and remain unverified if enabled later. Phase 13 operational recovery and applicable Phase 14 evaluation gates are also incomplete.

## Scope and environment

The active specification is `INSTRUCTIONS.md`; lowercase `instruction.md` is absent. The live profile used Tart 2.41.1, Softnet 0.25.0, an Ubuntu ARM64 worker with 4 vCPUs, 6144 MB RAM, and a 20-GB disk on `/Volumes/toothless`. The VM has one IPv4 interface (`enp0s1`), one IPv4 default route through the Softnet gateway, IPv6 link-local only, no IPv6 default/ULA route, and UFW inactive. The final worker was reset to the sealed baseline and shut down cleanly.

At the time of the original Phase 01 network fixture run, the launcher applied a process-scoped directional `out @host` block and TCP/UDP port 53 allowances to the `@host` destination, then read the policy back. Phase 01.7 superseded those DNS exceptions with a relay on port 55353; see [the Phase 01.7 report](phase-01.7.md) for its current tests. No PF rules, host-global firewall settings, or persistent host network configuration were changed. The temporary aliases were removed when Tart stopped the VM; exact alias and route cleanup was verified before the fixed-target reset.

## Test matrix

| Test | Classification | Result and limits |
| --- | --- | --- |
| Guest to Softnet gateway TCP/UDP | Required and testable on current network | PASS: owned listeners on `10.60.249.137:18080/18081` received no connection/datagram; guest attempts timed out. |
| Guest to Mac Wi-Fi/VPN TCP/UDP | Required and testable on current network | PASS: owned listeners on `192.168.4.21` and `100.80.175.115` at ports 18080/18081 received no connection/datagram. This covers the Mac-owned addresses, not all devices on those networks. |
| DNS to gateway using UDP and TCP | Historical Phase 01 evidence, superseded by Phase 01.7 | Original tests used gateway port 53. The current profile uses the validated TCP/UDP 55353 relay; see Phase 01.8 results below. |
| DNS exception application-layer confinement | Current implementation | PASS for rejecting malformed/non-DNS payloads at the relay. DNS-name exfiltration and direct public DoH/DoT remain possible; see Phase 01.7 limitations. |
| Public IPv4 HTTPS | Required and testable on current network | PASS: guest `curl -4 https://example.com` returned HTTP 200, before and after reset. |
| Private DNS answer | Applicable and locally simulated | PASS, limited: a temporary guest-local responder returned `203.0.113.7` and then the owned Mac Wi-Fi address `192.168.4.21` for the same name; a connection to the private answer timed out. The simulation did not publish a public DNS record or test a public recursive resolver's rebinding behavior. |
| HTTP redirect to private/host destinations | Applicable and locally simulated | PASS at network layer: a guest-local HTTP server redirected to owned `192.168.4.21` and `10.60.249.137` listeners; both target connections timed out. This tests the external network boundary, not a production fetcher's redirect policy. |
| HTTP redirect to loopback | Not a host-isolation target in this VM test | Guest-local redirect to `127.0.0.1:18083` reached a guest-owned fixture. Loopback refers to the guest, not the Mac. Application-layer SSRF/loopback denial remains to be tested in the future fetcher implementation. |
| IPv4 link-local owned endpoint | Required and testable on current profile | PASS in two rounds: Mac-owned `169.254.253.1:18087` listener returned the expected marker to the Mac; guest route used the Softnet gateway on `enp0s1`, and guest connection timed out. No extra listener accept occurred during guest probes. |
| IPv4 metadata-shaped owned endpoint | Required and testable on current profile | PASS in two rounds: Mac-owned `169.254.169.254:18088` listener returned the expected marker to the Mac; guest route used the Softnet gateway on `enp0s1`, and guest connection timed out. No real cloud metadata service was contacted. |
| IPv4-mapped IPv6 to owned Mac address | Required when mapped sockets are usable | PASS for tested endpoint: an AF_INET6 connection to `::ffff:192.168.4.21:18080` timed out. |
| IPv6 ULA/global egress | Not applicable to current guest routing | Guest `ip -6 route` contained only `fe80::/64`; `ip -6 route get fd86:eac0:4d11:bee7::1` returned “Network is unreachable.” No conclusion is made about a future profile with IPv6 routing. |
| IPv6 link-local to owned Mac bridge listener | Required where link-local peer route exists | PASS for tested endpoint: guest connection to the owned `fe80::...%bridge100` listener timed out and the listener received no connection. |
| IPv4 multicast | Applicable and locally simulated | PASS with narrow scope: guest sent one TTL-1 packet to group `239.255.0.42`; an owned listener on the Tart bridge received nothing. This does not establish behavior for all groups, scopes, or routes. |
| Alternate guest interface/route | Not applicable to current configuration | Guest exposed only `enp0s1` and one default route. No alternate interface or route was configured. Direct Tart launches or future interface changes are not covered by this observation. |
| Guest firewall independence | Required and testable on current network | PASS for observed state: UFW was inactive during the tests, while the owned gateway/private connection attempts remained blocked. The tests did not toggle UFW during a session. |
| Host-initiated SSH management | Required and testable on current network | PASS: repeated fresh Ed25519 key-only SSH commands worked after policy application and after reset. Long-lived idle-session survival was not separately timed in this run. |
| Reset and policy reapplication | Required lifecycle check | PASS: reset removed the guest-local fixtures; the next supported start re-acknowledged one block/two allow rules; post-reset SSH, DNS, and HTTPS worked; worker was shut down cleanly. |
| Emergency stop | Required lifecycle check | PASS, reused prior Phase 01 evidence: [phase-01.md](phase-01.md) records the CPU-bound guest stop in under one second. It was not repeated because this task changed neither stop code nor host lifecycle controls. |
| Fail-closed policy error | Required and locally simulated | PASS: fake Tart returned a JSON-RPC policy error then stayed alive; launcher exited 1 in under one second and terminated the child. No real VM was used by this failure test. |
| Direct Tart startup | Unsupported/unprotected path | NOT RUN: a direct `tart run --net-softnet` bypasses the wrapper and would start a VM without the managed policy sequence. Every repository start script calls the wrapper, but the Tart CLI cannot be prevented from direct host invocation. Do not treat direct Tart startup as protected. |
| PF/system firewall inspection | Not required by chosen mechanism | NOT RUN: no PF policy was needed or changed; earlier read-only inspection required sudo. |
| Restart persistence of link-local/metadata isolation | Required lifecycle check | PASS: after stopping and relaunching through the protected launcher, the policy acknowledged successfully and both owned endpoint probes timed out again. Tart changed the worker/gateway addresses; bridge selection was reverified using `vmenet0` membership, gateway ownership, and the host route to the guest. |

## Regression verification

| Command | Exit/result |
| --- | --- |
| `ops/worker/test-static.sh` | PASS, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m pytest tests/personal_agent/test_worker_vm_contract.py tests/security/test_security_network.py tests/config/test_network_proxy.py -q` | PASS, exit 0, 98 passed. The sandboxed first attempt had 97 passed/1 failed because it denied a test's `127.0.0.1` listener bind; rerun with local socket permission passed. |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check ops/worker/softnet_launcher.py tests/personal_agent/test_worker_vm_contract.py` | PASS, exit 0 |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync basedpyright ops/worker/softnet_launcher.py tests/personal_agent/test_worker_vm_contract.py` | PASS, exit 0, 0 errors/warnings/notes |
| `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m scripts.check_import_boundaries` | PASS, exit 0, 13 rules/218 files |
| `git diff --check` | PASS, exit 0 (after report updates) |
| Phase 01.6 procedure syntax check | PASS: `bash -n`, `sh -n`, and Python `compile()` checked all 9 fenced command blocks and nested heredocs before execution |

## Live commands and results

Exact fixture, host-control, guest-probe, and cleanup command blocks are recorded in [phase-01.6-fixture-procedure.md](phase-01.6-fixture-procedure.md). The operator's two local `sudo ifconfig ... alias` commands printed both addresses on `bridge100`; the shell exit code was not captured. `route -n get` showed `lo0`, expected for host-owned local addresses. The first `ops/worker/start.sh` attempt failed closed because the VM was already running and Softnet closed its control channel before policy acknowledgement; after `ops/worker/stop.sh`, two starts acknowledged the expected policy and both launcher sessions later exited 0 through the fixed stop path. The host positive-control Python script returned both expected markers in each round. The SSH test sequence completed with expected outputs in each round; its guest probes printed `DENIED ... TimeoutError` for both targets, followed by successful DNS UDP/TCP answers and `HTTPS 200`. The temporary listeners were intentionally interrupted with Ctrl+C (exit 130); their logs contained only the host positive-control accepts. `ops/worker/stop.sh` and `printf 'RESET-WORKER\n' | ops/worker/reset.sh` completed successfully; Tart listed both VMs stopped.

Both listener instances were stopped with Ctrl+C; their logs show only the two Mac positive-control accepts per round, and no guest connection. The worker was stopped; both aliases were absent and no exact routes remained. The pre-existing broad `169.254.0.0/16` route via `en1` remained unchanged. The fixed-target reset completed, and Tart reports both `toothless-worker` and the sealed base stopped. The temporary known-hosts file under `/private/tmp` was removed; no test fixture service was added to the repository.

## Risks and release decision

The tested network boundary prevents the guest from reaching the Mac's Softnet gateway, Wi-Fi, VPN, IPv4 link-local, and metadata-shaped owned listeners on the tested endpoints and ports while preserving fresh key-only SSH. For both fixture rounds, guest UFW was inactive, nftables and iptables inspection returned no rules, public DNS worked, and HTTPS returned 200. Phase 01.7 replaces arbitrary gateway TCP/UDP port 53 access with a validating relay on port 55353; DNS-name exfiltration and direct public DoH/DoT remain possible. IPv6 is absent from the current route table and has not been secured for a future IPv6-enabled profile. Direct Tart CLI use bypasses the wrapper and remains an operator hazard.

**Go/no-go:** Phase 01 is VERIFIED FOR THE CURRENT DEPLOYMENT PROFILE. Phase 02 and unattended autonomous execution remain NO-GO because DNS-based exfiltration/direct public encrypted DNS remain possible, future IPv6 profiles are unverified, and separate runtime, recovery, and evaluation gates remain. Keep the worker stopped between tasks and launch only through `ops/worker/start.sh` (or the documented console/serial wrapper).

## Phase 01.8 baseline persistence review

`ops/worker/seal-baseline.sh` deletes the stable `toothless-ubuntu-24.04-base`, clones the current `toothless-worker` into that name, deletes the current worker, then clones a new worker from the replacement base. Before sealing, the versioned `toothless-ubuntu-24.04-base-rollback-20261116` already existed, so it was preserved and not overwritten. The sealer replaced the stable baseline and worker only; the OCI cache and rollback clone remained. After sealing, the stable baseline and worker each used 4 GB, the rollback clone used 5 GB, and the SSD had 114 GiB free.

The installed guest configuration was confirmed before sealing: `toothless-dns-configure.service` enabled; executable `/usr/local/libexec/toothless/configure-dns-client`; `/etc/netplan/99-toothless-dns.yaml` disables DHCP DNS; the configurator discovers the `enp0s1` default gateway dynamically and sets `resolvectl` to `$gateway:55353` with domain `~.`. It does not hard-code the gateway address.

After sealing, the first protected launch acknowledged the `out @host` block and two allow rules. The guest resolver used `192.168.84.241:55353`; DNS returned `172.66.147.243` and `104.20.23.154`; gateway port 53 returned no DNS answer; HTTPS returned 200; and key-only SSH succeeded. The worker was gracefully stopped before reset.

`printf 'RESET-WORKER\n' | ops/worker/reset.sh` completed successfully. The reset clone was then launched through `ops/worker/start.sh`, which again acknowledged the block/two allows and started the relay at the newly assigned gateway `192.168.85.169:55353`. The guest resolver matched that gateway. Direct A and AAAA queries, `resolvectl query` for A and AAAA, `getent ahostsv4`, and the port 53 denial passed. The first HTTPS request timed out during DNS resolution; after the successful DNS diagnostics, one follow-up HTTPS request returned HTTP 200. Key-only SSH succeeded. The worker was gracefully stopped after verification.

The versioned rollback remains stopped and retains the pre-DNS-client baseline. Restoring it requires replacing the current stable baseline and worker with clones from that versioned image; the DNS client must then be installed again before using the current relay policy. No PF rules or global macOS firewall settings were changed. Direct Tart launches remain unprotected.

Final regression commands and results: `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m pytest -q tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py tests/security/test_security_network.py tests/config/test_network_proxy.py` — 107 passed, exit 0; `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync ruff check ops/worker/softnet_launcher.py ops/worker/dns_forwarder.py tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py` — exit 0; `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync basedpyright ops/worker/softnet_launcher.py ops/worker/dns_forwarder.py` — 0 errors/warnings/notes; `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m scripts.check_import_boundaries` — 13 rules, 218 files, exit 0; `bash -n ops/worker/*.sh` and `bash ops/worker/test-static.sh` — exit 0.

## Operator notes and scope limits

The executed procedure and its operator-local sudo boundary are recorded in [phase-01.6-fixture-procedure.md](phase-01.6-fixture-procedure.md). The `lo0` result from `route -n get` is expected for a host-owned address; bridge ownership was verified separately in `ifconfig bridge100`. Never send packets to a real cloud metadata service.

# Phase 01.7 — DNS boundary hardening

Date: 2026-11-16 (America/Los_Angeles)

Status: VERIFIED FOR THE CURRENT DEPLOYMENT PROFILE after Phase 01.8 sealed the DNS client into the baseline and verified reset persistence. Phase 01.8 evidence is in `reports/phase-01-final.md`. The worker is stopped. DNS-based exfiltration, public DoH/DoT, future IPv6 profiles, Phase 02, and autonomous command execution remain outside this verification.

## Design and changes

The prior exception allowed arbitrary UDP or TCP payloads to reach the Mac's Softnet gateway on port 53. Softnet rules are network-layer protocol/destination/port rules and cannot inspect DNS messages, so changing only the port restriction would not safely distinguish DNS from other traffic. This implementation removes the old TCP/UDP `@host` port 53 entries and permits only TCP/UDP port 55353 to `@host`.

The protected launcher dynamically identifies the guest IPv4 address and the host interface on the route to that guest, requiring the interface to be a `vmenet0` member. It binds the host DNS relay only to that bridge address and accepts packets only from the exact worker IPv4 address. The launcher applies and reads back the stateful `out @host` block and new two-rule allowlist, waits for relay readiness, monitors the relay, and terminates the worker if the relay exits. The protected start scripts remain the only supported launch path; direct Tart launches bypass this policy and are unprotected.

`ops/worker/dns_forwarder.py` parses DNS messages before forwarding them and rejects non-DNS, malformed, unsupported, oversized, or unauthorized-source traffic without returning a payload. It accepts one standard IN question, standard query opcode and selected standard header flags, ASCII/IDNA DNS labels, and only EDNS version 0 with the DNS COOKIE option. It rejects AXFR, IXFR, ANY, other EDNS options, malformed packets, and arbitrary extra records. UDP queries are limited to 1232 bytes, TCP query frames to 4096 bytes, replies to 4096 bytes, concurrent requests to 8, and upstream operations to 3 seconds. A per-worker token bucket permits a burst of 20 and refills at 10 queries per second. Upstream is fixed to `1.1.1.1:853` with TLS 1.2 or newer, normal certificate-chain verification, and SNI `cloudflare-dns.com`; no global macOS firewall or PF configuration is changed.

The guest resolver client uses `systemd-resolved` with the dynamically assigned gateway on port 55353. `ops/worker/install-dns-client.sh` installs a systemd oneshot and netplan DNS override in the guest. These files must be present in the sealed baseline for reset clones to use the relay. The guest's resolver configuration was installed on the current clone, then DNS and HTTPS were verified after a protected worker restart. The existing sealed baseline was not replaced.

## Validation and remaining DNS risk

The relay establishes that forwarded packets are well-formed DNS protocol messages, but it is not a domain allowlist or a data-loss prevention service. Any supported DNS name can contain encoded data in its labels; TXT and other ordinary query types can carry query intent or data. The relay can therefore be used for DNS-based exfiltration to destinations controlled by an external party, within DNS wire-format limits and the 20-query burst / 10-query-per-second limit. The upstream resolver sees requested names. No packet/query logging is enabled by this implementation, which limits local query auditing. Direct public-IP egress remains available, so an agent could use public DoH, DoT, or another public service that does not rely on this resolver. Those behaviors are outside this checkpoint; do not treat this change as general egress confinement.

The relay uses certificate-checked DNS-over-TLS to a fixed resolver, but the resolver remains an external service that receives DNS names. Changing the resolver, adding a domain allowlist, blocking direct public encrypted DNS, adding query logging, or applying a lower per-name policy would be separate policy decisions. The current Softnet configuration and relay are IPv4-only. IPv6-enabled guest profiles remain unverified, and DNS relay behavior must not be assumed to protect them.

Design references: [Softnet's documented control and policy model](https://github.com/openai/softnet), [systemd-resolved DNS server configuration](https://www.freedesktop.org/software/systemd/man/latest/resolved.conf.html), and [Cloudflare's DNS-over-TLS endpoint details](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-tls/).

## Verification matrix

| Check | Result | Evidence |
| --- | --- | --- |
| Deterministic Softnet policy migration | PASS | Fake control-channel test starts with old UDP/TCP port 53 allows and verifies only TCP/UDP 55353 remains with `out @host` blocked. |
| DNS parser and rate-limit unit tests | PASS | Standard DNS, EDNS COOKIE, malformed/non-DNS packets, unapproved EDNS, blocked transfer/ANY, mismatched response, SERVFAIL, and token bucket tests passed. |
| Focused contract and DNS tests | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m pytest -q tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py` — 14 passed, exit 0. |
| Ruff | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync ruff check ops/worker/softnet_launcher.py ops/worker/dns_forwarder.py tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py` — all checks passed, exit 0. |
| BasedPyright | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync basedpyright ops/worker/softnet_launcher.py ops/worker/dns_forwarder.py` — 0 errors, warnings, or notes, exit 0. |
| Shell syntax | PASS | `bash -n ops/worker/*.sh` — exit 0. |
| Static worker checks | PASS | `bash ops/worker/test-static.sh` — “Static worker safety checks passed.”, exit 0. |
| Broader security/config regression suite | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m pytest -q tests/personal_agent/test_worker_vm_contract.py tests/personal_agent/test_worker_dns_forwarder.py tests/security/test_security_network.py tests/config/test_network_proxy.py` — 107 passed, exit 0 when run with permission for its existing loopback fixture. An earlier sandboxed attempt failed only because that fixture could not bind `127.0.0.1`; it passed on the permitted rerun. |
| Import-boundary check | PASS | `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --project . --no-sync python -m scripts.check_import_boundaries` — 13 rules, 218 files, exit 0. |
| Protected worker restart and policy reapplication | PASS | Stopped and relaunched twice with the protected launcher; each launch acknowledged `out @host` block and exactly two allow rules and bound the relay to the dynamically discovered current bridge address (the gateway changed across launches). The guest resolver also showed the current `:55353` endpoint after the first restart. |
| Standard DNS with EDNS COOKIE | PASS | Authenticated guest query `dig +time=3 +tries=1 @172.31.132.161 -p 55353 example.com A +short` returned `104.20.23.154` and `172.66.147.243`. `getent ahostsv4 example.com` also resolved. |
| Public HTTPS | PASS | From the guest, `curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://example.com/` returned HTTP 200. |
| Direct gateway port 53 | PASS | On the second protected restart, a valid `dig` query to the current gateway on port 53 returned no answer; the same public lookup through port 55353 returned two IPv4 answers. |
| Malformed/non-DNS relay payload | PASS | On the final protected launch, an HTTP-shaped UDP payload to 55353 timed out with no DNS response; TCP closed with zero response bytes. The test output classified both as rejected. |
| Guest firewall independence | PASS | Guest reported `ufw` inactive during the live tests. |
| Host-initiated SSH | PASS | Fresh Ed25519 key-only SSH connection using a temporary known-hosts file pinned to the guest public host key succeeded after restart. Temporary file was removed. |
| Live DNS relay startup fail-closed | PASS by code path and launcher review | Worker startup waits for relay readiness and terminates Tart on policy/relay failure; a live forced failure injection was not run. |
| Baseline seal and reset persistence | PASS in Phase 01.8 | The existing versioned rollback was retained, the DNS-capable baseline was sealed, the worker was reset from it, and the post-reset policy, resolver, DNS, HTTPS, port 53, and SSH checks passed. See `reports/phase-01-final.md`; the worker remains stopped. |
| Global firewall/PF mutation | NOT PERFORMED | No global macOS firewall configuration was changed. |
| IPv6 relay/isolation | NOT VERIFIED | The current Softnet policy and DNS relay bind IPv4 only; this does not establish protection for a future IPv6-enabled worker. |

## Operator follow-up

Phase 01.8 sealed the guest DNS configuration into the baseline, reset the worker, and repeated the protected-launch checks. The worker is stopped after verification. Do not use direct `tart run` for protected launches.

No real cloud metadata endpoints or unowned private services were contacted. The Phase 01.7 relay narrows and validates the gateway exception; it does not authorize general autonomous command execution.

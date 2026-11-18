# Worker VM operator guide

## Selected backend

Phase 01 selects Tart 2.41.1 with Softnet 0.25.0 on Apple silicon. The worker image is Ubuntu 24.04 pinned to `ghcr.io/cirruslabs/ubuntu@sha256:e004f7f4f6765e2b3ae2738a95040c5d03867b667eb21a57ea0c3729aad640b6`. Tart and Softnet use the Functional Source License 1.1 with an Apache 2.0 future license; this personal/internal use is within the listed permitted purposes, but redistribution or competing commercial use needs a separate review.

Softnet runs outside the guest, creates a separate private network, and checks the guest MAC and assigned IPv4 address. Its default policy permits globally routable IPv4 destinations, return traffic, and the Mac vmnet gateway. Every supported worker start mode now runs through `softnet_launcher.py`, which uses Tart's control FD to add the stateful directional block `out @host` and permits only TCP/UDP port 55353 to `@host`. The launcher discovers the active Softnet bridge and worker address, starts a host DNS relay bound to that exact bridge/client pair, reads the Softnet policy back, and terminates Tart if policy acknowledgement or relay startup fails. The relay accepts one parsed DNS question, validates standard DNS framing, limits message sizes and concurrent requests, rejects malformed/non-DNS payloads and uncommon EDNS options, and forwards over certificate-checked DNS-over-TLS to the fixed Cloudflare resolver. It rate-limits the worker to a 20-query burst and 10 queries per second. The guest resolver configuration from `ops/worker/install-dns-client.sh` is sealed in the current baseline and passed the post-reset DNS/HTTPS check. The worker execution helper is installed separately by `ops/worker/install-execution-agent.sh` after a protected launch; it is not part of the baseline, so repeat that provisioning step after reset. No script changes macOS PF or global firewall settings or enables directory sharing, clipboard integration, USB, SSH-agent forwarding, Docker sockets, bridge mode, exposed ports, or private-network allowlists. Direct `tart run --net-softnet` launches bypass the repository wrapper and are unprotected. IPv6-enabled profiles remain unverified; see [the Phase 01.7 report](../reports/phase-01.7.md) and [the final Phase 01 report](../reports/phase-01-final.md).

## Current Mac profile

Discovery on 2026-11-15 found an Apple M4 Mac mini with 16 GB RAM, 10 logical cores, arm64, and macOS 26.5.2. The selected worker allocation is 4 vCPUs, 6 GB RAM, and a 20 GB bounded virtual disk. Hardware identifiers and serial numbers are deliberately not recorded.

The dedicated external SSD is an empty 127.8-GB APFS volume mounted at `/Volumes/toothless`. All Tart VM and OCI-cache data is fixed to the user-owned `/Volumes/toothless/Tools/Tart` directory through `TART_HOME`; the preflight rejects a missing, non-writable, non-APFS, or low-space volume rather than falling back to the internal disk. The user-owned subdirectory avoids granting an elevated process privacy access to the external volume root.

## Reviewed installation

The Mac's installed Command Line Tools were too old for Homebrew's installation gate, so the exact prebuilt release archives were installed under `/Volumes/toothless/Tools` after verifying the formula-published SHA-256 values. Tart's app signature was also verified. Softnet cannot execute setuid from the SSD's deliberately retained `nosuid` mount, so its hash-verified binary was ad-hoc signed for Apple silicon and installed at `/usr/local/libexec/toothless/softnet` as `root:wheel` mode `4755`, with `/usr/local/bin/softnet` as a symlink. The ad-hoc signature identifies the exact local payload but does not provide Developer ID provenance or notarization. The expected hashes are:

- Tart 2.41.1: `e680a734febccfce7d4e910c1f7f13fc3b00f19a181c945e2d746de3214c32c2`
- Softnet 0.25.0: `5a2e8ef779f0a2169b88d273ee806c0d64321ec7714c0dc99bf217523ae9b008`
- Installed ad-hoc-signed Softnet binary: `19768da958f49ea3dca4ba18e5cff34bdceab781063162e72ba90f77b7477105`

For a future Homebrew-managed reinstall after updating Command Line Tools, use:

```bash
brew install openai/tools/tart
brew install openai/tools/softnet
ops/worker/preflight.sh
ops/worker/provision.sh
```

`preflight.sh` rejects versions other than Tart 2.41.1 and Softnet 0.25.0. It also requires Softnet's exact resolved path, signed hash, signature, root ownership, and setuid mode. If Homebrew offers a different version, do not continue silently; pin the formula or update `ops/worker/config.sh` only after reviewing the replacement release and license. Manual-install rollback removes `/usr/local/bin/softnet` and `/usr/local/libexec/toothless/softnet`; VM data under `/Volumes/toothless/Tools/Tart` must be separately removed with an explicit `tart delete` command before removing `/Volumes/toothless/Tools`.

The published image initially uses `admin` / `admin`. Start once with `ops/worker/start.sh`, replace that password, install your SSH public key, disable password authentication, and stop the VM. Never put a host private key or host credentials in the guest. Run `ops/worker/seal-baseline.sh` to replace the vendor base with this hardened image and recreate a clean worker; do not treat the vendor-default credential image as deployment-ready.

## Lifecycle

```bash
ops/worker/start.sh
ops/worker/start-console.sh
ops/worker/start-serial.sh
ops/worker/harden.sh
ops/worker/shutdown.sh
ops/worker/stop.sh
ops/worker/seal-baseline.sh
ops/worker/reset.sh
```

`start-console.sh` is an operator-only recovery/first-boot launcher. It shows the guest display while retaining Softnet and explicitly disabling host clipboard, USB accessories, and audio. Do not enable a directory share or paste credentials through the host clipboard.

If the GUI cannot capture keyboard input, use `start-serial.sh`. It attaches the Linux serial console directly to Terminal while retaining the headless, Softnet, no-clipboard, no-USB, and no-audio restrictions. Exit it with `Ctrl+]` if the serial client consumes `Ctrl+C`.

On the first boot, run `ops/worker/harden.sh` in a second terminal. It defaults to `~/.ssh/id_ed25519.pub`, accepts only an Ed25519 public key, verifies key login before changing SSH, and installs the same key at the root-owned `/etc/ssh/authorized_keys/admin` path so cloud-init cannot replace it when a clone first boots. The `00-toothless-hardening.conf` name is deliberate because OpenSSH keeps the first value read for these settings and the image ships later cloud-init drop-ins. It disables password/keyboard-interactive/root login and forwarding, validates and reloads `sshd`, proves a fresh key-only connection through the persistent path, and only then locks the vendor `admin` password. The corresponding private key remains on the Mac and is never copied into the guest. After the verification succeeds, stop the worker and run `seal-baseline.sh` to replace the vendor base with the hardened state. A post-clone key-only login is mandatory before accepting the baseline.

`shutdown.sh` syncs the guest filesystem, requests a normal systemd poweroff over key-only SSH, and waits for Tart to report `stopped`. `seal-baseline.sh` refuses to clone a running worker. `stop.sh` is the host-controlled emergency stop and does not depend on guest cooperation, but it is not a clean shutdown and must not be used immediately before sealing a baseline. `reset.sh` requires the literal confirmation `RESET-WORKER`, deletes only the fixed worker name, and clones the fixed clean base. It never accepts a VM name from agent-controlled input.

After a reset, start the clone only with `ops/worker/start.sh`. Wait for the Softnet policy acknowledgement, then run `ops/worker/install-execution-agent.sh` in another terminal. That installer obtains the guest key through Tart's guest agent, pins it for the current address, and installs the fixed root-owned helper over key-only SSH. To restore Phase 05.2, also run `ops/worker/install-opencode.sh`, then authenticate the separate guest account using `ops/worker/opencode-auth-login.sh`. The authentication step is interactive; enter credentials only into the guest prompt. Host execution records live under the controller's runtime `worker/executions.sqlite3` database and remain available across guest resets. If the guest journal has been removed, reconciliation records `unknown` with `execution_not_found`; it never converts missing history to success or resubmits the command.

## Phase 05.2 guest tool boundary

The fixed root-owned OpenCode runner is reachable only through the SSH worker helper. It refuses non-root callers, then runs OpenCode as the separate system account `toothless-agent`, with no sudo/admin/root group membership and no SSH key. The account has a private home; its OpenCode Go credential is stored under `/home/toothless-agent/.local/share/opencode`. OpenCode shell processes run with this same identity and can read that guest-local credential. The guest credential is therefore inside the model/tool trust boundary; use a low-risk account and never copy Mac credentials into the guest.

The agent workspace is `/home/toothless-agent/workspace`; the authenticated worker file interface exposes it under the relative prefix `opencode-task-workspace/`. OpenCode rules allow read, edit/write, glob, grep, and shell tools, deny external-directory requests and all other tools by default, and deny common privilege-escalation, apt/dpkg, service-control, reboot/shutdown, and mount commands. Filesystem tool rules and shell scanning are application-level restrictions, not an OS sandbox: guest commands retain the account's ordinary filesystem, process, and public-network access. Host/LAN isolation remains enforced by Softnet outside Ubuntu; general public egress and DNS-based exfiltration remain possible.

The guest account cannot use sudo. System package installation and root privileges are unavailable in this checkpoint. A live preflight found that Ubuntu has no pip and cannot create a venv because `python3-venv`/`ensurepip` is missing. Do not install it until the operator separately approves the exact one-time package operation; do not grant passwordless sudo or expose package-manager elevation to the model. Do not add the agent to management groups, expose an SSH key, install plugins/MCP, change the pinned model/provider policy, or start queued tasks automatically. To roll back the configuration without deleting data, stop the worker safely and remove only the dedicated `toothless-agent` account and its workspace after confirming no task or artifact needs it; do not remove the baseline or admin account. A direct Tart launch remains unprotected.

## Real acceptance gate

Run from a disposable worker after setting up owned dummy listeners. Record addresses and results without including local secrets:

1. Confirm public HTTPS and package downloads work.
2. Attempt the host gateway, every host interface, owned LAN listeners, RFC1918 ranges, carrier-grade NAT, loopback, link-local, multicast, and metadata-shaped addresses.
3. Test IPv6 global, ULA, loopback, link-local, multicast, and IPv4-mapped addresses. Softnet documents IPv4 policy; any usable IPv6 egress is a failed gate until an external host rule blocks private/local targets.
4. Disable the guest firewall as root and repeat all tests.
5. Test DNS rebinding and redirects with an owned domain and server.
6. Confirm the guest has no shared mounts, clipboard channel, forwarded SSH agent, host Docker socket, or host-login credential.
7. Modify a marker, install a package, reset, and prove both changes disappeared.
8. Prove `stop.sh` halts a guest that is CPU-bound and ignoring shutdown requests.
9. Confirm configured CPU, RAM, and disk limits from host and guest views.

Phase 01.4 implements the gateway policy above. Start and stop the VM only through the documented launchers; the dynamic policy is attached to the Tart process and disappears when it stops, then is reapplied and verified on the next start. No host PF or global firewall configuration was changed. To stop immediately, run `ops/worker/stop.sh`; this does not require guest cooperation. To roll back the launch-time policy, stop the VM first and restore reviewed launcher files from version control, then keep unattended startup disabled until an equivalent tested isolation policy is in place. Do not launch the worker directly with `tart run --net-softnet`, because that bypasses the policy wrapper. Phase 01 is verified for the documented current profile; unattended operation remains disabled.

## Phase 01.6 test coverage and release state

Current evidence is recorded in [the final Phase 01 report](../reports/phase-01-final.md). The current worker profile has one IPv4 interface/default route, no global IPv6 or ULA route, and UFW inactive. Owned TCP/UDP fixtures at the Softnet gateway and Mac Wi-Fi/VPN addresses were blocked; DNS, HTTPS, key-only SSH, controlled local rebind/redirect tests, and a scoped multicast test were exercised. A guest-local loopback redirect reaches the guest itself and is not evidence of host access; application-layer loopback/redirect policy must be tested in the future fetcher implementation.

Phase 01.7 replaces the unrestricted `@host:53` exception with a host-side DNS relay on TCP/UDP 55353. Softnet permits only that destination and port, while the relay validates DNS packets and sends upstream queries over TLS. The relay does not apply a domain allowlist: DNS names and supported query types can still encode data, so DNS-based exfiltration remains possible at the configured rate. Public DoH/DoT or other direct public-IP traffic may also bypass DNS because general public egress remains available. Do not describe the relay as a complete egress filter.

Owned IPv4 link-local (`169.254.253.1`) and metadata-shaped (`169.254.169.254`) endpoint tests passed twice using temporary `/32` aliases and listeners on the verified Tart bridge. The Mac received each listener marker; guest connections timed out over the expected `enp0s1` path with UFW inactive and no guest nftables/iptables DROP or REJECT rules. The aliases and exact routes were absent after worker stop, and reset left the worker stopped. No real metadata endpoint was contacted. IPv6 global/ULA traffic is not available in the current guest route table, which does not establish isolation for a future profile with IPv6 routing. Direct `tart run --net-softnet` bypasses policy setup and is not a protected launch; all supported repository start scripts use `softnet_launcher.py`, but the Tart CLI itself cannot be prevented from being invoked directly by a host operator.

The targeted checks for this implementation are `ops/worker/test-static.sh`, `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m pytest tests/personal_agent/test_worker_vm_contract.py tests/security/test_security_network.py tests/config/test_network_proxy.py -q`, `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync ruff check ops/worker/softnet_launcher.py tests/personal_agent/test_worker_vm_contract.py`, `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync basedpyright ops/worker/softnet_launcher.py tests/personal_agent/test_worker_vm_contract.py`, and `UV_CACHE_DIR=/private/tmp/toothless-uv-cache uv run --no-sync python -m scripts.check_import_boundaries`.

Phase 01 is verified for the documented current deployment profile; unattended execution remains disabled and Phase 02 remains no-go pending later runtime/recovery/evaluation gates and a decision on DNS egress semantics. The executed fixture steps and results are in the [Phase 01.6 report](../reports/phase-01-final.md) and [operator procedure](../reports/phase-01.6-fixture-procedure.md). Do not use a real cloud metadata endpoint, and do not launch Tart directly for a protected test.

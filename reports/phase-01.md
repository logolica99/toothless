# Phase 01 verification report

Date: 2026-11-16 (America/Los_Angeles)

Status: VERIFIED FOR THE CURRENT DEPLOYMENT PROFILE for the documented IPv4 worker and protected Softnet launch path. Owned IPv4 link-local and metadata-shaped fixtures passed twice, including after a protected worker restart. Future IPv6-enabled or alternate network profiles remain unverified; unattended autonomous execution and Phase 02 remain NO-GO. See `reports/phase-01-final.md` for evidence and limits.

## Implemented

- Pinned Tart 2.41.1, Softnet 0.25.0, and Ubuntu image choices.
- Installed the reviewed Softnet payload at a fixed internal path with an ad-hoc Apple-silicon signature, exact hash validation, root ownership, and setuid mode; the external SSD remains `nosuid`.
- Sanitized hardware discovery without stable device identifiers.
- Idempotent fixed-name provisioning and bounded 4-vCPU, 6-GB RAM, 20-GB disk settings.
- Host-side Softnet-only launcher with no directory, clipboard, USB, exposed-port, or private-network allowlist flags.
- Fail-closed Tart/Softnet control-socket launcher that installs a directional block for new guest-initiated flows to `@host`, with DNS-only TCP/UDP port 53 allowances.
- Fixed-target emergency stop and confirmation-gated clean reset.
- Static safety checks and an explicit operator install/rollback guide.

## Verification

| Check | Result |
| --- | --- |
| Host architecture and OS discovery | PASS: arm64 macOS 26.5.2 |
| Host RAM and CPU discovery | PASS: 16 GB, 10 logical CPUs |
| Static launcher/reset checks | PASS |
| Image reference pinned by digest | PASS |
| Dedicated worker storage | PASS: 127.8-GB APFS SSD at `/Volumes/toothless` |
| Free disk preflight | PASS: more than 30 GB available on dedicated SSD |
| Tart 2.41.1 installed | PASS: checksum and app signature verified |
| Softnet 0.25.0 installed | PASS: version, ad-hoc signature, signed SHA-256, `root:wheel`, and mode `4755` verified |
| Complete host preflight | PASS: run outside the host development sandbox because that sandbox intentionally blocks setuid execution |
| Real VM provisioning | PASS: base and worker created stopped on `/Volumes/toothless/Tools/Tart` |
| Resource bounds | PASS: both VMs report 4 CPUs, 6144 MB RAM, and 20-GB disks |
| SSD placement and capacity | PASS: Tart store present on SSD; 114 GiB available after provisioning |
| Fresh pinned-image reinstall | PASS: local worker, base, and OCI cache entry removed; exact digest downloaded again before recreation |
| Vendor bootstrap credential | PASS: one noninteractive check confirmed documented `admin/admin` on the freshly downloaded image |
| Persistent SSH hardening | PASS: root-owned persistent public-key path, passwords/root login/forwarding disabled, vendor password locked |
| Clean baseline seal | PASS: guest synced and powered off normally; sealer refused running-state input and recreated the worker |
| Post-clone key-only access | PASS: fresh clone accepted only the selected Ed25519 key and retained all effective `sshd` restrictions |
| Post-clone persistence | PASS: hardening config and authorized-key file remained nonempty after clean shutdown and clone |
| Runtime mounts and forwarded capabilities | PASS: no host share, SSH agent socket, or host Docker socket; forwarding remained disabled |
| Public HTTPS and package download | PASS: public HTTPS, package index, and package payload succeeded |
| Owned Mac Wi-Fi listener | PASS: blocked at `192.168.4.21` |
| Owned Mac CGNAT/VPN listener | PASS: blocked at `100.80.175.115` |
| Guest loopback isolation | PASS: guest loopback did not reach the Mac loopback listener |
| IPv6 public egress | PASS for current profile: no global IPv6 address/default route and public IPv6 connection failed |
| Softnet Mac gateway listener before Phase 01.4 | FAIL: guest reached owned listener at `10.36.57.193`; fixed by Phase 01.4 launcher policy |
| Softnet Mac gateway listener after Phase 01.4 | PASS: owned TCP and UDP gateway fixtures blocked across changing vmnet gateway addresses |
| Guest firewall independence after Phase 01.4 | PASS: gateway TCP/UDP remained blocked with UFW inactive |
| Host-initiated SSH management | PASS: key-only SSH remained usable after policy application and after reset |
| Public DNS, HTTPS, and package install | PASS: DNS worked through the gateway's TCP/UDP port 53 exception, HTTPS and apt succeeded |
| IPv6 owned gateway link-local listener | PASS: guest could not connect; guest had only IPv6 link-local state and no IPv6 default route |
| Softnet startup control failure behavior | PASS by implementation review and deterministic policy test: missing/rejected/unverified policy acknowledgement terminates Tart |
| Reset persistence | PASS: `/opt/toothless-reset-marker` and installed `sl` package were absent after fixed-target reset |
| Emergency stop | PASS: fixed-target stop halted a CPU-bound guest ignoring signals in 0.362 seconds |
| Post-emergency restore | PASS: worker was replaced from the sealed clean baseline and left stopped |
| Guest resource view | PASS: 4 CPUs, 6,043,416 kB reported memory, 20,000,000,000-byte disk |
| Host resource view | PASS: 4 CPUs, 6144 MB RAM, 20-GB disk |
| Controlled DNS rebinding simulation | PASS with limitation: guest-local DNS responder returned TEST-NET then the owned Mac Wi-Fi IPv4; connection to the private answer was blocked |
| HTTP redirect to owned Wi-Fi and gateway fixtures | PASS at the network layer: guest-local HTTP redirects to both owned Mac targets timed out; guest-loopback redirect reached only a guest-owned listener |
| IPv4-mapped IPv6 to owned Wi-Fi target | PASS: connection timed out |
| IPv4 link-local owned listener | PASS twice: Mac positive control returned its marker; guest route used `enp0s1` and connection timed out |
| IPv4 metadata-shaped owned listener | PASS twice: Mac positive control returned its marker; guest route used `enp0s1` and connection timed out; no real metadata endpoint contacted |
| Scoped IPv4 multicast fixture | PASS with limitation: a TTL-1 packet to the owned receiver on the Tart bridge was not received; this does not establish behavior for other multicast paths |
| IPv6 ULA and global route | NOT APPLICABLE to current guest route table: no ULA/global IPv6 route; future IPv6-enabled profiles remain unverified |
| Alternate guest interface/route | NOT APPLICABLE to current configuration: only `enp0s1` and one default route were present |

The host-gateway fix remains live-verified: the launcher obtains Softnet's supported JSON-RPC control channel, adds the stateful directional `out @host` block, retains TCP/UDP DNS port 53 exceptions to `@host`, applies the policy, then reads it back and refuses startup unless the acknowledged policy matches. Owned link-local and metadata-shaped endpoints passed twice with UFW inactive, no guest nftables/iptables DROP or REJECT rules, and working key-only SSH, DNS, and HTTPS controls. Local rebinding and redirect simulations confirm that tested private Mac endpoints remain unreachable after DNS resolution or HTTP redirects; they do not replace public owned-domain or application-layer tests. The exact `/32` fixtures were removed, no exact routes or listener ports remained, and the fixed-target reset left the worker stopped. No real metadata service was contacted. Current IPv6 has no global or ULA route; future IPv6 profiles remain unverified. Phase 01 is verified for the current deployment profile; Phase 02 remains no-go. See [phase-01-final.md](phase-01-final.md) for the complete matrix and command results.

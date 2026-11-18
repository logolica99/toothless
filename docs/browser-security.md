# Browser security boundary

## Status

Phase 05.3A verified guest-only Playwright 1.65.2 and Chromium 159.0.8154.6 ARM64. Phase 05.3B is VERIFIED for the current browser-only task profile behind systemd cgroup IP filtering, without changing Tart, Softnet, or macOS firewall configuration. The owned endpoint, public egress, browser rendering, redirects, controlled DNS-rebinding simulations, real Telegram Example Domain/Python research, restart recovery, duplicate-run prevention, and cleanup passed. A bounded cancellation stopped before the first browser tool call, so cancellation during active Chromium navigation remains unverified. Public egress and DNS-name exfiltration remain possible. See [`../reports/phase-05-03b.md`](../reports/phase-05-03b.md).

## Execution profile

The host selects a browser profile only for requests matching the browser/research cues in `nanobot.personal_agent.opencode_task.needs_browser_profile`. The host fixes the profile field in the encoded dispatch; Telegram text cannot choose a different OpenCode config. The root-owned guest runner verifies the exact `opencode-go/glm-5.3` model, OpenCode v2.3.0, browser MCP server shape, provider allowlist, and deny-by-default agent permissions from effective standalone config before every run. It does not fall back to the shell-enabled Phase 05.2 profile if browser config validation fails.

The browser agent allows only `toothless_browser_navigate`, `extract_text`, `click_link`, `click_button`, `screenshot`, `download`, and `close_session`. Shell, read/write file, edit, plugins, skills, subagents, arbitrary MCP servers, host tools, and package-manager privileges are denied. The MCP helper uses stdio, not an externally reachable port. The browser uses a fresh Playwright context and has no Mac cookies, host mount, SSH key, Telegram token, or host OpenCode credentials. The guest-local OpenCode credential remains accessible to the guest agent identity; browser or guest compromise could expose it.

## OS-enforced network policy

`ops/opencode/phase05/browser-network-policy.json` is installed root-owned at `/etc/toothless/browser-network-policy.json`, pinned by SHA-256 in the root-owned task runner, and passed as systemd `IPAddressAllow`/`IPAddressDeny` properties to a transient scope containing OpenCode and every descendant process. The scope caps memory at 1536 MiB, tasks at 128, and CPU at 200%. The process tree cannot write the cgroup or policy file and has no sudo; the browser profile also provides no shell tool. The BPF socket filter evaluates actual IPv4/IPv6 destination addresses, so the decision does not depend on a hostname staying at one DNS answer.

IPv4 denied ranges include unspecified, RFC1918, CGNAT, loopback, link-local, metadata-shaped, documentation, benchmarking, multicast, and reserved/broadcast ranges. All IPv6 is denied for this deployment profile. Only `127.0.0.53/32` and `127.0.0.54/32` are allowed to preserve the Ubuntu systemd-resolved stub. This is an address-level exception, not a port-level DNS sandbox: the current guest has DNS stub listeners on those IPs at TCP/UDP 53. The browser helper also rejects private and non-public URL answers, rechecks each browser request/redirect, blocks browser methods other than GET/HEAD, and uses its own bounded redirect-validated HTTP client for downloads. The kernel filter remains the security boundary if helper URL validation or DNS answers change.

Public IPv4 internet remains available to OpenCode and Chromium; this feature does not claim general egress isolation. A task, webpage, or model can still send information to public web servers, and DNS-name exfiltration remains possible through the approved guest resolver. Public encrypted DNS or other public services may also carry data. Browser task content is untrusted data; the browser agent prompt says not to follow page instructions or perform external actions, but the prompt is not an isolation control.

The browser helper rejects credential-bearing URLs, restricts navigation to HTTP(S), limits page text to 32,000 characters, uses 20-second Playwright timeouts, limits screenshots to 2 MiB and HTTP downloads to 10 MiB, and stores artifacts only below the guest workspace. The browser-profile OpenCode task has a separate 300-second guest runtime bound and 315-second host dispatch/reconciliation bound; non-browser tasks retain their existing 120-second guest and 150-second host bounds. Chromium-managed downloads are disabled so links cannot create unbounded downloads outside the explicit download tool. Account login, form submission, POST/PUT, purchase, publishing, and destructive operations are not supported. Buttons are limited to standalone non-form `type=button` controls; public GET endpoints may still have server-side effects, so research tasks must avoid external actions.

## Live boundary evidence

| Test | Result |
|---|---|
| Effective browser OpenCode config | PASS — exact model/provider, disabled updates, plugins empty, one local MCP server, deny-all tools except the seven browser tools |
| Rootless identity and policy immutability | PASS — UID 999, no sudo group/capability path, cgroup and root-owned policy are not writable by `toothless-agent` |
| Owned endpoint fixtures in guest loopback | PASS — 127.0.0.1, private IPv4, CGNAT, IPv4 link-local, metadata-shaped IPv4, IPv6 ULA, IPv6 link-local, and IPv4-mapped IPv6 listeners were first confirmed reachable by the authorized fixture process, then blocked from the filtered scope |
| Public DNS and HTTPS under the same filter | PASS — `www.python.org` resolved and `https://example.com/` returned HTTP 200 inside the filtered rootless scope |
| Browser public navigation and rendered content | PASS — real Chromium rendered `Example Domain` and the documentation body from example.com |
| JavaScript and button interaction | PASS — an owned in-memory page rendered the changed text after the allowed button click |
| Public link click | PASS — Playwright followed a link to example.com and returned its page title |
| Screenshot | PASS — guest workspace artifact `browser-artifacts/phase053b-runtime.png`, 74,622 bytes, PNG signature verified through the authenticated worker file interface |
| Direct private URL and owned redirect | PASS — private URL checks rejected loopback/private/link-local/metadata/ULA addresses; an owned 302 to a metadata-shaped destination did not load and navigation timed out inside the filtered scope, after which the browser closed cleanly |
| Controlled DNS rebinding simulation | PASS — the helper was given a simulated public validation answer while Chromium's owned host-resolver mapping selected the independently reachable metadata-shaped listener; the cgroup filter caused a timeout and the listener received no filtered connection |
| Browser close cleanup | PASS — the helper found no remaining Chromium process after close; its temporary files and profile were removed |
| Host and Softnet changes | PASS — no global firewall, Softnet rule, VM baseline, or Mac permission was changed |

The redirect and DNS-rebinding fixtures are local simulations. The rebinding test specifically verifies that a public-looking validation result cannot authorize a socket to the later private destination. No real cloud metadata service or unowned private service was contacted. Public browser release research through the actual owner Telegram workflow is a separate live acceptance gate.

## Operation and limitations

Start the VM only with `ops/worker/start.sh`. Direct Tart launches remain unprotected and must not be treated as a secure browser run. After VM reset, the guest-local browser packages and root-owned browser helper/configuration may be absent because Phase 05.3A changes were not sealed into a baseline; reinstall/reprovision the reviewed components with `ops/worker/install-opencode.sh` after confirming the guest-local browser versions, then rerun both effective-config and owned-fixture checks. Never copy the Mac browser profile, OpenCode auth directory, SSH key, or Telegram token into Ubuntu.

Browser file output remains inside the Ubuntu workspace and can be retrieved with the existing authenticated worker file interface. Phase 05.4 file attachments are limited to workspace-profile tasks; tasks with files are rejected before dispatch if they would select the browser-only profile. Browser and workspace shell tools are not combined. The temporary cgroup policy is scoped to one browser-profile task and does not change the normal worker network policy; non-browser shell-profile tasks retain their existing Phase 05.2 public-network authority. Unattended scheduling remains disabled. See [`file-security.md`](./file-security.md).

Reference documentation: [systemd resource control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html), [OpenCode MCP servers](https://opencode.ai/docs/mcp-servers/), and [Playwright Python](https://playwright.dev/python/docs/intro).

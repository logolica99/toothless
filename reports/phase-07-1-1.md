# Toothless — Phase 07.1.1: Private Remote Desktop and Memory Explorer

Date: 2026-11-19 (America/Los_Angeles)

Status: VERIFIED for owner-authorized MacBook Air and iPhone-over-cellular flows. The owner chose to retain the existing broad tailnet policy for everyone and explicitly approved private HTTPS routes. Tailscale Serve reports both routes as tailnet-only: `https://<mac-mini>.<tailnet>.ts.net/desktop` → `http://127.0.0.1:8769` and `/memory` → `http://127.0.0.1:8768`. No Funnel route, ACL/grant edit, router change, guest-network change, or public binding was made. Owner confirmed both apps, graph interaction, desktop refresh, and revocation on MacBook Air and iPhone using mobile data with Tailscale connected.

## Owner decision and network boundary

The reviewed Tailscale policy has a wildcard allow (`src: *`, `dst: *`, `ip: *`, all ports/protocols). The owner elected to keep it unchanged and allow all tailnet devices, including future members. The owner confirmed HTTPS certificates are enabled. Therefore tailnet membership grants network reachability to these routes; it is not application authorization. Device names in HTTPS certificate issuance may appear in public Certificate Transparency logs. Serve status labels the configured hostname/routes “tailnet only.” No Funnel command was run. Host backends continue listening on loopback only; ports 8768 and 8769 are not directly bound to LAN interfaces.

## Application authentication and data boundaries

`/remote` is a private-chat, owner-authorized Telegram command. It starts/supervises the host Memory Explorer in managed mode and issues a one-use memory sign-in code. The code is stored hashed in the owner-private remote-session SQLite store and exchanged at `/memory/authorize` for a short-lived HttpOnly, SameSite, Secure cookie. The Explorer opens MemoryStore/TaskStore read-only. It does not accept the local bearer token from a remote-origin request and does not rely on Tailscale identity headers.

The desktop URL is issued only after an owner-bound one-use Telegram callback and full protected worker readiness. The viewer accepts a one-use code and issues a short-lived HttpOnly, SameSite, Secure cookie scoped to `/desktop/`. It only polls fixed image frames and has no mouse, keyboard, clipboard, shell, upload, file-transfer, or WebSocket operation. `/remote revoke` invalidates outstanding remote grants/sessions and closes the desktop viewer. Controller startup advances the session epoch, invalidating previous codes and cookies. Memory and desktop sessions are separate scopes in the same host-owned session store.

Both applications remain bound to `127.0.0.1`, enforce the configured HTTPS Host/Origin, and support the Tailscale Serve path prefix whether the proxy preserves or strips that prefix. All application actions still require app-level authentication. The Ubuntu VM and its Softnet network are not part of the Memory Explorer route; desktop access fails closed when protected worker readiness is unavailable.

## Deployment evidence

The approved commands configured the routes:

```text
tailscale serve --bg --https=443 --set-path=/desktop http://127.0.0.1:8769
tailscale serve --bg --https=443 --set-path=/memory http://127.0.0.1:8768
```

The second command reported both routes and “Serve started and running in the background.” A subsequent `tailscale serve status` showed:

```text
https://<mac-mini>.<tailnet>.ts.net (tailnet only)
|-- /memory  proxy http://127.0.0.1:8768
|-- /desktop proxy http://127.0.0.1:8769
```

The unprivileged CLI could not load Tailscale preferences; the verified status command required the authorized elevated CLI context. No token or authentication code was printed in command output. App services remain loopback-only by implementation. Route configuration itself does not prove that either application is currently listening or that a remote browser can authenticate.

After protected worker startup, `worker_readiness()` reported every field ready: running VM, verified Softnet policy, ready relay and guest DNS, guest OpenCode Go authentication, execution helper, and both profiles. After the owner closed the desktop session, the Memory Explorer remained as the only app listener on `127.0.0.1:8768`; port 8769 had no listener, as expected for an on-demand viewer. The unauthenticated live HTTPS graph API probe returned HTTP 401. The controller LaunchAgent remained running as one poller. The owner confirmed both application flows on MacBook Air and iPhone over mobile data with Tailscale connected.

The owner subsequently confirmed the code was accepted but the graph appeared empty. Inspection found a stale JavaScript guard referencing undeclared `authToken`; the UI had moved to cookie auth, so `refreshGraph()` threw before requesting any graph data. The guard was removed. The host memory database contains the `mechanochemistry` project, one current saved fact, a task reference, and linked task/file/artifact records; a live read-only lexical query returns the project, memory, task, `number.csv`, `chart.png`, and `summary.csv`. Automated tests now seed the same kind of project/fact and verify they appear through both prefix-preserved and stripped authenticated graph routes. After revocation, the HTTPS route and sign-in page intentionally remain reachable to tailnet devices, but protected graph APIs reject the old cookie. The UI now checks authorization every 10 seconds and on window focus/tab return, hiding graph access when the server returns 401. Requesting `/remote` creates a new one-use code; the inline “New Memory sign-in code” button is bound to a single Telegram card and must not be reused after consumption/expiry. The focused Memory Explorer/session suite passed (8 tests), Ruff passed, and the controller was restarted after the final changes. The owner must obtain a fresh code and verify graph visibility plus revocation from a real device; these fixes are not counted as live acceptance until that succeeds.

## Automated verification

Focused local tests passed before route deployment: 62 passed across `test_remote_sessions.py`, `test_desktop_viewer.py`, `test_memory_explorer.py`, and `test_telegram_controller.py`. Ruff passed for the changed Python files and `compileall` passed. These tests cover owner-created one-use session codes, single-use consumption, expiry/revocation/controller epoch, secure cookie scope, remote Host/Origin checks, prefixed and prefix-stripped route handling, owner-only `/remote`, and absence of desktop input operations. These are local backend tests, not live remote-device acceptance. BasedPyright was unavailable in the active environment and is NOT RUN.

## Acceptance status

| Check | Result | Evidence |
| --- | --- | --- |
| Owner review of HTTPS and broad tailnet policy | PASS | User confirmed HTTPS is enabled and elected to retain the wildcard policy for everyone. |
| Tailscale Serve routes configured | PASS | CLI status reports exactly `/desktop` → 127.0.0.1:8769 and `/memory` → 127.0.0.1:8768, tailnet-only. |
| Funnel/public route configured | PASS (not configured) | No Funnel command or route was used. Router state was not inspected. |
| App-level one-time authentication, owner gate, remote path handling | PASS (local tests) | 62 focused tests passed; these do not substitute for remote browser tests. |
| Remote Memory Explorer sign-in and graph search from MacBook Air | PASS (owner-confirmed) | Owner reported successful graph display and revocation on MacBook Air. Host data inspection confirms mechanochemistry has current project/fact and task/file/artifact context. |
| Remote application revoke | PASS (owner-confirmed for both applications) | Owner confirmed old graph session and desktop frames stopped working after revoke/close. HTTPS routes and the sign-in page remain reachable within the tailnet by design. |
| Remote desktop sign-in and image refresh from MacBook Air | PASS (owner-confirmed) | Owner reported that the frame appeared and the desktop flow worked, then closed/revoked it. |
| Controller-restart invalidation through remote route | PASS (local tests); NOT RUN live | Controller restart invalidated old codes during deployment; route-level owner recheck remains unrecorded. |
| LaunchAgent singleton after restart | PASS | `launchctl` reported the controller running; process inspection found one Telegram poller. |
| Unauthorized tailnet member blocked at network layer | NOT APPLICABLE | Owner explicitly requested all tailnet devices/users remain allowed. Application authentication remains required. |
| Another computer acceptance | PASS (owner-confirmed) | MacBook Air successfully tested Memory Explorer and desktop viewing. |
| iPhone acceptance over cellular with Tailscale connected | PASS (owner-confirmed) | Owner confirmed Memory Explorer graph search/node inspection, desktop frame viewing/refresh, and revoke using mobile data with Tailscale connected. |
| Non-owner tailnet identity cannot authenticate | NOT RUN | A separate non-owner tailnet account was not available/reported; broad network reachability remains intentional. |
| Memory Explorer API read-only enforcement | PASS (existing design/local tests) | SQLite stores are opened read-only; remote session grants no write operation. |
| Worker readiness and guest networking | PASS | The stopped VM was started only through `ops/worker/start.sh`; Softnet, relay DNS, guest DNS, authentication, helper, and profiles all passed readiness. No policy/configuration was changed. |

## Required owner acceptance and remaining limitations

The owner confirmed successful Memory Explorer and remote desktop viewing, frame refresh, and revocation on MacBook Air and iPhone over mobile data with Tailscale connected. No separate non-owner tailnet account was available for the negative authentication check; automated tests verify missing/invalid codes and reject remote bearer-token authentication. Do not paste either code into this conversation.

Phase 07.1.1 is VERIFIED for the specified owner devices and read-only workflows. Remaining limitation: the negative authentication check against a separate non-owner tailnet identity was not performed because no such account was available. The broad tailnet policy intentionally makes all tailnet members network-eligible, but app data still requires the owner-issued one-time code. Remote access depends on the controller and local services being available. There is no public internet access, Funnel, LAN binding, desktop input, manual takeover, or AI GUI control. The graphical Firefox browser remains non-operational, and headless OpenCode browser activity is not visible in this viewer.

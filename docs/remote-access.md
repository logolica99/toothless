# Toothless private remote access

Phase 07.1.1 routes are configured and owner-device live acceptance passed on MacBook Air and iPhone using mobile data with Tailscale connected. The controller reads the private `TOOTHLESS_REMOTE_ORIGIN` setting from `~/.config/toothless/telegram.env`; set it to the HTTPS Tailscale Serve origin only (for example `https://<mac-mini>.<tailnet>.ts.net`, without a path). The owner chose to retain the visible broad tailnet rule (`src: *`, `dst: *`, `ip: *`, all ports/protocols) for all tailnet users/devices, including future members/devices. The owner also confirmed HTTPS certificates are enabled (the admin console displayed “Disable HTTPS…”). This is tailnet access, not public internet access; certificate device names are published in public Certificate Transparency logs.

## Current application boundaries

The desktop viewer is an image-only host service bound to `127.0.0.1:8769`; it uses an owner-authorized one-use code, a short-lived HttpOnly/SameSite/Secure cookie, and controller-owned revocation/expiry. The Memory Explorer is a separate read-only host service bound to `127.0.0.1:8768`; remote sign-in exchanges a Telegram-issued one-use code for a short-lived HttpOnly/SameSite/Secure cookie. Both applications retain their localhost-only peer checks and accept only the configured remote Host/Origin when proxied by Serve; Tailscale identity headers are not treated as application authentication. Controller restart advances the shared remote-session epoch and invalidates prior codes/sessions.

The app backends must remain bound exclusively to localhost. A Tailscale Serve URL alone must not authorize application access. Never pass persistent bearer tokens in Telegram URLs, query strings, browser JavaScript bundles, or third-party requests. Do not trust arbitrary forwarded headers; any future identity-header use must be restricted to requests arriving from the local Serve proxy and is not a replacement for the app's own authentication.

## Owner preflight and approval

The local CLI is working in the current owner session. Do not paste credentials or unrelated peer details into Telegram or this repository.

The owner approved retaining the current broad tailnet policy for everyone and the HTTPS-certificate setting; no ACL/grant or certificate changes were made. Tailscale grants are additive, and the existing wildcard allow means all tailnet users/devices can reach these served routes unless the policy is later changed by the owner. Application access still requires an independent one-time code delivered through the authorized private Telegram chat.

Configured routes (2026-11-19): `TOOTHLESS_REMOTE_ORIGIN/desktop/` → `http://127.0.0.1:8769` and `TOOTHLESS_REMOTE_ORIGIN/memory/` → `http://127.0.0.1:8768`. `tailscale serve status` reports both as “tailnet only.” No Funnel route was configured. The application base-path, origin, and one-time-code session handling have focused automated coverage, and owner device acceptance passed on MacBook Air and iPhone over mobile data with Tailscale connected. Do not expose ports 8768/8769 directly to LAN or internet. See [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve), [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve), [grants](https://tailscale.com/docs/features/access-control/grants), and [HTTPS certificates](https://tailscale.com/docs/how-to/set-up-https-certificates).

The installed Serve path behavior is supported by application base-path handling for both prefix-preserving and prefix-stripped proxy paths. Backends remain loopback-only. Use only the configured Tailscale Serve routes; never use Funnel. Do not expose ports 8768/8769 directly to LAN or internet.

## Device revocation and application revocation

Because the owner retained a broad wildcard tailnet policy, all tailnet members/devices can reach the HTTPS route at the network layer. If a device or user should no longer have network access, remove/disable it from the tailnet and review the policy in the admin console. `/remote revoke` invalidates application-issued one-time codes and sessions and closes the desktop viewer; it does not change Tailscale membership or policy. The public sign-in page remains reachable after revocation, but protected graph API requests are denied; the browser checks authorization periodically and on focus, then hides the graph and requests a fresh Telegram code. A controller restart also invalidates all outstanding remote sessions. Memory Explorer data remains read-only even for an authenticated viewer.

## Current verification state

Serve route configuration and local authorization tests are PASS. The owner confirmed Memory Explorer sign-in, mechanochemistry graph inspection, desktop frame refresh, and revocation from MacBook Air and iPhone over mobile data with Tailscale connected. Phase 07.1.1 is VERIFIED for these owner devices. A live negative check with a separate non-owner tailnet identity remains NOT RUN because no such account was available; automated invalid/absent-code and bearer-token rejection tests pass. See `reports/phase-07-1-1.md`.

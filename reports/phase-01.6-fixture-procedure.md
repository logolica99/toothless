# Phase 01.6 temporary IPv4 fixture procedure

Status: EXECUTED 2026-11-16 after operator approval. Temporary aliases and listeners were removed; no exact fixture routes or address aliases remain, and the worker was reset and left stopped.

## Fixture and safety

macOS has no Linux network namespace for attaching a disposable address to the Tart host-side network. The fixture assigned only two `/32` aliases to the live Tart Softnet bridge: `169.254.253.1` (link-local) and `169.254.169.254` (metadata-shaped). Temporary TCP listeners bound only to those local addresses. The metadata-shaped endpoint was a socket owned by the Mac on the verified Tart bridge; no cloud metadata service was contacted.

Each alias changes only its exact local address/host route. It adds no `169.254/16` route, PF rule, persistent service, or guest route. Unrelated existing link-local routes are allowed; only an exact route/address collision aborts. Tart may remove the bridge and aliases when stopped; cleanup still checks and removes them if the bridge remains.

The bridge was identified live by `vmenet0` membership, ownership of the guest-reported gateway IP, and the host route to the guest IP using that same interface; its name was `bridge100` on both rounds. After each worker restart the gateway address changed and was rechecked. macOS reports `lo0` for `route -n get` to an address assigned locally; this is expected and does not indicate the alias was assigned to the wrong interface. Bridge ownership was verified directly with `ifconfig bridge100`. Record a denial only if the Mac independently reaches each listener and receives its marker, the guest routes targets through its expected `enp0s1` default route, the Softnet launcher acknowledges policy, UFW is inactive, and DNS/HTTPS positive controls pass. Softnet exposes no per-packet denial counter; report boundary behavior, not packet-drop telemetry.

## Commands used and observed results

The following command blocks record the procedure used. The operator ran both administrator-required alias additions locally; no password was requested or stored. A stale already-running worker first caused the protected launcher to fail closed; the fixed worker was stopped before retrying, and each subsequent protected start acknowledged the policy.

### 1. Start protected worker

Terminal A:

```bash
cd /Users/jubaerjami/Documents/always-on-agent
ops/worker/start.sh
```

Expected: preflight passes and output includes `Softnet isolation acknowledged: blocks new guest flows to @host; 1 block and 2 allow rule(s).` If not, stop here.

### 2. Identify the bridge and check exact-target conflicts

Terminal B; keep this shell open. SSH must verify the existing host key. If it fails, verify the fingerprint through the trusted console; never disable host-key checking.

```bash
cd /Users/jubaerjami/Documents/always-on-agent
source ops/worker/config.sh
tart list
WORKER_IP="$(tart ip "$WORKER_VM_NAME")"
GUEST_GATEWAY="$(ssh -i /Users/jubaerjami/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes "admin@$WORKER_IP" 'ip -4 route show default' | awk '$1 == "default" {print $3; exit}')"
test -n "$WORKER_IP" && test -n "$GUEST_GATEWAY"
BRIDGE=""
for candidate in $(ifconfig -l); do
  info="$(ifconfig "$candidate" 2>/dev/null || true)"
  if printf '%s\n' "$info" | grep -q 'member: vmenet0' && printf '%s\n' "$info" | grep -Fq "inet $GUEST_GATEWAY "; then
    if test -n "$BRIDGE"; then echo 'Multiple bridge candidates; stop.' >&2; exit 1; fi
    BRIDGE="$candidate"
  fi
done
test -n "$BRIDGE" || { echo 'No verified Tart bridge; stop.' >&2; exit 1; }
route -n get "$WORKER_IP" | grep -F "interface: $BRIDGE"
printf 'Verified bridge=%s worker=%s gateway=%s\n' "$BRIDGE" "$WORKER_IP" "$GUEST_GATEWAY"
for address in 169.254.253.1 169.254.169.254; do
  if netstat -rn -f inet | awk -v target="$address" '$1 == target {found=1} END {exit found ? 0 : 1}'; then echo "Exact route exists for $address; stop." >&2; exit 1; fi
  if ifconfig -a | awk -v target="$address" '$1 == "inet" && $2 == target {found=1} END {exit found ? 0 : 1}'; then echo "Address exists for $address; stop." >&2; exit 1; fi
done
```

Expected: worker is `running`; guest default uses `enp0s1`; selected host interface has `member: vmenet0` and `inet $GUEST_GATEWAY`; route to `$WORKER_IP` uses `$BRIDGE`. Exact-target checks intentionally ignore unrelated `169.254/16` routes.

### 3. Add two exact aliases

These `sudo` commands change only the verified bridge and install two exact `/32` addresses. macOS may show a local route via `lo0`; verify address ownership on `$BRIDGE` and record `route -n get` output.

```bash
set -e
LINKLOCAL=169.254.253.1
METADATA=169.254.169.254
sudo /sbin/ifconfig "$BRIDGE" inet "$LINKLOCAL" netmask 255.255.255.255 alias
sudo /sbin/ifconfig "$BRIDGE" inet "$METADATA" netmask 255.255.255.255 alias
ifconfig "$BRIDGE" | grep -F "inet $LINKLOCAL "
ifconfig "$BRIDGE" | grep -F "inet $METADATA "
route -n get "$LINKLOCAL"
route -n get "$METADATA"
```

If an alias or ownership check fails, do not probe; go to cleanup.

### 4. Start owned TCP listeners and verify them from the Mac

Terminal C; stop with `Ctrl+C`. Listeners bind only the two aliases and ports 18087/18088, log accepted connections, and return unique markers; no persistent service is installed.

```bash
python3 -u - <<'PY'
import socket
import threading

def listen(address, port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((address, port))
    server.listen()
    print(f"LISTENING {address}:{port}", flush=True)
    while True:
        connection, peer = server.accept()
        print(f"ACCEPTED {address}:{port} from {peer}", flush=True)
        with connection:
            connection.sendall(f"owned-fixture:{address}:{port}\n".encode())

for address, port in (("169.254.253.1", 18087), ("169.254.169.254", 18088)):
    threading.Thread(target=listen, args=(address, port), daemon=True).start()
threading.Event().wait()
PY
```

Host-side positive control (both markers must print before guest tests):

```bash
python3 - <<'PY'
import socket

for address, port in (("169.254.253.1", 18087), ("169.254.169.254", 18088)):
    with socket.create_connection((address, port), timeout=2) as connection:
        marker = connection.recv(128).decode().strip()
        print(marker)
        if marker != f"owned-fixture:{address}:{port}":
            raise SystemExit(f"wrong listener response for {address}:{port}")
PY
```

Expected: one host-side marker and one `ACCEPTED` line per listener. If either marker fails, guest results are NOT RUN. Note the current accepted-line count; no additional accept should occur during guest probes.

### 5. Probe as guest root and verify firewall/route

UFW must be inactive, and no guest nftables/iptables DROP or REJECT rule may be present; if a check fails, do not change the guest firewall and mark NOT RUN. The probe runs as guest root and requires an `enp0s1` default route to each target. Success is FAIL. Timeout/reset after the fixture, route, and policy positive controls pass means the guest could not establish a connection. Any other socket error is NOT RUN.

```bash
ssh -i /Users/jubaerjami/.ssh/id_ed25519 \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "admin@$WORKER_IP" sh -s <<'SH'
set -eu
sudo -n ufw status | grep -F 'Status: inactive'
command -v nft >/dev/null || { echo 'Cannot inspect nftables; NOT RUN.' >&2; exit 2; }
command -v iptables-save >/dev/null || { echo 'Cannot inspect iptables; NOT RUN.' >&2; exit 2; }
if sudo -n nft list ruleset | grep -E '(^|[^[:alpha:]])(drop|reject)([^[:alpha:]]|$)'; then echo 'Guest nftables DROP/REJECT rule present; NOT RUN.' >&2; exit 2; fi
if sudo -n iptables-save | grep -E '(^-P [A-Z_]+ DROP| -j (DROP|REJECT)( |$))'; then echo 'Guest iptables DROP/REJECT rule present; NOT RUN.' >&2; exit 2; fi
sudo -n python3 - <<'PY'
import socket
import subprocess

for address, port in (("169.254.253.1", 18087), ("169.254.169.254", 18088)):
    route = subprocess.run(["ip", "route", "get", address], text=True, capture_output=True)
    print(f"ROUTE {address}: {route.stdout.strip() or route.stderr.strip()}")
    if route.returncode or "via " not in route.stdout or "dev enp0s1" not in route.stdout:
        print(f"NOT RUN {address}: no expected enp0s1 default route")
        raise SystemExit(2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect((address, port))
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError) as error:
        print(f"DENIED {address}:{port}: {type(error).__name__}")
    except OSError as error:
        print(f"NOT RUN {address}:{port}: {error}")
        raise SystemExit(2)
    else:
        print(f"FAIL connection established to {address}:{port}")
        raise SystemExit(1)
    finally:
        sock.close()
PY
SH
```

The host marker validates the fixture; guest route plus working public controls validates the VM path; launcher acknowledgement validates the configured external policy. Softnet has no per-packet denial counter here, so do not claim packet-drop telemetry.

### 6. Verify key-only SSH, DNS, and HTTPS

The SSH command itself confirms host-initiated management. Expected: UFW inactive, UDP/TCP DNS answers, and HTTPS 200.

```bash
ssh -i /Users/jubaerjami/.ssh/id_ed25519 \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "admin@$WORKER_IP" sh -s <<'SH'
set -eu
sudo -n ufw status | grep -F 'Status: inactive'
gateway="$(ip route | awk '$1 == "default" {print $3; exit}')"
dig "@$gateway" example.com +time=2 +tries=1 +short
dig +tcp "@$gateway" example.com +time=2 +tries=1 +short
curl -4 -sS -o /dev/null -w 'HTTPS %{http_code}\n' --max-time 10 https://example.com
SH
```

### 7. Repeat after worker restart

Stop the fixed worker and listener; start only with `ops/worker/start.sh` and require policy acknowledgement. Tart may recreate its bridge. Repeat bridge identification, conflict checks, alias additions, listener/host checks, guest probes, and public controls with freshly computed variables. Never use direct `tart run`.

```bash
cd /Users/jubaerjami/Documents/always-on-agent
ops/worker/stop.sh
# In Terminal C, press Ctrl+C to stop the old listener.
ops/worker/start.sh
```

### 8. Cleanup and rollback, including after any failure

Cleanup is manual and operator-confirmed, not an automatic trap. Stop the fixed worker and listener. If the verified bridge remains, remove only these aliases; if Tart destroyed it, they disappeared with it. Verify exact addresses/routes are absent, while ignoring unrelated link-local routes. Then reset and verify the worker is stopped.

```bash
cd /Users/jubaerjami/Documents/always-on-agent
ops/worker/stop.sh
# In Terminal C, press Ctrl+C to stop the listener.
if test -n "$BRIDGE" && ifconfig "$BRIDGE" >/dev/null 2>&1; then
  sudo /sbin/ifconfig "$BRIDGE" inet 169.254.253.1 -alias
  sudo /sbin/ifconfig "$BRIDGE" inet 169.254.169.254 -alias
fi
for address in 169.254.253.1 169.254.169.254; do
  if ifconfig -a | awk -v target="$address" '$1 == "inet" && $2 == target {found=1} END {exit found ? 0 : 1}'; then
    echo "Alias remains for $address; stop and inspect." >&2
    exit 1
  fi
  if netstat -rn -f inet | awk -v target="$address" '$1 == target {found=1} END {exit found ? 0 : 1}'; then
    echo "Exact route remains for $address; stop and inspect." >&2
    exit 1
  fi
done
printf 'RESET-WORKER\n' | ops/worker/reset.sh
source ops/worker/config.sh
tart list
```

Expected: no exact fixture address/route remains; reset completes; `tart list` shows `toothless-worker` stopped. If alias removal fails, do not reset/restart; inspect the verified interface and remove only those two addresses. No PF/global firewall command is used.

## Approval boundary

The operator approved the fixture procedure and ran all `sudo ifconfig` alias additions locally; no password was requested or stored. The protected worker was stopped and restarted through repository scripts. Two rounds passed for both owned endpoints. The listener processes were stopped; Tart removed the aliases when the bridge stopped. Read-only checks confirmed the aliases and exact routes were absent, and the pre-existing `169.254.0.0/16` route via `en1` remained. The fixed-target reset completed with both worker and base stopped. Phase 01 is verified for the current deployment profile; Phase 02 remains NO-GO. See [phase-01-final.md](phase-01-final.md) for the test matrix and regression results.

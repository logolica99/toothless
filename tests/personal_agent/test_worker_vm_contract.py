import json
import socket
import threading
from pathlib import Path

WORKER_OPS = Path("ops/worker")


def test_softnet_startup_policy_blocks_only_new_host_initiations() -> None:
    from ops.worker.softnet_launcher import apply_host_isolation_policy

    controller, peer = socket.socketpair()
    replies = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "allow": ["out proto udp @host port 53", "out proto tcp @host port 53"],
                "block": ["10.0.0.0/8"],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "allow": ["out proto tcp @host port 55353", "out proto udp @host port 55353"],
                "block": ["10.0.0.0/8", "out @host"],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "allow": ["out proto tcp @host port 55353", "out proto udp @host port 55353"],
                "block": ["10.0.0.0/8", "out @host"],
            },
        },
    ]

    def serve() -> None:
        for response in replies:
            request = json.loads(peer.makefile("rb").readline())
            assert request["method"] in {"softnet.policy.get", "softnet.policy.set"}
            peer.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        policy = apply_host_isolation_policy(controller)
    finally:
        controller.close()
        peer.close()
    thread.join(timeout=1)

    assert policy["allow"] == ["out proto tcp @host port 55353", "out proto udp @host port 55353"]
    assert "out proto udp @host port 53" not in policy["allow"]
    assert "out proto tcp @host port 53" not in policy["allow"]
    assert policy["block"] == ["10.0.0.0/8", "out @host"]


def test_worker_image_is_digest_pinned() -> None:
    config = (WORKER_OPS / "config.sh").read_text(encoding="utf-8")

    assert "ghcr.io/cirruslabs/ubuntu@sha256:" in config
    assert 'WORKER_DISK_GB="20"' in config
    assert 'WORKER_MIN_FREE_GB="30"' in config
    assert 'WORKER_STORAGE_VOLUME="/Volumes/toothless"' in config
    assert 'WORKER_STORAGE_ROOT="$WORKER_STORAGE_VOLUME/Tools"' in config
    assert 'TART_HOME="$WORKER_STORAGE_ROOT/Tart"' in config
    assert 'REQUIRED_SOFTNET_PATH="/usr/local/libexec/toothless/softnet"' in config
    assert 'PATH="/usr/local/bin:$WORKER_TOOLS_BIN:$PATH"' in config
    assert 'WORKER_TOOLS_BIN="$WORKER_STORAGE_VOLUME/Tools/bin"' in config


def test_worker_start_always_uses_softnet_without_host_shares() -> None:
    start = (WORKER_OPS / "start.sh").read_text(encoding="utf-8")
    assert 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" headless' in start

    console = (WORKER_OPS / "start-console.sh").read_text(encoding="utf-8")
    assert 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" console' in console

    serial = (WORKER_OPS / "start-serial.sh").read_text(encoding="utf-8")
    assert 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" serial' in serial

    launch_agent = (Path("ops/macos/install-controller-launch-agent.sh")).read_text(encoding="utf-8")
    assert "com.toothless.telegram-controller" in launch_agent
    assert "ThrottleInterval" in launch_agent and "KeepAlive" in launch_agent
    assert "TOOTHLESS_TELEGRAM_BOT_TOKEN" not in launch_agent


def test_visible_browser_authorization_runtime_has_boot_and_service_start_creation() -> None:
    desktop_unit = (WORKER_OPS / "toothless-desktop.service").read_text(encoding="utf-8")
    auth_unit = (WORKER_OPS / "toothless-browser-auth-dir.service").read_text(encoding="utf-8")
    tmpfiles = (WORKER_OPS / "toothless-browser-authorizations.conf").read_text(encoding="utf-8")
    installer = (WORKER_OPS / "guest_install_visible_browser.sh").read_text(encoding="utf-8")
    transfer = (WORKER_OPS / "install-visible-browser-runtime.sh").read_text(encoding="utf-8")

    assert "Requires=toothless-browser-auth-dir.service" in desktop_unit
    assert "After=network-online.target toothless-browser-auth-dir.service" in desktop_unit
    assert "Type=oneshot" in auth_unit
    assert "systemd-tmpfiles --create /etc/tmpfiles.d/toothless-browser-authorizations.conf" in auth_unit
    assert tmpfiles.strip() == "d /run/toothless-browser-authorizations 0750 root toothless-browser -"
    assert "toothless-browser-authorizations.conf" in installer
    assert "toothless-browser-auth-dir.service" in installer
    assert "systemctl start toothless-browser-auth-dir.service" in installer
    assert "toothless-browser-authorizations.conf" in transfer
    assert "toothless-browser-auth-dir.service" in transfer


def test_worker_readiness_requires_fresh_protected_marker_and_guest_checks(monkeypatch, tmp_path) -> None:
    import json
    import os
    import time
    from subprocess import CompletedProcess

    from nanobot.personal_agent import worker_readiness as readiness

    marker_path = tmp_path / "protected-launch.json"
    marker_path.write_text(json.dumps({
        "pid": os.getpid(), "heartbeat": time.time(), "policy_verified": True,
        "dns_relay_ready": True, "guest_dns_verified": True,
    }), encoding="utf-8")
    marker_path.chmod(0o600)
    monkeypatch.setattr(readiness, "MARKER", marker_path)

    def run(args: list[str], *, timeout: float = 5) -> CompletedProcess[str]:
        if args[:3] == ["tart", "get", readiness.WORKER_NAME]:
            return CompletedProcess(args, 0, "Name: worker\nState: running\n", "")
        return CompletedProcess(args, 0, json.dumps({
            "opencode_auth": "ready", "execution_agent": "ready", "guest_dns": "ready",
            "workspace_profile": "ready", "browser_profile": "ready",
        }), "")

    monkeypatch.setattr(readiness, "_run", run)
    result = readiness.worker_readiness()
    assert result["worker_ready"] is True
    assert result["softnet_policy"] == "verified"

    marker_path.write_text(json.dumps({
        "pid": os.getpid(), "heartbeat": time.time() - 60, "policy_verified": True,
        "dns_relay_ready": True, "guest_dns_verified": True,
    }), encoding="utf-8")
    stale = readiness.worker_readiness()
    assert stale["worker_ready"] is False
    assert stale["softnet_policy"] == "unverified"


def test_protected_start_reapplies_dns_helper_and_verifies_dynamic_relay(monkeypatch) -> None:
    from ops.worker import softnet_launcher

    calls: list[list[str]] = []

    def capture(args: list[str], *, timeout: float = 2.0) -> str:
        calls.append(args)
        if args[-1] == softnet_launcher.GUEST_DNS_HELPER:
            return "Trusted DNS relay configured: 172.30.249.129:55353"
        if args[-2:] == ["status", "enp0s1"]:
            return "Current DNS Server: 172.30.249.129:55353"
        if args[-2:] == ["ahostsv4", "opencode.ai"]:
            return "104.18.10.66 STREAM opencode.ai"
        raise AssertionError(f"Unexpected guest readiness command: {args}")

    monkeypatch.setattr(softnet_launcher, "_capture", capture)
    softnet_launcher.configure_and_verify_guest_dns()

    assert calls[0] == [
        "tart", "exec", "toothless-worker", "sudo", "-n",
        "/usr/local/libexec/toothless/configure-dns-client",
    ]
    assert calls[1] == ["tart", "exec", "toothless-worker", "resolvectl", "status", "enp0s1"]
    assert calls[2] == ["tart", "exec", "toothless-worker", "getent", "ahostsv4", "opencode.ai"]


def test_protected_start_dns_verification_fails_closed_on_wrong_relay(monkeypatch) -> None:
    import pytest

    from ops.worker import softnet_launcher

    def capture(args: list[str], *, timeout: float = 2.0) -> str:
        if args[-1] == softnet_launcher.GUEST_DNS_HELPER:
            return "Trusted DNS relay configured: 172.30.249.129:55353"
        return "Current DNS Server: 192.0.2.53"

    monkeypatch.setattr(softnet_launcher, "_capture", capture)
    with pytest.raises(softnet_launcher.SoftnetControlError, match="could not be reapplied and verified"):
        softnet_launcher.configure_and_verify_guest_dns()


def test_protected_start_reports_resolver_and_direct_relay_after_lookup_timeout(monkeypatch) -> None:
    import subprocess

    import pytest

    from ops.worker import softnet_launcher

    def capture(args: list[str], *, timeout: float = 2.0) -> str:
        if args[-1] == softnet_launcher.GUEST_DNS_HELPER:
            return "Trusted DNS relay configured: 172.30.249.129:55353"
        if args[-2:] == ["status", "enp0s1"]:
            return "Current DNS Server: 172.30.249.129:55353"
        if args[-2:] == ["log-level", "debug"]:
            return ""
        if args[-2:] == ["ahostsv4", "opencode.ai"]:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[-4:] == ["resolvectl", "query", "--type=A", "opencode.ai"]:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[-3:] == ["--no-pager", "-n", "80"]:
            return "Using DNS server 172.30.249.129:55353; DNS query timed out"
        if args[-3:] == ["readlink", "-f", "/etc/resolv.conf"]:
            return "/run/systemd/resolve/stub-resolv.conf"
        if args[-3] == softnet_launcher.GUEST_DNS_RELAY_PROBE:
            return "direct guest relay probe: rcode=0 answers=2"
        raise AssertionError(f"Unexpected diagnostic command: {args}")

    monkeypatch.setattr(softnet_launcher, "_capture", capture)
    with pytest.raises(softnet_launcher.SoftnetControlError, match="resolvectl timed out; /run/systemd/resolve/stub-resolv.conf; resolver log: server-selections=1, timeouts=1, failures=0; direct guest relay probe: rcode=0 answers=2"):
        softnet_launcher.configure_and_verify_guest_dns()


def test_run_worker_stops_tart_when_guest_dns_readiness_fails(monkeypatch, capsys) -> None:

    from ops.worker import softnet_launcher

    controller, guest = socket.socketpair()

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout: float = 3.0):
            return self.returncode

        def kill(self):
            self.returncode = -9

    vm = FakeProcess()

    class ExitedDnsProcess:
        stdout = None

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(softnet_launcher.socket, "socketpair", lambda: (controller, guest))
    monkeypatch.setattr(softnet_launcher.subprocess, "Popen", lambda *args, **kwargs: vm)
    monkeypatch.setattr(softnet_launcher, "apply_host_isolation_policy", lambda _channel: {"allow": [], "block": []})
    monkeypatch.setattr(softnet_launcher, "wait_for_softnet_endpoint", lambda: ("192.0.2.2", "192.0.2.3"))
    monkeypatch.setattr(softnet_launcher, "start_dns_forwarder", lambda *_args: ExitedDnsProcess())
    monkeypatch.setattr(softnet_launcher, "wait_for_guest_exec_agent", lambda: None)

    def capture(args: list[str], *, timeout: float = 2.0) -> str:
        if args[-1] == "true":
            return ""
        if args[-1] == softnet_launcher.GUEST_DNS_HELPER:
            return "Trusted DNS relay configured: 172.30.249.129:55353"
        return "Current DNS Server: 192.0.2.53"

    monkeypatch.setattr(softnet_launcher, "_capture", capture)

    try:
        assert softnet_launcher.run_worker("headless") == 1
    finally:
        controller.close()
        guest.close()
    assert vm.returncode == -15
    assert "Worker startup blocked: Trusted guest DNS could not be reapplied and verified" in capsys.readouterr().err


def test_destructive_lifecycle_actions_have_fixed_targets_and_confirmation() -> None:
    reset = (WORKER_OPS / "reset.sh").read_text(encoding="utf-8")
    seal = (WORKER_OPS / "seal-baseline.sh").read_text(encoding="utf-8")

    assert "RESET-WORKER" in reset
    assert "SEAL-BASELINE" in seal
    assert "$1" not in reset
    assert "$1" not in seal


def test_hardening_disables_passwords_and_forwarding() -> None:
    harden = (WORKER_OPS / "harden.sh").read_text(encoding="utf-8")

    assert "ssh-copy-id" in harden
    assert "AuthorizedKeysFile /etc/ssh/authorized_keys/admin" in harden
    assert "00-toothless-hardening.conf" in harden
    assert "install -o root -g root -m 0644" in harden
    assert "PasswordAuthentication no" in harden
    assert "KbdInteractiveAuthentication no" in harden
    assert "PermitRootLogin no" in harden
    assert "AllowAgentForwarding no" in harden
    assert "AllowTcpForwarding no" in harden
    assert "passwd -l admin" in harden
    assert "sshd -t" in harden
    assert harden.index("BatchMode=yes") < harden.index("passwd -l admin")
    assert "sudo sync" in harden

    shutdown = (WORKER_OPS / "shutdown.sh").read_text(encoding="utf-8")
    assert 'tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub' in shutdown
    assert "StrictHostKeyChecking=yes" in shutdown
    assert "IdentityAgent=none" in shutdown
    assert "ForwardAgent=no" in shutdown
    assert 'ssh "${ssh_options[@]}" "admin@$worker_ip"' in shutdown
    assert "sudo -n sync && sudo -n systemctl poweroff" in shutdown
    assert "stop.sh only as an emergency stop" in shutdown

    seal = (WORKER_OPS / "seal-baseline.sh").read_text(encoding="utf-8")
    assert "shutdown.sh before sealing" in seal
    assert '[[ "$worker_state" == "stopped" ]]' in seal

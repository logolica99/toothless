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
    assert "sudo sync; sudo systemctl poweroff" in shutdown
    assert "stop.sh only as an emergency stop" in shutdown

    seal = (WORKER_OPS / "seal-baseline.sh").read_text(encoding="utf-8")
    assert "shutdown.sh before sealing" in seal
    assert '[[ "$worker_state" == "stopped" ]]' in seal

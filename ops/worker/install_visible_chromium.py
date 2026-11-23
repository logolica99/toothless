#!/usr/bin/env python3
"""Install only the reviewed Playwright-compatible ARM64 Chrome archive."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nanobot.personal_agent.worker_readiness import worker_readiness

WORKER = "toothless-worker"
VERSION = "159.0.8154.6"
ARCHIVE_BYTES = 207_644_679
EXTRACTED_BYTES = 430_344_758
ARCHIVE_ENTRIES = 306
INSTALLER = r'''import base64, hashlib, json, os, pathlib, shutil, struct, subprocess, tempfile, urllib.parse, urllib.request, zipfile
version = "159.0.8154.6"
url = "https://storage.googleapis.com/chrome-for-testing-public/" + version + "/linux-arm64/chrome-linux-arm64.zip"
expected_bytes = 207644679
expected_md5 = "SeqxbLZxEWilmAo+U4w3LQ=="
expected_crc32c = "/LRbng=="
expected_unpacked = 430344758
expected_entries = 306
root = pathlib.Path("/opt/toothless/visible-browser")
destination = root / "chrome-linux-arm64"
if destination.exists() or destination.is_symlink():
    raise SystemExit("Refusing to replace an existing visible-browser installation")
root.mkdir(mode=0o755, parents=True, exist_ok=True)
if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o022:
    raise SystemExit("Visible-browser installation root ownership or mode is unsafe")
request = urllib.request.Request(url, headers={"User-Agent": "Toothless-visible-browser/1.0"})
with urllib.request.urlopen(request, timeout=30) as response:
    final = urllib.parse.urlsplit(response.geturl())
    if response.status != 200 or final.scheme != "https" or final.hostname != "storage.googleapis.com" or final.path != "/chrome-for-testing-public/" + version + "/linux-arm64/chrome-linux-arm64.zip":
        raise SystemExit("Chrome archive origin or response did not match the approved asset")
    if response.headers.get("Content-Length") != str(expected_bytes):
        raise SystemExit("Chrome archive size differs from the approved asset")
    object_hashes = {}
    for item in response.headers.get("x-goog-hash", "").split(","):
        key, separator, value = item.strip().partition("=")
        if separator:
            object_hashes[key] = value
    etag = response.headers.get("ETag", "").strip('"')
    published_md5 = object_hashes.get("md5")
    if published_md5 is None and len(etag) == 32:
        published_md5 = base64.b64encode(bytes.fromhex(etag)).decode("ascii")
    if published_md5 != expected_md5 or object_hashes.get("crc32c") != expected_crc32c:
        raise SystemExit("Google archive integrity metadata differs from the reviewed values")
    descriptor, archive_name = tempfile.mkstemp(prefix="toothless-chrome-", suffix=".zip", dir="/var/tmp")
    archive_path = pathlib.Path(archive_name)
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    crc = 0xffffffff
    table = []
    for table_value in range(256):
        table_entry = table_value
        for _ in range(8):
            table_entry = (table_entry >> 1) ^ (0x82f63b78 if table_entry & 1 else 0)
        table.append(table_entry)
    try:
        with os.fdopen(descriptor, "wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > expected_bytes:
                    raise SystemExit("Chrome archive exceeded the approved size")
                output.write(chunk)
                md5.update(chunk)
                sha256.update(chunk)
                for byte in chunk:
                    crc = table[(crc ^ byte) & 0xff] ^ (crc >> 8)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise
    crc ^= 0xffffffff
    crc_b64 = base64.b64encode(struct.pack(">I", crc)).decode("ascii")
    if total != expected_bytes or base64.b64encode(md5.digest()).decode("ascii") != expected_md5 or crc_b64 != expected_crc32c:
        archive_path.unlink(missing_ok=True)
        raise SystemExit("Downloaded archive failed size, MD5, or CRC32C verification")
    stage = pathlib.Path(tempfile.mkdtemp(prefix=".visible-browser-", dir=root))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) != expected_entries or sum(info.file_size for info in infos) != expected_unpacked:
                raise SystemExit("Chrome archive contents differ from the reviewed manifest")
            for info in infos:
                member = pathlib.PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if member.is_absolute() or ".." in member.parts or not member.parts or member.parts[0] != "chrome-linux-arm64" or mode & 0o170000 == 0o120000:
                    raise SystemExit("Chrome archive contains an unsafe member")
            if archive.testzip() is not None:
                raise SystemExit("Chrome archive ZIP CRC verification failed")
            archive.extractall(stage)
            for info in infos:
                target = stage.joinpath(*pathlib.PurePosixPath(info.filename).parts)
                mode = info.external_attr >> 16
                target.chmod(0o755 if info.is_dir() or mode & 0o111 else 0o644)
        tree = stage / "chrome-linux-arm64"
        executable = tree / "chrome"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise SystemExit("Chrome executable is missing or not executable")
        version_result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=15, check=True)
        if version not in version_result.stdout:
            raise SystemExit("Installed Chrome version differs from the approved version")
        os.replace(tree, destination)
        binary_sha256 = hashlib.sha256((destination / "chrome").read_bytes()).hexdigest()
        manifest = {
            "browser": "Chrome for Testing",
            "version": version,
            "architecture": "linux-arm64",
            "playwright_version": "1.65.2",
            "archive_bytes": expected_bytes,
            "archive_md5_base64": expected_md5,
            "archive_crc32c_base64": expected_crc32c,
            "archive_sha256": sha256.hexdigest(),
            "chrome_binary_sha256": binary_sha256,
            "publisher_signature": "not provided by the upstream ZIP; HTTPS and published object hashes verified",
        }
        (root / "INSTALLATION.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        (root / "INSTALLATION.json").chmod(0o644)
        print(json.dumps({"status": "installed", "version": version, "archive_bytes": total, "extracted_bytes": expected_unpacked, "archive_sha256": sha256.hexdigest(), "chrome_binary_sha256": binary_sha256}, separators=(",", ":")))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
'''


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env={**os.environ, "TART_HOME": os.environ.get("TART_HOME", "/Volumes/toothless/Tools/Tart")},
    )


def main() -> int:
    readiness = worker_readiness()
    if readiness.get("worker_ready") is not True:
        print("Protected worker readiness is not verified; no browser files were installed.")
        return 1
    disk = _run(["tart", "exec", WORKER, "sudo", "-n", "/usr/bin/df", "-B1", "/"])
    try:
        available = int(disk.stdout.splitlines()[1].split()[3])
    except (IndexError, ValueError):
        available = 0
    required = ARCHIVE_BYTES + EXTRACTED_BYTES + 1024 * 1024 * 1024
    if disk.returncode != 0 or available < required:
        print("Guest free-space preflight is below the approved archive, extraction, and 1 GiB reserve.")
        return 1
    encoded = base64.b64encode(INSTALLER.encode()).decode("ascii")
    guest_command = [
        "tart", "exec", WORKER, "sudo", "-n", "/usr/bin/python3", "-c",
        "import base64;exec(compile(base64.b64decode('" + encoded + "'),'<approved-browser-installer>','exec'))",
    ]
    completed = _run(guest_command)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        print("Guest browser install failed closed. No apt operation was run. " + detail[:600])
        return completed.returncode
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        print("Guest browser install returned malformed bounded output.")
        return 1
    if not isinstance(payload, dict):
        print("Guest browser install returned an invalid result.")
        return 1
    result = cast(dict[str, object], payload)
    if result.get("status") != "installed" or result.get("version") != VERSION:
        print("Guest browser install result did not match the approved version.")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Owner-only host configuration for the private web console."""

from __future__ import annotations

import os
import re
import secrets
import ssl
import stat
from pathlib import Path

_KEY = "TOOTHLESS_SUPABASE_DSN"
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class WebConsoleConfig:
    """Read and atomically update the console runtime DSN in a private file."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("TOOTHLESS_WEB_CONSOLE_CONFIG")
        self.path = path if path is not None else (
            Path(configured).expanduser() if configured else Path.home() / ".config/toothless/web-console.env"
        )
        self.ca_cert_path = self.path.parent / "supabase-root.crt"

    def create_ssl_context(self) -> ssl.SSLContext:
        """Create strict TLS trust, adding the owner-provided project CA when present."""
        context = ssl.create_default_context()
        try:
            info = self.ca_cert_path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise PermissionError("Supabase CA certificate must be an owner-only regular file (0600)")
            context.load_verify_locations(cafile=str(self.ca_cert_path))
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("Supabase TLS must verify the certificate and hostname")
        return context

    def get_dsn(self) -> str | None:
        values = self._read()
        return values.get(_KEY)

    def set_dsn(self, dsn: str) -> None:
        if not dsn or "\n" in dsn or "\r" in dsn or len(dsn) > 4096:
            raise ValueError("invalid database connection setting")
        values = self._read() if self._exists() else {}
        values[_KEY] = dsn
        self._write(values)

    def migrate_keychain_dsn(self, dsn: str) -> None:
        """Copy an explicitly selected legacy value without overwriting config."""
        if self._exists():
            raise FileExistsError("web console configuration already exists")
        self.set_dsn(dsn)

    def _read(self) -> dict[str, str]:
        if not self._exists():
            return {}
        parent = self.path.parent
        parent_stat = parent.lstat()
        file_stat = self.path.lstat()
        if (stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode)
                or stat.S_IMODE(parent_stat.st_mode) != 0o700
                or parent_stat.st_uid != os.getuid()):
            raise PermissionError("web console configuration directory must be owner-only (0700)")
        if (stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode)
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or file_stat.st_uid != os.getuid()):
            raise PermissionError("web console configuration file must be owner-only (0600)")
        values: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            match = _ASSIGNMENT.fullmatch(line)
            if not match or match.group(1) != _KEY or _KEY in values:
                raise ValueError("web console configuration has an unsupported or duplicate setting")
            values[match.group(1)] = match.group(2)
        return values

    def _exists(self) -> bool:
        try:
            self.path.lstat()
            return True
        except FileNotFoundError:
            return False

    def _write(self, values: dict[str, str]) -> None:
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True)
        parent_stat = parent.lstat()
        if (stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode)
                or parent_stat.st_uid != os.getuid()):
            raise PermissionError("web console configuration directory is not owner-controlled")
        os.chmod(parent, 0o700)
        content = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
        temporary = parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

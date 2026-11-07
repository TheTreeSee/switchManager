"""SSH connections with Netmiko. Imported lazily so --gen and the tests work without Netmiko installed."""

import getpass
from typing import Any

from .config import Settings



class ConnectionLost(Exception):
    """The SSH session dropped (or could not be (re)established)."""


def ensure_credentials(settings: Settings) -> None:
    """Ask once for what .env does not provide - before any (parallel) work starts."""
    if not settings.username:
        settings.username = input("SSH username: ").strip()
    if not settings.password:
        settings.password = getpass.getpass(f"SSH password for {settings.username}: ")
    if not settings.secret:
        settings.secret = settings.password


def connection_params(ip: str, settings: Settings) -> dict[str, Any]:
    if not settings.username or not settings.password:
        raise ConnectionLost("no SSH credentials - set SWITCH_USERNAME/SWITCH_PASSWORD in .env")
    return {
        "device_type": settings.device_type,
        "host": ip,
        "username": settings.username,
        "password": settings.password,
        "secret": settings.secret or settings.password,
        "port": settings.ssh_port,
        "timeout": settings.ssh_timeout,
        "conn_timeout": settings.ssh_timeout,
        "fast_cli": False,
    }


def connect(ip: str, settings: Settings):
    """Open an SSH session in enable mode."""
    try:
        from netmiko import ConnectHandler
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit("Netmiko is not installed: pip install -r requirements.txt") from exc
    conn = ConnectHandler(**connection_params(ip, settings))
    if not conn.check_enable_mode():
        conn.enable()
    return conn


def is_connection_error(exc: BaseException) -> bool:
    """Network/SSH failures, as opposed to logic errors in our code."""
    chain = list(_chain(exc))
    if any(isinstance(e, (OSError, EOFError, TimeoutError, ConnectionLost)) for e in chain):
        return True
    names = {type(e).__name__ for e in chain}
    return bool(names & {
        "ConnectionLost", "NetmikoTimeoutException", "ReadTimeout", "ReadException", "SSHException",
        "EOFError", "OSError", "socket.timeout", "TimeoutError", "ConnectionResetError", "BrokenPipeError",
        "NetmikoAuthenticationException", "AuthenticationException",
    })


def _chain(exc: BaseException):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__

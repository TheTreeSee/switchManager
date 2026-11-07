"""Settings from .env / the environment. Nothing important is hard coded."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # SSH
    username: str
    password: str
    secret: str
    device_type: str
    ssh_port: int
    ssh_timeout: int
    # Batch
    batch_max_workers: int
    # Rendering
    mgmt_gateway: str            # 'first', 'last' or an explicit IPv4 address
    trunk_encapsulation: str     # 'auto', 'yes' or 'no' (switchport trunk encapsulation dot1q)
    # Backup / TFTP
    tftp_bind: str
    tftp_port: int
    tftp_server_address: str     # address the switch uses to reach this PC; empty = auto-detect
    tftp_timeout: int
    backup_after_apply: bool
    # Directories
    input_dir: Path
    output_dir: Path
    inventory_dir: Path
    logs_dir: Path
    backup_dir: Path

    @property
    def inventory_file(self) -> Path:
        return self.inventory_dir / "switches.csv"

    @property
    def discovery_file(self) -> Path:
        return self.inventory_dir / "discovery.json"

    @property
    def interface_report(self) -> Path:
        return self.inventory_dir / "interface-report.txt"


def load_settings(root: Path = ROOT, env_file: Path | None = None) -> Settings:
    env = load_dotenv(env_file or root / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("SWITCH_", "SSH_", "BATCH_", "TFTP_", "MGMT_", "TRUNK_", "BACKUP_", "SM_"))})

    def get(key: str, default: str = "") -> str:
        return env.get(key, default)

    def path(key: str, default: str) -> Path:
        p = Path(get(key, default))
        return p if p.is_absolute() else root / p

    return Settings(
        username=get("SWITCH_USERNAME"),
        password=get("SWITCH_PASSWORD"),
        secret=get("SWITCH_ENABLE_SECRET"),
        device_type=get("SWITCH_DEVICE_TYPE", "cisco_ios"),
        ssh_port=int(get("SSH_PORT", "22")),
        ssh_timeout=int(get("SSH_TIMEOUT", "20")),
        batch_max_workers=max(1, int(get("BATCH_MAX_WORKERS", "1"))),
        mgmt_gateway=get("MGMT_GATEWAY", "first"),
        trunk_encapsulation=get("TRUNK_ENCAPSULATION", "auto").lower(),
        tftp_bind=get("TFTP_BIND_ADDRESS", "0.0.0.0"),
        tftp_port=int(get("TFTP_PORT", "69")),
        tftp_server_address=get("TFTP_SERVER_ADDRESS"),
        tftp_timeout=int(get("TFTP_TIMEOUT", "60")),
        backup_after_apply=_bool(get("BACKUP_AFTER_APPLY", "true")),
        input_dir=path("SM_INPUT_DIR", "input"),
        output_dir=path("SM_OUTPUT_DIR", "output"),
        inventory_dir=path("SM_INVENTORY_DIR", "inventory"),
        logs_dir=path("SM_LOGS_DIR", "logs"),
        backup_dir=path("BACKUP_DIR", "backup"),
    )

"""One append-only log file per switch per apply run: logs/<UTC time>_<ip>_apply.log"""

import hashlib
import traceback
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApplyLog:
    def __init__(self, logs_dir: Path, ip: str, action: str = "apply"):
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        self.path = logs_dir / f"{stamp}_{ip}_{action}.log"
        self._fh = self.path.open("a", encoding="utf-8")
        self.last_confirmed = "(nothing sent yet)"

    def event(self, text: str) -> None:
        self._fh.write(f"[{utc_now()}] {text}\n")
        self._fh.flush()

    def block(self, title: str, text: str) -> None:
        self._fh.write(f"[{utc_now()}] ----- {title} -----\n{text.rstrip()}\n[{utc_now()}] ----- end {title} -----\n")
        self._fh.flush()

    def exchange(self, title: str, commands: list[str] | str, output: str) -> None:
        sent = commands if isinstance(commands, str) else "\n".join(commands)
        self.block(f"{title}: sent", sent)
        self.block(f"{title}: device output", output)
        self.last_confirmed = f"{title}: {sent.splitlines()[-1] if sent else ''}"

    def exception(self, exc: BaseException) -> None:
        self.block("exception", "".join(traceback.format_exception(exc)))

    def close(self) -> None:
        self._fh.close()

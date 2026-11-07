"""A fake Cisco IOS switch with the parts of the Netmiko API that switchManager uses."""

import re
import socket
import struct
import tempfile
from pathlib import Path

from switch_manager.config import load_settings


def make_settings(tmp: Path):
    settings = load_settings(root=tmp, env_file=tmp / "missing.env")
    settings.username, settings.password = "test", "test"
    settings.input_dir = tmp / "input"
    settings.output_dir = tmp / "output"
    settings.inventory_dir = tmp / "inventory"
    settings.logs_dir = tmp / "logs"
    settings.backup_dir = tmp / "backup"
    settings.tftp_bind, settings.tftp_port, settings.tftp_server_address = "127.0.0.1", 0, "127.0.0.1"
    settings.tftp_timeout = 5
    for d in (settings.input_dir, settings.output_dir, settings.inventory_dir):
        d.mkdir(parents=True, exist_ok=True)
    return settings


def tempdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="switchmanager-test-"))


def ports_2960() -> list[str]:
    return ["Vlan1"] + [f"FastEthernet0/{i}" for i in range(1, 25)] + ["GigabitEthernet0/1", "GigabitEthernet0/2"]


class Dropped(OSError):
    """Raised like a socket error when the fake drops the session."""


class SwitchState:
    """Device state shared by every session to the same fake switch (so reconnects see the same switch)."""

    def __init__(self, interfaces=None, model="WS-C2960-24TT-L", hostname="Switch"):
        self.interfaces = interfaces or ports_2960()
        self.model = model
        self.hostname = hostname
        self.running: list[str] = []
        self.startup: list[str] = []
        self.reload_pending = False
        self.sessions = 0
        self.history: list[str] = []          # every command/config line received, in order
        # failure injection
        self.reject_line: str | None = None      # config line that answers '% Invalid input'
        self.drop_on_line: str | None = None     # config line during which the session drops
        self.drop_after_config = False           # session works for config, then the next find_prompt fails once
        self.drop_on_save = False
        self.drop_on_reload_cancel = False
        self.reload_cancel_ignored = False
        self.refuse_connections_after = None     # number of sessions allowed
        self.tftp_payload = b"!\nhostname Switch\n!\nend\n" * 100


class FakeSwitch:
    def __init__(self, state: SwitchState):
        self.state = state
        state.sessions += 1
        self.alive = True
        self._pending_prompt = None

    # --- session ---------------------------------------------------------------
    def _check(self):
        if not self.alive:
            raise Dropped("Socket is closed")

    def find_prompt(self):
        self._check()
        if self.state.drop_after_config and self.state.running:
            self.state.drop_after_config = False
            self.alive = False
            raise Dropped("session dropped after management change")
        return f"{self.state.hostname}#"

    def check_enable_mode(self):
        return True

    def enable(self):
        pass

    def disconnect(self):
        self.alive = False

    # --- exec commands -----------------------------------------------------------
    def send_command(self, command, **_kw):
        self._check()
        self.state.history.append(command)
        if command.startswith("show running-config | include ^hostname"):
            return f"hostname {self.state.hostname}"
        if command == "show version":
            return f"Cisco IOS Software, C2960 Software, Version 15.2(7)E10\nModel number                    : {self.state.model}\n"
        if command == "show ip interface brief":
            rows = ["Interface              IP-Address      OK? Method Status                Protocol"]
            rows += [f"{i:<22} unassigned      YES unset  up                    up" for i in self.state.interfaces]
            return "\n".join(rows)
        if command == "show switch":
            return "                  ^\n% Invalid input detected at '^' marker."
        if command == "show reload":
            return "Reload scheduled for 15:10:00 UTC Sat Nov 7 2026 (in 9 minutes 59 seconds)" if self.state.reload_pending else "No reload is scheduled."
        if command == "show startup-config":
            return "\n".join(self.state.startup)
        return ""

    def send_command_timing(self, command, **_kw):
        self._check()
        self.state.history.append(command)
        if command.startswith("reload in"):
            self._pending_prompt = "reload-save"
            return "System configuration has been modified. Save? [yes/no]: "
        if self._pending_prompt == "reload-save" and command == "no":
            self._pending_prompt = "reload-confirm"
            return "Reload scheduled in 10 minutes\nProceed with reload? [confirm]"
        if self._pending_prompt == "reload-confirm" and command == "\n":
            self._pending_prompt = None
            self.state.reload_pending = True
            return f"{self.state.hostname}#"
        if command == "reload cancel":
            if self.state.drop_on_reload_cancel:
                self.alive = False
                raise Dropped("dropped during reload cancel")
            if not self.state.reload_cancel_ignored:
                self.state.reload_pending = False
            return "***\n*** --- SHUTDOWN ABORTED ---\n***"
        match = re.match(r"copy running-config tftp://([\d.]+):?(\d*)/(\S+)", command)
        if match:
            self._pending_prompt = ("copy-host", match.group(1), int(match.group(2) or 69), match.group(3))
            return f"Address or name of remote host [{match.group(1)}]? "
        if isinstance(self._pending_prompt, tuple) and self._pending_prompt[0] == "copy-host":
            self._pending_prompt = ("copy-file",) + self._pending_prompt[1:]
            return f"Destination filename [{self._pending_prompt[3]}]? "
        if isinstance(self._pending_prompt, tuple) and self._pending_prompt[0] == "copy-file":
            _, host, port, filename = self._pending_prompt
            self._pending_prompt = None
            size = tftp_upload(host, port, filename, self.state.tftp_payload)
            return f"!!\n{size} bytes copied in 0.123 secs\n{self.state.hostname}#"
        return f"{self.state.hostname}#"

    # --- config mode --------------------------------------------------------------
    def send_config_set(self, commands, **_kw):
        self._check()
        out = ["configure terminal", "Enter configuration commands, one per line.  End with CNTL/Z."]
        for line in commands:
            self.state.history.append(line)
            out.append(f"{self.state.hostname}(config)#{line}")
            if self.state.drop_on_line and line == self.state.drop_on_line:
                self.alive = False
                raise Dropped(f"dropped while sending '{line}'")
            if self.state.reject_line and line == self.state.reject_line:
                out.append("                ^\n% Invalid input detected at '^' marker.")
                continue
            self.state.running.append(line)
        out.append(f"{self.state.hostname}(config)#end")
        return "\n".join(out)

    def save_config(self):
        self._check()
        self.state.history.append("write memory")
        if self.state.drop_on_save:
            self.alive = False
            raise Dropped("dropped during save")
        self.state.startup = list(self.state.running)
        return "Building configuration...\n[OK]"


def connector_for(state: SwitchState):
    def connector(ip, settings):
        if state.refuse_connections_after is not None and state.sessions >= state.refuse_connections_after:
            raise Dropped(f"connection to {ip} refused")
        return FakeSwitch(state)
    return connector


def tftp_upload(host: str, port: int, filename: str, payload: bytes) -> int:
    """Tiny TFTP client (WRQ, octet) like the one in IOS."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(struct.pack("!H", 2) + filename.encode() + b"\0octet\0", (host, port))
        ack, server = s.recvfrom(516)
        assert struct.unpack("!HH", ack[:4]) == (4, 0), ack
        block = 1
        for offset in range(0, len(payload) + 1, 512):
            chunk = payload[offset:offset + 512]
            s.sendto(struct.pack("!HH", 3, block) + chunk, server)
            ack, _ = s.recvfrom(516)
            assert struct.unpack("!HH", ack[:4]) == (4, block), ack
            block += 1
            if len(chunk) < 512:
                break
    return len(payload)

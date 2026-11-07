"""--backup: a temporary TFTP server receives the switch's running config (copy running-config tftp:)."""

import re
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .netmiko_client import connect

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR = 1, 2, 3, 4, 5
BLOCK = 512
_BACKUP_LOCK = threading.Lock()      # one TFTP server port: one backup at a time, also in parallel batches


class TftpReceiver:
    """Minimal RFC 1350 TFTP server that accepts one write (WRQ) of one expected file from one host."""

    def __init__(self, bind: str, port: int, expected_filename: str, target: Path, allowed_host: str | None = None,
                 timeout: float = 5.0, retries: int = 5):
        self.expected_filename = expected_filename
        self.target = target
        self.allowed_host = allowed_host
        self.timeout = timeout
        self.retries = retries
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((bind, port))
        except PermissionError as exc:
            self.sock.close()
            raise PermissionError(
                f"cannot listen on UDP port {port}: ports below 1024 need extra rights on Linux. Either run\n"
                "  sudo sysctl -w net.ipv4.ip_unprivileged_port_start=69\n"
                "or set TFTP_PORT to a high port and forward UDP 69 to it."
            ) from exc
        self.port = self.sock.getsockname()[1]
        self.done = threading.Event()
        self.error: str | None = None
        self.bytes_received = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "TftpReceiver":
        self._thread.start()
        return self

    def wait(self, timeout: float) -> bool:
        return self.done.wait(timeout)

    def close(self) -> None:
        self.done.set()
        self.sock.close()

    @staticmethod
    def _error_packet(code: int, message: str) -> bytes:
        return struct.pack("!HH", OP_ERROR, code) + message.encode() + b"\0"

    def _serve(self) -> None:
        self.sock.settimeout(0.5)
        while not self.done.is_set():
            try:
                packet, client = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(packet) < 4:
                continue
            opcode = struct.unpack("!H", packet[:2])[0]
            if self.allowed_host and client[0] != self.allowed_host:
                self.sock.sendto(self._error_packet(2, "access violation"), client)
                continue
            if opcode == OP_RRQ:
                self.sock.sendto(self._error_packet(2, "read not supported"), client)
                continue
            if opcode != OP_WRQ:
                continue
            parts = packet[2:].split(b"\0")
            filename = parts[0].decode(errors="replace").lstrip("/")
            mode = parts[1].decode(errors="replace").lower() if len(parts) > 1 else "octet"
            if filename != self.expected_filename:
                self.sock.sendto(self._error_packet(2, f"unexpected file {filename}"), client)
                continue
            self._receive(client, mode)
            return

    def _receive(self, client, mode: str) -> None:
        # A new socket = new transfer ID (RFC 1350); options in the WRQ are ignored -> client uses defaults
        tid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tid.settimeout(self.timeout)
        tmp = self.target.with_suffix(".part")
        try:
            with tmp.open("wb") as fh:
                ack = struct.pack("!HH", OP_ACK, 0)
                tid.sendto(ack, client)
                expected, retries = 1, 0
                while True:
                    try:
                        packet, sender = tid.recvfrom(4 + BLOCK + 64)
                    except socket.timeout:
                        retries += 1
                        if retries > self.retries:
                            raise TimeoutError("transfer timed out")
                        tid.sendto(ack, client)
                        continue
                    if sender != client:
                        tid.sendto(self._error_packet(5, "unknown transfer ID"), sender)
                        continue
                    opcode, block = struct.unpack("!HH", packet[:4])
                    if opcode == OP_ERROR:
                        raise RuntimeError(f"switch aborted the transfer: {packet[4:].rstrip(b'\0').decode(errors='replace')}")
                    if opcode != OP_DATA:
                        continue
                    if block == expected:
                        data = packet[4:]
                        fh.write(data)
                        self.bytes_received += len(data)
                        ack = struct.pack("!HH", OP_ACK, block)
                        tid.sendto(ack, client)
                        retries = 0
                        if len(data) < BLOCK:
                            break
                        expected = (expected + 1) % 65536
                    else:
                        tid.sendto(ack, client)            # duplicate: ACK again
            if mode == "netascii":
                tmp.write_bytes(tmp.read_bytes().replace(b"\r\n", b"\n").replace(b"\r\0", b"\r"))
            tmp.replace(self.target)
        except Exception as exc:
            self.error = str(exc)
            tmp.unlink(missing_ok=True)
        finally:
            tid.close()
            self.done.set()


def local_address_towards(ip: str) -> str:
    """The address of this PC on the route to the switch (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ip, 9))
        return s.getsockname()[0]


def copy_running_config(conn, server: str, filename: str, port: int) -> str:
    """Answer the interactive prompts of 'copy running-config tftp:'; returns all device output."""
    url = f"tftp://{server}/{filename}" if port == 69 else f"tftp://{server}:{port}/{filename}"
    output = conn.send_command_timing(f"copy running-config {url}", strip_prompt=False, strip_command=False)
    full = output
    for _ in range(6):
        if re.search(r"\?\s*\[|\?\s*$|\[confirm\]", output.strip().splitlines()[-1] if output.strip() else ""):
            output = conn.send_command_timing("\n", strip_prompt=False, strip_command=False, read_timeout=60)
            full += output
        else:
            break
    return full


def run_backup(ip: str, settings: Settings, conn=None, hostname: str = "", log=None) -> Path:
    """Back up the running config of one switch to backup/. Raises with a clear message on failure."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    server = settings.tftp_server_address or local_address_towards(ip)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{ip}_{stamp}.cfg"
    target = settings.backup_dir / f"{ip}_{hostname + '_' if hostname else ''}{stamp}.cfg"

    with _BACKUP_LOCK:
        return _backup(ip, settings, conn, server, filename, target, log)


def _backup(ip, settings, conn, server, filename, target, log) -> Path:
    # No source-address filter: a switch may send from another SVI than the SSH address; the unique file name is checked
    receiver = TftpReceiver(settings.tftp_bind, settings.tftp_port, filename, target).start()
    own = conn is None
    try:
        conn = conn or connect(ip, settings)
        output = copy_running_config(conn, server, filename, receiver.port)
        if log:
            log.exchange("backup (copy running-config tftp:)", f"copy running-config tftp://{server}/{filename}", output)
        if re.search(r"%Error|Error opening|Timed out|No route|refused", output, re.IGNORECASE):
            raise RuntimeError(
                f"the switch could not send the file to TFTP server {server}:{receiver.port} - check that the switch "
                f"can route to this PC, the firewall allows UDP {receiver.port}, and TFTP_SERVER_ADDRESS.\nSwitch said: "
                + output.strip().splitlines()[-1]
            )
        if not receiver.wait(settings.tftp_timeout):
            raise TimeoutError(f"no TFTP transfer received from {ip} within {settings.tftp_timeout}s (server {server}:{receiver.port})")
        if receiver.error:
            raise RuntimeError(f"TFTP transfer failed: {receiver.error}")
        time.sleep(0.2)
        return target
    finally:
        receiver.close()
        if own and conn is not None:
            conn.disconnect()

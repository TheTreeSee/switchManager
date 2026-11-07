"""inventory/switches.csv (ip;hostname;note) and inventory/discovery.json."""

import csv
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

from .models import ValidationError


@dataclass
class InventoryEntry:
    ip: str
    hostname: str = ""
    note: str = ""


def read_inventory(path: Path) -> list[InventoryEntry]:
    """Entries in file order (the operator puts downstream switches first)."""
    if not path.is_file():
        raise ValidationError(f"inventory file {path} not found - create it with the header 'ip;hostname;note'")
    entries: list[InventoryEntry] = []
    problems: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for number, row in enumerate(csv.DictReader(fh, delimiter=";"), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            ip = row.get("ip", "")
            if not ip:
                continue
            try:
                ip = str(ipaddress.IPv4Address(ip))
            except ValueError:
                problems.append(f"{path.name} line {number}: '{ip}' is not an IPv4 address")
                continue
            if ip in seen:
                problems.append(f"{path.name} line {number}: {ip} is listed more than once")
                continue
            seen.add(ip)
            entries.append(InventoryEntry(ip, row.get("hostname", ""), row.get("note", "")))
    if problems:
        raise ValidationError(problems)
    return entries


def write_inventory(path: Path, entries: list[InventoryEntry]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", lineterminator="\n")
        writer.writerow(["ip", "hostname", "note"])
        for e in entries:
            writer.writerow([e.ip, e.hostname, e.note])
    tmp.replace(path)


def find_entry(entries: list[InventoryEntry], ip: str) -> InventoryEntry | None:
    return next((e for e in entries if e.ip == ip), None)


def load_discovery(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_discovery(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)

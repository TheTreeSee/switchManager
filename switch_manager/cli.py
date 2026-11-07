"""Command line.

  python switch_manager.py [--gen [CSV ...]]                generate configs (default mode)
  python switch_manager.py --detect                         discover all inventory switches
  python switch_manager.py <ip> --apply [CSV]               generate + apply to one switch (CSV defaults to <ip>.csv)
  python switch_manager.py --batch --apply                  apply every input/<ip>.csv to its switch
  python switch_manager.py <ip> --apply --rollback-timer 10 with a 'reload in 10' safety net
  python switch_manager.py <ip> --backup                    only back up the running config via TFTP
  --examples uses input/examples, inventory/examples and output/examples (offline demo)
"""

import argparse
import ipaddress
import os
import sys
from pathlib import Path

from .apply import SUCCESS, apply_to_switch
from .backup import run_backup
from .batch import run_batch
from .config import ROOT, Settings, load_settings
from .csv_parser import parse_switch_csv
from .discovery import run_detect
from .interface_map import InterfaceMap
from .inventory import find_entry, load_discovery, read_inventory
from .models import ValidationError
from .netmiko_client import ensure_credentials
from .renderer import render


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="switch_manager.py", description="Cisco L2/L3 switch configuration from CSV files (Netmiko).")
    p.add_argument("target", nargs="?", help="switch IPv4 address (for --apply, --backup or --gen of one switch)")
    p.add_argument("--gen", nargs="*", metavar="CSV", help="generate configs (default): given CSVs, or every CSV in input/")
    p.add_argument("--apply", nargs="?", const="", metavar="CSV", help="generate and apply; CSV defaults to <target>.csv")
    p.add_argument("--batch", action="store_true", help="with --apply: every input/<ip>.csv to its inventory switch")
    p.add_argument("--backup", action="store_true", help="back up the running config via TFTP (alone, or after --apply)")
    p.add_argument("--no-backup", action="store_true", help="no backup after --apply even if BACKUP_AFTER_APPLY=true")
    p.add_argument("--detect", action="store_true", help="discover hostname, platform and interfaces of every inventory switch")
    p.add_argument("--rollback-timer", type=int, metavar="MIN", help="schedule 'reload in MIN' before management-path changes")
    p.add_argument("--set-hostname", action="store_true", help="also configure the inventory hostname on the switch")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--examples", action="store_true", help="use the tracked example input/inventory/output folders")
    return p


def resolve_csv(name: str, settings: Settings) -> Path:
    path = Path(name)
    if path.suffix.lower() != ".csv":
        path = path.with_name(path.name + ".csv")
    if path.is_file():
        return path
    candidate = settings.input_dir / path.name
    if candidate.is_file():
        return candidate
    raise ValidationError(f"input file {name} not found (looked in {settings.input_dir})")


def check_ip(text: str) -> str:
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise ValidationError(f"'{text}' is not a switch IPv4 address")


def generate(settings: Settings, csv_paths: list[Path], target: str | None, set_hostname: bool) -> int:
    discovery = load_discovery(settings.discovery_file)
    hostnames = {}
    if settings.inventory_file.is_file():
        hostnames = {e.ip: e.hostname for e in read_inventory(settings.inventory_file)}
    failed = 0
    for path in csv_paths:
        ip = target or path.stem
        try:
            ip = check_ip(ip)
            data = discovery.get(ip)
            if not data or not data.get("interfaces"):
                raise ValidationError(f"no discovery data for {ip} in {settings.discovery_file} - run --detect first "
                                      "(physical interface names are model dependent)")
            spec = parse_switch_csv(path, settings.mgmt_gateway)
            rendered = render(spec, InterfaceMap(data["interfaces"]), ip, hostname=hostnames.get(ip) or data.get("hostname"),
                              model=data.get("model"), trunk_encapsulation=settings.trunk_encapsulation, set_hostname=set_hostname)
        except ValidationError as exc:
            failed += 1
            print(f"[FAILED] {path.name}\n  " + "\n  ".join(exc.problems))
            continue
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        out = settings.output_dir / f"{ip}.cfg"
        out.write_text(rendered.text, encoding="utf-8")
        kind = "L3" if spec.layer3 else "L2"
        print(f"[OK] {path.name} -> {os.path.relpath(out)}  ({kind}, {sum(len(s.commands) for s in rendered.stages)} commands)")
        for warning in rendered.warnings:
            print(f"     warning: {warning}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.examples:
        settings.input_dir = ROOT / "input" / "examples"
        settings.inventory_dir = ROOT / "inventory" / "examples"
        settings.output_dir = ROOT / "output" / "examples"

    try:
        if args.detect or args.batch or args.apply is not None or args.backup:
            ensure_credentials(settings)
        if args.detect:
            return run_detect(settings)

        if args.batch:
            if args.apply is None:
                raise ValidationError("--batch only works together with --apply")
            backup = (args.backup or settings.backup_after_apply) and not args.no_backup
            return run_batch(settings, rollback_minutes=args.rollback_timer, set_hostname=args.set_hostname, yes=args.yes, backup=backup)

        if args.apply is not None:
            if not args.target:
                raise ValidationError("--apply needs the switch IP: switch_manager.py <ip> --apply [csv]")
            ip = check_ip(args.target)
            csv_path = resolve_csv(args.apply or ip, settings)
            entries = read_inventory(settings.inventory_file) if settings.inventory_file.is_file() else []
            entry = find_entry(entries, ip)
            backup = (args.backup or settings.backup_after_apply) and not args.no_backup
            result = apply_to_switch(ip, csv_path, settings, hostname=entry.hostname if entry else "",
                                     set_hostname=args.set_hostname, rollback_minutes=args.rollback_timer,
                                     confirm=not args.yes, backup=backup)
            print(f"\n[{result.status}] {ip} - {result.message}")
            if result.output_path:
                print(f"  config: {result.output_path}")
            print(f"  log:    {result.log_path}")
            return 0 if result.status == SUCCESS else 1

        if args.backup and args.gen is None:
            if not args.target:
                raise ValidationError("--backup needs the switch IP: switch_manager.py <ip> --backup")
            ip = check_ip(args.target)
            entries = read_inventory(settings.inventory_file) if settings.inventory_file.is_file() else []
            entry = find_entry(entries, ip)
            path = run_backup(ip, settings, hostname=entry.hostname if entry else "")
            print(f"[OK] backup of {ip} -> {path}")
            return 0

        # Default: --gen
        names = args.gen or []
        if names:
            paths = [resolve_csv(n, settings) for n in names]
        elif args.target:
            paths = [resolve_csv(args.target, settings)]
        else:
            paths = sorted(settings.input_dir.glob("*.csv"))
            if not paths:
                raise ValidationError(f"no CSV files in {settings.input_dir}")
        target = check_ip(args.target) if args.target else None
        return generate(settings, paths, target, args.set_hostname)

    except ValidationError as exc:
        print("ERROR: " + "\n       ".join(exc.problems), file=sys.stderr)
        return 2
    except (PermissionError, TimeoutError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except EOFError:
        print("\nERROR: input needed (SSH credentials or confirmation) but none available - set them in .env or use --yes", file=sys.stderr)
        return 2

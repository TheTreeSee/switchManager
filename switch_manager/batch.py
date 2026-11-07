"""--batch --apply: input/<ip>.csv -> the inventory switch with exactly that IP.

Targets are matched on the file name only (never on a hostname or note). The whole batch is
refused when a file name is not an IPv4 address or has no inventory entry. Switches are processed
in inventory order (put downstream switches first); BATCH_MAX_WORKERS > 1 opts into parallel work.
"""

import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .apply import FAILED, SKIPPED, SUCCESS, UNCERTAIN, ApplyResult, apply_to_switch, ask_confirmation
from .config import Settings
from .csv_parser import parse_switch_csv
from .interface_map import InterfaceMap
from .inventory import InventoryEntry, load_discovery, read_inventory
from .models import ValidationError
from .renderer import render


def plan_batch(settings: Settings) -> tuple[list[tuple[InventoryEntry, Path]], list[InventoryEntry]]:
    """(targets in inventory order, inventory entries without input). Raises ValidationError on any mismatch."""
    entries = read_inventory(settings.inventory_file)
    problems: list[str] = []
    files: dict[str, Path] = {}
    for path in sorted(settings.input_dir.glob("*.csv")):          # only directly in input/, never input/examples/
        try:
            ip = ipaddress.IPv4Address(path.stem)
        except ValueError:
            problems.append(f"{path.name}: file name must be the switch IPv4 address, e.g. 172.19.67.13.csv")
            continue
        if str(ip) != path.stem:
            problems.append(f"{path.name}: write the address exactly as {ip}.csv")
            continue
        if str(ip) in files:
            problems.append(f"{path.name}: duplicate input for {ip}")
            continue
        files[str(ip)] = path
    known = {e.ip for e in entries}
    for ip, path in files.items():
        if ip not in known:
            problems.append(f"{path.name}: {ip} is not in {settings.inventory_file.name}")
    if problems:
        raise ValidationError(problems)
    if not files:
        raise ValidationError(f"no <ip>.csv files in {settings.input_dir}")
    targets = [(e, files[e.ip]) for e in entries if e.ip in files]
    without = [e for e in entries if e.ip not in files]
    return targets, without


def run_batch(settings: Settings, *, rollback_minutes: int | None, set_hostname: bool, yes: bool, backup: bool,
              connector=None) -> int:
    try:
        targets, without = plan_batch(settings)
    except ValidationError as exc:
        print("Batch refused:\n  " + "\n  ".join(exc.problems))
        return 2

    discovery = load_discovery(settings.discovery_file)
    expected: dict[str, str] = {}
    results: list[ApplyResult] = []
    runnable: list[tuple[InventoryEntry, Path]] = []

    print(f"Batch plan ({len(targets)} switch(es), {settings.batch_max_workers} worker(s), inventory order):\n")
    for number, (entry, path) in enumerate(targets, start=1):
        data = discovery.get(entry.ip)
        status = "ready"
        try:
            spec = parse_switch_csv(path, settings.mgmt_gateway)
            if not data or not data.get("interfaces"):
                raise ValidationError("no discovery data - run --detect first")
            rendered = render(spec, InterfaceMap(data["interfaces"]), entry.ip, hostname=entry.hostname, model=data.get("model"),
                              trunk_encapsulation=settings.trunk_encapsulation, set_hostname=set_hostname)
            expected[entry.ip] = rendered.body()
            mgmt = " +mgmt-path" if rendered.has_management_changes else ""
            status = f"ready ({'L3' if spec.layer3 else 'L2'}, {sum(len(s.commands) for s in rendered.stages)} commands{mgmt})"
            runnable.append((entry, path))
        except ValidationError as exc:
            status = "INVALID: " + "; ".join(exc.problems)
            results.append(ApplyResult(entry.ip, FAILED, status))
        print(f"  {number}. {entry.ip:<16} {entry.hostname or '-':<20} <- {path.name:<22} {status}")
    for entry in without:
        print(f"     {entry.ip:<16} {entry.hostname or '-':<20}    (no input file - skipped)")
        results.append(ApplyResult(entry.ip, SKIPPED, "no input file"))

    if rollback_minutes:
        print(f"\nRollback timer: 'reload in {rollback_minutes}' per switch with management-path changes.")
        print("WARNING: a reload restores the startup config and also discards older unsaved running changes.")
    if not runnable:
        print("\nNothing to apply.")
        return summarize(results)
    if not yes and not ask_confirmation(f"\nApply to {len(runnable)} switch(es)?"):
        print("Batch not confirmed - nothing sent.")
        return 1

    print()
    with ThreadPoolExecutor(max_workers=settings.batch_max_workers) as pool:
        futures = {
            pool.submit(apply_to_switch, entry.ip, path, settings, hostname=entry.hostname, set_hostname=set_hostname,
                        rollback_minutes=rollback_minutes, confirm=False, expected_body=expected[entry.ip],
                        backup=backup, echo=lambda _text: None, **({'connector': connector} if connector else {})): entry
            for entry, path in runnable
        }
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:          # apply_to_switch handles its own errors; this is a last resort
                result = ApplyResult(entry.ip, FAILED, f"{type(exc).__name__}: {exc}")
            results.append(result)
            print(f"  [{result.status}] {result.ip} - {result.message}" + (f"\n      log: {result.log_path}" if result.log_path else ""))
    return summarize(results)


def summarize(results: list[ApplyResult]) -> int:
    groups = {
        "succeeded": [r for r in results if r.status == SUCCESS],
        "failed": [r for r in results if r.status == FAILED],
        "UNCERTAIN - verify on the switch": [r for r in results if r.status in UNCERTAIN],
        "skipped": [r for r in results if r.status == SKIPPED],
    }
    print("\nSummary")
    for title, items in groups.items():
        print(f"  {title}: {len(items)}")
        for r in items:
            log = f"  ({r.log_path})" if r.log_path else ""
            print(f"    {r.ip:<16} {r.status:<34} {r.message}{log}")
    return 0 if not groups["failed"] and not groups["UNCERTAIN - verify on the switch"] else 1

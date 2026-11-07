"""--detect: read hostname, platform, physical interfaces and stack members. Changes nothing on the switch."""

import re
from datetime import datetime, timezone

from .config import Settings
from .interface_map import InterfaceMap, parse_show_ip_interface_brief
from .inventory import load_discovery, read_inventory, save_discovery, write_inventory
from .netmiko_client import connect


def parse_hostname(running_hostname_line: str, prompt: str = "") -> str:
    match = re.search(r"^hostname\s+(\S+)", running_hostname_line, re.MULTILINE)
    if match:
        return match.group(1)
    return prompt.strip().rstrip("#>")


def parse_show_version(output: str) -> tuple[str, str]:
    """(model, software version) from 'show version'."""
    model = ""
    for pattern in (r"Model [Nn]umber\s*:\s*(\S+)", r"^cisco\s+(\S+)\s+\(", r"^[Cc]isco\s+(WS-\S+|C\d\S+|IE-\S+)"):
        match = re.search(pattern, output, re.MULTILINE)
        if match:
            model = match.group(1)
            break
    version = ""
    match = re.search(r"Version\s+([\w.()-]+)", output)
    if match:
        version = match.group(1).rstrip(",")
    return model, version


def parse_show_switch(output: str) -> list[int]:
    """Stack member numbers from 'show switch' (empty when the command is not supported)."""
    if "Invalid input" in output or "% " in output[:200]:
        return []
    members = []
    for line in output.splitlines():
        match = re.match(r"^\s*\*?\s*(\d+)\s+(Active|Standby|Member|Master|Slave)\b", line)
        if match:
            members.append(int(match.group(1)))
    return sorted(set(members))


def discover_device(conn) -> dict:
    """Run the read-only discovery commands on an open session."""
    prompt = conn.find_prompt()
    hostname = parse_hostname(conn.send_command("show running-config | include ^hostname"), prompt)
    model, version = parse_show_version(conn.send_command("show version"))
    interfaces = parse_show_ip_interface_brief(conn.send_command("show ip interface brief"))
    members = parse_show_switch(conn.send_command("show switch"))
    return {
        "hostname": hostname,
        "model": model,
        "version": version,
        "stack_members": members,
        "interfaces": interfaces,
        "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_interface_report(settings: Settings, discovery: dict) -> None:
    lines = []
    for ip, data in discovery.items():
        if data.get("error"):
            lines += [f"{ip} - ERROR: {data['error']}", ""]
            continue
        title = f"{ip} - {data.get('hostname') or '?'}"
        if data.get("model"):
            title += f" ({data['model']})"
        lines.append(title)
        imap = InterfaceMap(data.get("interfaces", []))
        lines += imap.report_lines() or ["    no physical interfaces found"]
        lines.append("")
    settings.interface_report.write_text("\n".join(lines), encoding="utf-8")


def run_detect(settings: Settings) -> int:
    entries = read_inventory(settings.inventory_file)
    discovery = load_discovery(settings.discovery_file)
    failed = 0
    for entry in entries:
        print(f"{entry.ip:<16} ", end="", flush=True)
        try:
            conn = connect(entry.ip, settings)
            try:
                data = discover_device(conn)
            finally:
                conn.disconnect()
        except Exception as exc:  # report and continue with the next switch
            failed += 1
            previous = discovery.get(entry.ip, {})
            previous["error"] = f"{type(exc).__name__}: {exc}".splitlines()[0]
            discovery[entry.ip] = previous
            print(f"FAILED  {previous['error']}")
            continue

        discovery[entry.ip] = data
        ports = sum(len(v) for v in InterfaceMap(data["interfaces"]).members.values())
        stack = f", stack members {data['stack_members']}" if len(data["stack_members"]) > 1 else ""
        print(f"ok      {data['hostname']} {data['model']} {data['version']}, {ports} physical ports{stack}")
        if data["hostname"] and not entry.hostname:
            entry.hostname = data["hostname"]

    write_inventory(settings.inventory_file, entries)
    save_discovery(settings.discovery_file, discovery)
    write_interface_report(settings, discovery)
    print(f"\nWritten: {settings.discovery_file}, {settings.interface_report}, hostnames in {settings.inventory_file}")
    return 1 if failed else 0

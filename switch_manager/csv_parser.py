"""Read a ';'-delimited switch CSV and turn it into a validated SwitchSpec.

Header: Vlan;Description;IP Address;Netmask;Switch;Ports  (case and spaces do not matter)

Rules
  * row with IP address + netmask          -> Layer 3 VLAN (SVI with that address)
  * row without IP details                 -> Layer 2 VLAN
  * description contains 'management'/'mgt' -> management VLAN (usually no ports)
  * description contains 'trunk'/'uplink'  -> trunk ports; a filled-in Vlan field is the allowed-VLAN list
  * all (non-management) VLANs have an IP  -> 'ip routing'
  * Layer 2 switch                         -> 'ip default-gateway' derived from the management VLAN
Lists such as 1,5,9,220,1580-1590,2034 are accepted for ports and trunk VLANs.
"""

import csv
import ipaddress
import re
from pathlib import Path

from .models import PortGroup, SwitchSpec, ValidationError, Vlan

MGMT_RE = re.compile(r"management|mgt", re.IGNORECASE)
TRUNK_RE = re.compile(r"trunk|uplink", re.IGNORECASE)

HEADER_ALIASES = {
    "vlan": "vlan",
    "vlans": "vlan",
    "description": "description",
    "ipaddress": "ip",
    "ip": "ip",
    "netmask": "mask",
    "subnetmask": "mask",
    "mask": "mask",
    "switch": "switch",
    "member": "switch",
    "ports": "ports",
    "port": "ports",
}
REQUIRED = ("vlan", "description", "ip", "mask", "switch", "ports")


def parse_number_list(expr: str, low: int = 1, high: int = 4094, what: str = "value") -> list[int]:
    """'1,5,9,220,1580-1590,2034' -> sorted unique list. Raises ValueError with a clear message."""
    numbers: set[int] = set()
    for part in re.split(r"[,\s]+", expr.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"invalid {what} '{part}' in '{expr}'")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            raise ValueError(f"invalid {what} range '{part}' (start is greater than end)")
        if start < low or end > high:
            raise ValueError(f"{what} '{part}' outside {low}-{high}")
        numbers.update(range(start, end + 1))
    if not numbers:
        raise ValueError(f"empty {what} list")
    return sorted(numbers)


def format_number_list(numbers: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3,5,7-8' (also what IOS accepts for allowed VLANs)."""
    out: list[str] = []
    values = sorted(set(numbers))
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1] == values[j] + 1:
            j += 1
        out.append(str(values[i]) if i == j else f"{values[i]}-{values[j]}")
        i = j + 1
    return ",".join(out)


def read_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValidationError(f"{path.name}: file is empty")

    reader = csv.reader(lines, delimiter=";")
    header = [HEADER_ALIASES.get(re.sub(r"[\s_-]", "", h).lower(), h.strip().lower()) for h in next(reader)]
    missing = [col for col in REQUIRED if col not in header]
    if missing:
        raise ValidationError(f"{path.name}: missing column(s) {', '.join(missing)} - expected Vlan;Description;IP Address;Netmask;Switch;Ports")

    rows = []
    for number, values in enumerate(reader, start=2):
        values = [v.strip() for v in values] + [""] * (len(header) - len(values))
        rows.append((number, dict(zip(header, values))))
    return rows


def parse_switch_csv(path: Path, mgmt_gateway: str = "first") -> SwitchSpec:
    """Parse and validate one CSV. Collects every problem before raising ValidationError."""
    path = Path(path)
    spec = SwitchSpec(source=path)
    problems: list[str] = []

    for line, row in read_rows(path):
        where = f"{path.name} line {line}"
        description = row["description"]
        member_text = row["switch"] or "1"
        try:
            member = int(member_text)
            if not 1 <= member <= 16:
                raise ValueError
        except ValueError:
            problems.append(f"{where}: Switch '{member_text}' is not a stack member number (1-16)")
            continue

        ports: list[int] = []
        if row["ports"]:
            try:
                ports = parse_number_list(row["ports"], 1, 999, "port")
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
                continue

        # Trunk / uplink rows
        if TRUNK_RE.search(description):
            if not ports:
                problems.append(f"{where}: trunk/uplink row without ports")
                continue
            if row["ip"] or row["mask"]:
                problems.append(f"{where}: trunk/uplink row cannot have IP details")
                continue
            allowed = None
            if row["vlan"]:
                try:
                    allowed = parse_number_list(row["vlan"], 1, 4094, "VLAN")
                except ValueError as exc:
                    problems.append(f"{where}: {exc}")
                    continue
            spec.port_groups.append(PortGroup(line, member, ports, description, "trunk", allowed_vlans=allowed))
            continue

        # VLAN rows
        try:
            vlan_id = int(row["vlan"])
            if not 1 <= vlan_id <= 4094:
                raise ValueError
        except ValueError:
            problems.append(f"{where}: Vlan '{row['vlan']}' must be one VLAN number 1-4094 (lists are only allowed on trunk/uplink rows)")
            continue

        svi = None
        if row["ip"] or row["mask"]:
            if not (row["ip"] and row["mask"]):
                problems.append(f"{where}: IP address and netmask must both be filled in")
                continue
            try:
                svi = ipaddress.IPv4Interface(f"{row['ip']}/{row['mask']}")
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
                continue
            if svi.ip in (svi.network.network_address, svi.network.broadcast_address) and svi.network.prefixlen < 31:
                problems.append(f"{where}: {svi.ip} is the network/broadcast address of {svi.network}")
                continue

        is_mgmt = bool(MGMT_RE.search(description))
        vlan = spec.vlans.get(vlan_id)
        if vlan is None:
            vlan = spec.vlans[vlan_id] = Vlan(vlan_id, description, svi, is_mgmt)
        else:
            if svi and vlan.svi and svi != vlan.svi:
                problems.append(f"{where}: VLAN {vlan_id} already has IP {vlan.svi}, cannot also be {svi}")
                continue
            vlan.svi = vlan.svi or svi
            vlan.management = vlan.management or is_mgmt
            if description and description.lower() != vlan.name.lower():
                spec.warnings.append(f"{where}: VLAN {vlan_id} is named '{vlan.name}' earlier; using that name")

        if ports:
            spec.port_groups.append(PortGroup(line, member, ports, description, "access", vlan_id=vlan_id))

    # A port may only be configured once
    seen: dict[tuple[int, int], int] = {}
    for group in spec.port_groups:
        for port in group.ports:
            key = (group.member, port)
            if key in seen:
                problems.append(f"{path.name} line {group.line}: switch {group.member} port {port} is already used on line {seen[key]}")
            else:
                seen[key] = group.line

    # Management VLAN
    mgmt = [v for v in spec.vlans.values() if v.management]
    if len(mgmt) > 1:
        problems.append(f"{path.name}: more than one management VLAN: {', '.join(str(v.vlan_id) for v in mgmt)}")
    elif mgmt:
        spec.management = mgmt[0]

    # Trunk filters must only allow VLANs that exist on this switch
    for group in spec.trunk_groups:
        unknown = [v for v in group.allowed_vlans or [] if v not in spec.vlans and v != 1]
        if unknown:
            spec.warnings.append(f"{path.name} line {group.line}: trunk allows VLAN(s) {format_number_list(unknown)} that are not defined in this CSV")

    # Layer 3 when every (non-management) VLAN has an IP address
    data_vlans = [v for v in spec.vlans.values() if not v.management]
    spec.layer3 = bool(data_vlans) and all(v.svi for v in data_vlans)
    if not spec.layer3 and any(v.svi for v in data_vlans):
        spec.warnings.append(f"{path.name}: some VLANs have an IP address but not all - routing is NOT enabled (Layer 2 switch)")

    if not spec.layer3:
        if not spec.management:
            spec.warnings.append(f"{path.name}: Layer 2 switch without management VLAN - no default gateway configured")
        elif not spec.management.svi:
            problems.append(f"{path.name}: management VLAN {spec.management.vlan_id} needs an IP address and netmask")
        else:
            try:
                spec.default_gateway = derive_gateway(spec.management.svi, mgmt_gateway)
            except ValueError as exc:
                problems.append(f"{path.name}: {exc}")

    if not spec.vlans and not spec.port_groups:
        problems.append(f"{path.name}: no VLANs or ports defined")
    if problems:
        raise ValidationError(problems)
    return spec


def derive_gateway(svi: ipaddress.IPv4Interface, rule: str = "first") -> ipaddress.IPv4Address:
    """Default gateway of a Layer 2 switch, from its management VLAN: first/last usable or an explicit address."""
    network = svi.network
    hosts = list(network.hosts()) if network.num_addresses <= 65536 else None
    rule = (rule or "first").strip().lower()
    if rule == "first":
        gateway = hosts[0] if hosts else network.network_address + 1
    elif rule == "last":
        gateway = hosts[-1] if hosts else network.broadcast_address - 1
    else:
        gateway = ipaddress.IPv4Address(rule)
        if gateway not in network:
            raise ValueError(f"MGMT_GATEWAY {gateway} is not in the management network {network}")
    if gateway == svi.ip:
        raise ValueError(f"the derived default gateway {gateway} is the switch's own management address - set MGMT_GATEWAY to 'last' or an explicit address")
    return gateway

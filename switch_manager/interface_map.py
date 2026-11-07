"""Map CSV 'Switch' + 'Ports' numbers to the physical interfaces discovered on the real device.

Interface names are model dependent (Fa0/1, Gi0/1, Gi1/0/1, Te1/1/1, ...). Per stack member the
physical ports are put in one sequence - module, then speed, then port number - and CSV port N is the
N-th interface of that sequence. Examples:
  2960-24TT   : ports 1-24 = FastEthernet0/1-24, 25-26 = GigabitEthernet0/1-2
  3850-48 x2  : member 2 ports 1-48 = GigabitEthernet2/0/1-48, 49-52 = GigabitEthernet2/1/1-4, ...
A port that does not exist fails validation; nothing is generated for it.
"""

import re
from dataclasses import dataclass

from .models import ValidationError

# Physical Ethernet interface types, slowest first
TYPE_ORDER = {
    "Ethernet": 0,
    "FastEthernet": 1,
    "GigabitEthernet": 2,
    "TwoGigabitEthernet": 3,
    "FiveGigabitEthernet": 4,
    "TenGigabitEthernet": 5,
    "TwentyFiveGigE": 6,
    "FortyGigabitEthernet": 7,
    "HundredGigE": 8,
}
SHORT_NAMES = {
    "Eth": "Ethernet", "Et": "Ethernet",
    "Fa": "FastEthernet", "Gi": "GigabitEthernet", "Tw": "TwoGigabitEthernet",
    "Fi": "FiveGigabitEthernet", "Te": "TenGigabitEthernet", "Twe": "TwentyFiveGigE",
    "Fo": "FortyGigabitEthernet", "Hu": "HundredGigE",
}
NAME_RE = re.compile(r"^(?P<type>[A-Za-z]+?)(?P<nums>\d+(?:/\d+){1,3})$")


@dataclass(frozen=True)
class PhysicalInterface:
    type: str
    nums: tuple[int, ...]

    @property
    def name(self) -> str:
        return self.type + "/".join(str(n) for n in self.nums)

    @property
    def member(self) -> int:
        # Gi2/0/1 -> member 2; Fa0/1 and Fa0/0/1 (slot 0, not a stack) -> member 1
        return self.nums[0] if len(self.nums) >= 3 and self.nums[0] >= 1 else 1

    @property
    def sort_key(self) -> tuple:
        module = self.nums[-2]
        return (module, TYPE_ORDER[self.type], self.nums[-1])

    @property
    def prefix(self) -> str:
        """Everything except the last number: 'GigabitEthernet1/0/'."""
        return self.type + "".join(f"{n}/" for n in self.nums[:-1])


def parse_interface(name: str) -> PhysicalInterface | None:
    """Return a PhysicalInterface for an Ethernet port, None for Vlan/Loopback/Port-channel/etc."""
    match = NAME_RE.match(name.strip())
    if not match:
        return None
    type_ = match.group("type")
    type_ = SHORT_NAMES.get(type_, type_)
    if type_ not in TYPE_ORDER:
        return None
    nums = tuple(int(n) for n in match.group("nums").split("/"))
    return PhysicalInterface(type_, nums)


def parse_show_ip_interface_brief(output: str) -> list[str]:
    """First column of 'show ip interface brief' (all interfaces, physical or not)."""
    names = []
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] == "Interface" or "#" in parts[0]:
            continue
        if re.match(r"^[A-Za-z][\w-]*\d", parts[0]):
            names.append(parts[0])
    return names


class InterfaceMap:
    def __init__(self, interface_names: list[str]):
        self.members: dict[int, list[PhysicalInterface]] = {}
        for name in interface_names:
            iface = parse_interface(name)
            if iface:
                self.members.setdefault(iface.member, []).append(iface)
        for ports in self.members.values():
            ports.sort(key=lambda i: i.sort_key)

    @property
    def is_empty(self) -> bool:
        return not self.members

    def resolve(self, member: int, ports: list[int], where: str = "") -> list[PhysicalInterface]:
        prefix = f"{where}: " if where else ""
        if member not in self.members:
            have = ", ".join(str(m) for m in sorted(self.members)) or "none"
            raise ValidationError(f"{prefix}switch {member} does not exist on this device (members: {have})")
        sequence = self.members[member]
        bad = [p for p in ports if p > len(sequence)]
        if bad:
            raise ValidationError(f"{prefix}switch {member} has only {len(sequence)} physical ports, port(s) {','.join(map(str, bad))} do not exist")
        return [sequence[p - 1] for p in ports]

    def report_lines(self) -> list[str]:
        """Human readable: '  ports 1-24   FastEthernet0/1-24'."""
        lines = []
        for member in sorted(self.members):
            if len(self.members) > 1:
                lines.append(f"  switch {member}")
            index = 1
            for block in contiguous_blocks(self.members[member]):
                first, last = index, index + len(block) - 1
                ports = f"{first}" if first == last else f"{first}-{last}"
                lines.append(f"    ports {ports:<8} {block_name(block)}")
                index = last + 1
        return lines


def contiguous_blocks(interfaces: list[PhysicalInterface]) -> list[list[PhysicalInterface]]:
    """Split into runs with the same prefix and consecutive last numbers (input order kept)."""
    blocks: list[list[PhysicalInterface]] = []
    for iface in interfaces:
        if blocks and blocks[-1][-1].prefix == iface.prefix and blocks[-1][-1].nums[-1] + 1 == iface.nums[-1]:
            blocks[-1].append(iface)
        else:
            blocks.append([iface])
    return blocks


def block_name(block: list[PhysicalInterface]) -> str:
    first, last = block[0], block[-1]
    return first.name if first == last else f"{first.name}-{last.nums[-1]}"


def interface_range_commands(interfaces: list[PhysicalInterface]) -> list[str]:
    """'interface range GigabitEthernet1/0/1 - 28' per contiguous block; 'interface X' for a single port."""
    ordered = sorted(interfaces, key=lambda i: (i.member, i.sort_key))
    commands = []
    for block in contiguous_blocks(ordered):
        if len(block) == 1:
            commands.append(f"interface {block[0].name}")
        else:
            commands.append(f"interface range {block[0].name} - {block[-1].nums[-1]}")
    return commands

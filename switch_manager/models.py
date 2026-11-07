"""Validated data models for one switch CSV."""

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path


class ValidationError(Exception):
    """One or more problems in the input; str() lists all of them."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


@dataclass
class Vlan:
    vlan_id: int
    name: str
    svi: ipaddress.IPv4Interface | None = None   # IP address + mask of the VLAN interface
    management: bool = False


@dataclass
class PortGroup:
    """Ports of one stack member that get the same configuration."""
    line: int
    member: int
    ports: list[int]
    description: str
    kind: str                                    # 'access' or 'trunk'
    vlan_id: int | None = None                   # access VLAN
    allowed_vlans: list[int] | None = None       # trunk filter; None = all VLANs


@dataclass
class SwitchSpec:
    source: Path
    vlans: dict[int, Vlan] = field(default_factory=dict)
    port_groups: list[PortGroup] = field(default_factory=list)
    layer3: bool = False                         # 'ip routing'
    management: Vlan | None = None
    default_gateway: ipaddress.IPv4Address | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def access_groups(self) -> list[PortGroup]:
        return [g for g in self.port_groups if g.kind == "access"]

    @property
    def trunk_groups(self) -> list[PortGroup]:
        return [g for g in self.port_groups if g.kind == "trunk"]

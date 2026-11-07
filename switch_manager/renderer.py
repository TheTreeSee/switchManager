"""Render a SwitchSpec into Cisco IOS configuration, in apply stages.

Stages (sent in this order by --apply):
  base        hostname (opt-in), VLANs
  access      access ports (no impact on the management path)
  management  SVIs, management VLAN, default gateway, ip routing, trunks/uplinks - sent last,
              because these can interrupt the SSH session
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .csv_parser import format_number_list
from .interface_map import InterfaceMap, interface_range_commands
from .models import SwitchSpec, ValidationError

# Catalyst families that support ISL and therefore need 'switchport trunk encapsulation dot1q'
ISL_CAPABLE = re.compile(r"3550|3560|3750|4500|4900|6500|6800", re.IGNORECASE)


@dataclass
class Stage:
    name: str
    commands: list[str]


@dataclass
class RenderedConfig:
    target: str
    stages: list[Stage]
    header: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_management_changes(self) -> bool:
        return any(s.name == "management" and s.commands for s in self.stages)

    @property
    def text(self) -> str:
        lines = list(self.header)
        for stage in self.stages:
            if not stage.commands:
                continue
            lines.append(f"! ---- stage: {stage.name}")
            lines.extend(stage.commands)
        lines.append("end")
        return "\n".join(lines) + "\n"

    def body(self) -> str:
        """Config without the header comment (stable across runs, used to compare renders)."""
        return "\n".join(c for s in self.stages for c in s.commands)


def needs_dot1q_encapsulation(setting: str, model: str | None) -> bool:
    if setting == "yes":
        return True
    if setting == "no":
        return False
    return bool(model and ISL_CAPABLE.search(model))


def render(spec: SwitchSpec, imap: InterfaceMap, target: str, *, hostname: str | None = None,
           model: str | None = None, trunk_encapsulation: str = "auto", set_hostname: bool = False) -> RenderedConfig:
    problems: list[str] = []
    resolved = []
    for group in spec.port_groups:
        try:
            resolved.append((group, imap.resolve(group.member, group.ports, f"{spec.source.name} line {group.line}")))
        except ValidationError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ValidationError(problems)

    base: list[str] = []
    if set_hostname:
        if not hostname or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", hostname):
            raise ValidationError(f"--set-hostname: inventory hostname '{hostname or ''}' is not a valid IOS hostname")
        base.append(f"hostname {hostname}")
    for vlan in sorted(spec.vlans.values(), key=lambda v: v.vlan_id):
        if vlan.vlan_id == 1:
            continue                       # VLAN 1 always exists and cannot be renamed
        base += [f"vlan {vlan.vlan_id}", f" name {ios_name(vlan.name)}", " exit"]

    access: list[str] = []
    for group, interfaces in resolved:
        if group.kind != "access":
            continue
        for command in interface_range_commands(interfaces):
            access += [
                command,
                f" description {ios_text(group.description)}",
                " switchport mode access",
                f" switchport access vlan {group.vlan_id}",
                " no shutdown",
                " exit",
            ]

    management: list[str] = []
    for vlan in sorted(spec.vlans.values(), key=lambda v: (not v.management, v.vlan_id)):
        if not vlan.svi:
            continue
        management += [
            f"interface Vlan{vlan.vlan_id}",
            f" description {ios_text(vlan.name)}",
            f" ip address {vlan.svi.ip} {vlan.svi.netmask}",
            " no shutdown",
            " exit",
        ]
    if spec.default_gateway:
        management.append(f"ip default-gateway {spec.default_gateway}")
    if spec.layer3:
        management.append("ip routing")

    dot1q = needs_dot1q_encapsulation(trunk_encapsulation, model)
    for group, interfaces in resolved:
        if group.kind != "trunk":
            continue
        allowed = format_number_list(group.allowed_vlans) if group.allowed_vlans else "all"
        for command in interface_range_commands(interfaces):
            management.append(command)
            management.append(f" description {ios_text(group.description)}")
            if dot1q:
                management.append(" switchport trunk encapsulation dot1q")
            management += [
                " switchport mode trunk",
                f" switchport trunk allowed vlan {allowed}",
                " no shutdown",
                " exit",
            ]

    mode = "Layer 3 (ip routing)" if spec.layer3 else "Layer 2" + (f" (default gateway {spec.default_gateway})" if spec.default_gateway else "")
    header = [
        f"! Generated by switchManager on {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}",
        f"! Target   : {target}" + (f" ({hostname})" if hostname else ""),
        f"! Input    : {spec.source.name}",
        f"! Mode     : {mode}",
        f"! VLANs    : {format_number_list(list(spec.vlans)) if spec.vlans else '-'}",
        "!",
        "configure terminal",
    ]
    return RenderedConfig(
        target=target,
        stages=[Stage("base", base), Stage("access", access), Stage("management", management)],
        header=header,
        warnings=list(spec.warnings),
    )


def ios_name(text: str) -> str:
    """VLAN names: max 32 characters, no spaces."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text.strip())[:32] or "VLAN"


def ios_text(text: str) -> str:
    return re.sub(r"[^\x20-\x7e]", "", text).strip()[:200] or "-"

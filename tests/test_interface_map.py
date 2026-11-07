import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_manager.interface_map import InterfaceMap, interface_range_commands, parse_interface, parse_show_ip_interface_brief
from switch_manager.models import ValidationError

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = json.loads((ROOT / "inventory" / "examples" / "discovery.json").read_text())


class Names(unittest.TestCase):
    def test_physical_names(self):
        for name, member in (("FastEthernet0/1", 1), ("Fa0/0/1", 1), ("GigabitEthernet0/1", 1),
                             ("Gi2/0/14", 2), ("TenGigabitEthernet1/1/1", 1), ("Te3/1/4", 3)):
            with self.subTest(name=name):
                iface = parse_interface(name)
                self.assertIsNotNone(iface)
                self.assertEqual(iface.member, member)

    def test_non_physical_ignored(self):
        for name in ("Vlan1", "Loopback0", "Port-channel1", "Null0", "Tunnel5"):
            self.assertIsNone(parse_interface(name))

    def test_show_ip_interface_brief(self):
        output = """Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES NVRAM  administratively down down
FastEthernet0/1        unassigned      YES unset  down                  down
GigabitEthernet0/1     unassigned      YES unset  up                    up
Switch#"""
        self.assertEqual(parse_show_ip_interface_brief(output), ["Vlan1", "FastEthernet0/1", "GigabitEthernet0/1"])


class Resolution(unittest.TestCase):
    def test_2960_sequence_continues_into_uplinks(self):
        imap = InterfaceMap(DISCOVERY["172.19.67.13"]["interfaces"])
        self.assertEqual([i.name for i in imap.resolve(1, [1, 24, 25, 26])],
                         ["FastEthernet0/1", "FastEthernet0/24", "GigabitEthernet0/1", "GigabitEthernet0/2"])

    def test_stack_member_two(self):
        imap = InterfaceMap(DISCOVERY["172.19.67.10"]["interfaces"])
        self.assertEqual([i.name for i in imap.resolve(2, [1, 48, 49])],
                         ["GigabitEthernet2/0/1", "GigabitEthernet2/0/48", "GigabitEthernet2/1/1"])

    def test_nonexistent_port_fails(self):
        imap = InterfaceMap(DISCOVERY["172.19.67.13"]["interfaces"])
        with self.assertRaises(ValidationError) as ctx:
            imap.resolve(1, [26, 27])
        self.assertIn("only 26 physical ports", str(ctx.exception))

    def test_nonexistent_member_fails(self):
        imap = InterfaceMap(DISCOVERY["172.19.67.13"]["interfaces"])
        with self.assertRaises(ValidationError) as ctx:
            imap.resolve(2, [1])
        self.assertIn("switch 2 does not exist", str(ctx.exception))

    def test_interface_ranges(self):
        imap = InterfaceMap(DISCOVERY["172.19.67.13"]["interfaces"])
        commands = interface_range_commands(imap.resolve(1, [1, 2, 3, 7, 25, 26]))
        self.assertEqual(commands, ["interface range FastEthernet0/1 - 3", "interface FastEthernet0/7",
                                    "interface range GigabitEthernet0/1 - 2"])

    def test_report(self):
        lines = InterfaceMap(DISCOVERY["172.19.67.13"]["interfaces"]).report_lines()
        self.assertIn("    ports 1-24     FastEthernet0/1-24", lines)
        self.assertIn("    ports 25-26    GigabitEthernet0/1-2", lines)


if __name__ == "__main__":
    unittest.main()

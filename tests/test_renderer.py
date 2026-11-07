import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_manager.csv_parser import parse_switch_csv
from switch_manager.interface_map import InterfaceMap
from switch_manager.models import ValidationError
from switch_manager.renderer import render
from tests.test_csv_parser import csv_file

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "input" / "examples"
DISCOVERY = json.loads((ROOT / "inventory" / "examples" / "discovery.json").read_text())


def rendered(ip: str, **kw):
    data = DISCOVERY[ip]
    spec = parse_switch_csv(EXAMPLES / f"{ip}.csv")
    return render(spec, InterfaceMap(data["interfaces"]), ip, hostname=data["hostname"], model=data["model"], **kw)


class Rendering(unittest.TestCase):
    def test_layer3_stack(self):
        cfg = rendered("172.19.67.10")
        text = cfg.text
        self.assertIn("ip routing", text)
        self.assertNotIn("ip default-gateway", text)
        self.assertIn("interface range GigabitEthernet2/0/41 - 48\n description CD-Clients-C2-3", text)
        self.assertIn("interface Vlan1982\n description CD-WIFI\n ip address 172.19.82.1 255.255.255.0", text)

    def test_layer2_access(self):
        text = rendered("172.19.67.13").text
        self.assertNotIn("ip routing", text)
        self.assertIn("ip default-gateway 172.19.67.1", text)
        self.assertIn("interface Vlan1967\n description CD-Management\n ip address 172.19.67.13 255.255.255.0", text)
        self.assertEqual(text.count("interface Vlan"), 1)

    def test_trunk_filter_and_no_dot1q_on_2960(self):
        text = rendered("172.19.67.14").text
        self.assertIn("interface range GigabitEthernet0/1 - 2\n description Uplink to BST-C-Core-2\n switchport mode trunk\n"
                      " switchport trunk allowed vlan 1967,1969,1978,1982", text)
        self.assertNotIn("encapsulation dot1q", text)

    def test_dot1q_on_3560(self):
        text = rendered("10.10.10.1").text
        self.assertIn(" switchport trunk encapsulation dot1q", text)
        self.assertIn(" switchport trunk allowed vlan all", text)

    def test_stages_put_management_last(self):
        cfg = rendered("172.19.67.14")
        names = [s.name for s in cfg.stages]
        self.assertEqual(names, ["base", "access", "management"])
        management = cfg.stages[2].commands
        self.assertIn("ip default-gateway 172.19.67.1", management)
        self.assertTrue(any("switchport mode trunk" in c for c in management))
        self.assertFalse(any("switchport mode trunk" in c for c in cfg.stages[1].commands))
        self.assertTrue(cfg.has_management_changes)

    def test_hostname_is_opt_in(self):
        self.assertNotIn("hostname ", rendered("172.19.67.13").body())
        self.assertIn("hostname BST-D-1-242", rendered("172.19.67.13", set_hostname=True).body())

    def test_port_missing_on_device_fails(self):
        spec = parse_switch_csv(csv_file("10;Users;;;1;20-30", "99;mgt;10.0.0.5;255.255.255.0;;"))
        with self.assertRaises(ValidationError) as ctx:
            render(spec, InterfaceMap(DISCOVERY["192.168.99.20"]["interfaces"]), "192.168.99.20")
        self.assertIn("only 10 physical ports", str(ctx.exception))

    def test_example_outputs_are_current(self):
        """output/examples must match what the code renders now (header line with the date excluded)."""
        for ip in DISCOVERY:
            with self.subTest(ip=ip):
                saved = (ROOT / "output" / "examples" / f"{ip}.cfg").read_text().splitlines()[1:]
                self.assertEqual(saved, rendered(ip).text.splitlines()[1:])


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_manager.csv_parser import derive_gateway, format_number_list, parse_number_list, parse_switch_csv
from switch_manager.models import ValidationError
from tests.fakes import tempdir

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "input" / "examples"
HEADER = "Vlan;Description;IP Address;Netmask;Switch;Ports"


def csv_file(*rows: str, header: str = HEADER) -> Path:
    path = tempdir() / "10.0.0.1.csv"
    path.write_text("\r\n".join([header, *rows]) + "\r\n", encoding="utf-8")
    return path


class NumberLists(unittest.TestCase):
    def test_assignment_example(self):
        numbers = parse_number_list("1,5,9,220,1580-1590,2034")
        self.assertEqual(numbers[:4], [1, 5, 9, 220])
        self.assertEqual(numbers[4:15], list(range(1580, 1591)))
        self.assertEqual(numbers[-1], 2034)
        self.assertEqual(format_number_list(numbers), "1,5,9,220,1580-1590,2034")

    def test_spaces_duplicates_and_order(self):
        self.assertEqual(parse_number_list(" 3, 1-2 ,2 "), [1, 2, 3])

    def test_invalid(self):
        for bad in ("10-5", "a", "1-", "0", "4095", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_number_list(bad)


class Scenarios(unittest.TestCase):
    def test_layer3_stack_from_assignment(self):
        spec = parse_switch_csv(EXAMPLES / "172.19.67.10.csv")
        self.assertTrue(spec.layer3)
        self.assertIsNone(spec.default_gateway)
        self.assertEqual(sorted(spec.vlans), [1966, 1968, 1969, 1971, 1972, 1978, 1982])
        self.assertEqual(str(spec.vlans[1982].svi), "172.19.82.1/24")
        self.assertEqual({g.member for g in spec.access_groups}, {1, 2})

    def test_layer2_access_from_assignment(self):
        spec = parse_switch_csv(EXAMPLES / "172.19.67.13.csv")
        self.assertFalse(spec.layer3)
        self.assertEqual(spec.management.vlan_id, 1967)
        self.assertEqual(str(spec.default_gateway), "172.19.67.1")

    def test_trunk_with_vlan_filter(self):
        spec = parse_switch_csv(EXAMPLES / "172.19.67.14.csv")
        trunk = spec.trunk_groups[0]
        self.assertEqual(trunk.allowed_vlans, [1967, 1969, 1978, 1982])
        self.assertEqual(trunk.ports, [25, 26])

    def test_trunk_without_filter_allows_all(self):
        spec = parse_switch_csv(EXAMPLES / "10.10.10.1.csv")
        self.assertIsNone(spec.trunk_groups[0].allowed_vlans)
        self.assertEqual(spec.trunk_groups[1].allowed_vlans, [10, 20, 30])
        self.assertTrue(spec.layer3)

    def test_management_only(self):
        spec = parse_switch_csv(EXAMPLES / "192.168.99.20.csv")
        self.assertFalse(spec.layer3)
        self.assertEqual(str(spec.default_gateway), "192.168.99.1")

    def test_header_variants(self):
        spec = parse_switch_csv(csv_file("10;Users;;;1;1-4", "99;mgt;10.0.99.5;255.255.255.0;;",
                                         header="vlan; description; ip address; subnetmask; switch; ports;"))
        self.assertEqual(spec.management.vlan_id, 99)

    def test_mixed_ip_is_layer2_with_warning(self):
        spec = parse_switch_csv(csv_file("10;Users;10.0.10.1;255.255.255.0;1;1-4", "20;Guests;;;1;5-8",
                                         "99;MGT;10.0.99.5;255.255.255.0;;"))
        self.assertFalse(spec.layer3)
        self.assertTrue(any("routing is NOT enabled" in w for w in spec.warnings))


class Validation(unittest.TestCase):
    def assertProblem(self, fragment: str, *rows: str):
        with self.assertRaises(ValidationError) as ctx:
            parse_switch_csv(csv_file(*rows))
        self.assertIn(fragment, str(ctx.exception))

    def test_port_used_twice(self):
        self.assertProblem("already used on line 2", "10;A;;;1;1-5", "20;B;;;1;5-8", "99;mgt;10.0.0.5;255.255.255.0;;")

    def test_bad_range(self):
        self.assertProblem("start is greater than end", "10;A;;;1;8-1")

    def test_vlan_list_only_on_trunks(self):
        self.assertProblem("lists are only allowed on trunk", "10,20;Users;;;1;1-4")

    def test_two_management_vlans(self):
        self.assertProblem("more than one management VLAN", "98;mgt-a;10.0.98.5;255.255.255.0;;", "99;mgt-b;10.0.99.5;255.255.255.0;;")

    def test_ip_without_mask(self):
        self.assertProblem("must both be filled in", "10;A;10.0.0.1;;1;1")

    def test_conflicting_svi(self):
        self.assertProblem("already has IP", "10;A;10.0.0.1;255.255.255.0;1;1", "10;A;10.0.1.1;255.255.255.0;2;1")

    def test_management_needs_ip_on_layer2(self):
        self.assertProblem("needs an IP address", "10;Users;;;1;1-4", "99;mgt;;;;")

    def test_trunk_needs_ports(self):
        self.assertProblem("trunk/uplink row without ports", "10;A;;;1;1", "10;uplink;;;1;")

    def test_missing_column(self):
        with self.assertRaises(ValidationError) as ctx:
            parse_switch_csv(csv_file("10;A;1", header="Vlan;Description;Ports"))
        self.assertIn("missing column", str(ctx.exception))

    def test_gateway_rules(self):
        import ipaddress
        svi = ipaddress.IPv4Interface("10.0.0.10/24")
        self.assertEqual(str(derive_gateway(svi, "first")), "10.0.0.1")
        self.assertEqual(str(derive_gateway(svi, "last")), "10.0.0.254")
        self.assertEqual(str(derive_gateway(svi, "10.0.0.250")), "10.0.0.250")
        with self.assertRaises(ValueError):
            derive_gateway(ipaddress.IPv4Interface("10.0.0.1/24"), "first")
        with self.assertRaises(ValueError):
            derive_gateway(svi, "192.168.1.1")


if __name__ == "__main__":
    unittest.main()

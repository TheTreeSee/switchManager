"""Staged apply, rollback timer, connection-loss states, batch and TFTP backup against a fake switch."""

import io
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_manager import apply as ap
from switch_manager.backup import TftpReceiver, run_backup
from switch_manager.batch import plan_batch, run_batch
from switch_manager.models import ValidationError
from tests.fakes import SwitchState, connector_for, make_settings, tempdir, tftp_upload

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "input" / "examples"
IP = "172.19.67.14"          # 2960 with access ports, management VLAN and an uplink trunk


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.settings = make_settings(self.tmp)
        self.csv = self.settings.input_dir / f"{IP}.csv"
        shutil.copy(EXAMPLES / f"{IP}.csv", self.csv)
        self.state = SwitchState()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_apply(self, **kw):
        kw.setdefault("confirm", False)
        return ap.apply_to_switch(IP, self.csv, self.settings, connector=connector_for(self.state),
                                  sleep=lambda _s: None, echo=lambda _t: None, **kw)

    def sent(self) -> list[str]:
        return self.state.history

    def test_success_with_rollback_timer(self):
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.SUCCESS, result.message)
        h = self.sent()
        # timer first, then VLANs, access ports, management path, save, cancel
        self.assertLess(h.index("reload in 10"), h.index("vlan 1982"))
        self.assertLess(h.index("vlan 1982"), h.index("interface range FastEthernet0/1 - 12"))
        self.assertLess(h.index("interface range FastEthernet0/1 - 12"), h.index("interface Vlan1967"))
        self.assertLess(h.index("ip default-gateway 172.19.67.1"), h.index("write memory"))
        self.assertLess(h.index("write memory"), h.index("reload cancel"))
        self.assertFalse(self.state.reload_pending)
        self.assertIn("ip default-gateway 172.19.67.1", self.state.startup)
        self.assertTrue(result.output_path.is_file())
        log = result.log_path.read_text()
        for part in ("sha256", "stage management: sent", "device output", "result SUCCESS", "rollback reload scheduled"):
            self.assertIn(part, log)

    def test_existing_reload_is_not_replaced(self):
        self.state.reload_pending = True
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.FAILED)
        self.assertIn("already scheduled", result.message)
        self.assertNotIn("vlan 1982", self.sent())

    def test_rejected_command_stops_before_management_path(self):
        self.state.reject_line = " switchport access vlan 1969"
        result = self.run_apply()
        self.assertEqual(result.status, ap.FAILED)
        self.assertIn("rejected commands in stage 'access'", result.message)
        self.assertNotIn("interface Vlan1967", self.sent())
        self.assertNotIn("write memory", self.sent())

    def test_connection_lost_with_timer(self):
        self.state.drop_on_line = "ip default-gateway 172.19.67.1"
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.CONNECTION_LOST_ROLLBACK_PENDING)
        self.assertIn("last confirmed step", result.message)

    def test_connection_lost_without_timer(self):
        self.state.drop_on_line = "ip default-gateway 172.19.67.1"
        result = self.run_apply()
        self.assertEqual(result.status, ap.CONNECTION_LOST_STATE_UNKNOWN)

    def test_connection_lost_during_save(self):
        self.state.drop_on_save = True
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.SAVE_RESULT_UNKNOWN)
        self.assertIn("NOT a guaranteed rollback", result.message)

    def test_reload_cancel_not_confirmed(self):
        self.state.reload_cancel_ignored = True
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.RELOAD_CANCEL_UNKNOWN)

    def test_reload_cancel_connection_drop(self):
        self.state.drop_on_reload_cancel = True
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.RELOAD_CANCEL_UNKNOWN)

    def test_reconnects_after_management_change(self):
        self.state.drop_after_config = True
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.SUCCESS, result.message)
        self.assertGreaterEqual(self.state.sessions, 2)
        self.assertIn("reconnected on attempt 1", result.log_path.read_text())

    def test_reconnect_fails(self):
        self.state.drop_after_config = True
        self.state.refuse_connections_after = 1
        result = self.run_apply(rollback_minutes=10)
        self.assertEqual(result.status, ap.CONNECTION_LOST_ROLLBACK_PENDING)

    def test_port_missing_on_live_switch(self):
        self.state.interfaces = ["Vlan1"] + [f"FastEthernet0/{i}" for i in range(1, 9)]
        result = self.run_apply()
        self.assertEqual(result.status, ap.FAILED)
        self.assertIn("live interfaces", result.message)
        self.assertNotIn("vlan 1982", self.sent())

    def test_connection_refused(self):
        self.state.refuse_connections_after = 0
        result = self.run_apply()
        self.assertEqual(result.status, ap.FAILED)
        self.assertIn("connection failed", result.message)

    def test_confirmation_declined(self):
        import builtins
        original = builtins.input
        builtins.input = lambda _prompt="": "no"
        try:
            result = self.run_apply(confirm=True)
        finally:
            builtins.input = original
        self.assertEqual(result.status, ap.SKIPPED)
        self.assertNotIn("vlan 1982", self.sent())

    def test_backup_after_apply(self):
        result = self.run_apply(backup=True)
        self.assertEqual(result.status, ap.SUCCESS, result.message)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(result.backup_path.read_bytes(), self.state.tftp_payload)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.settings = make_settings(self.tmp)
        self.settings.backup_after_apply = False
        shutil.copy(ROOT / "inventory" / "examples" / "discovery.json", self.settings.discovery_file)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def inventory(self, *ips):
        self.settings.inventory_file.write_text("ip;hostname;note\n" + "".join(f"{ip};;\n" for ip in ips))

    def add_input(self, ip, name=None):
        shutil.copy(EXAMPLES / f"{ip}.csv", self.settings.input_dir / (name or f"{ip}.csv"))

    def test_plan_follows_inventory_order(self):
        self.inventory("172.19.67.14", "172.19.67.13", "10.10.10.1")
        self.add_input("172.19.67.13")
        self.add_input("172.19.67.14")
        targets, without = plan_batch(self.settings)
        self.assertEqual([e.ip for e, _ in targets], ["172.19.67.14", "172.19.67.13"])
        self.assertEqual([e.ip for e in without], ["10.10.10.1"])

    def test_plan_rejects_bad_names_and_unknown_targets(self):
        self.inventory("172.19.67.13")
        self.add_input("172.19.67.13", "BST-D-1-242.csv")
        self.add_input("172.19.67.14")
        with self.assertRaises(ValidationError) as ctx:
            plan_batch(self.settings)
        text = str(ctx.exception)
        self.assertIn("file name must be the switch IPv4 address", text)
        self.assertIn("172.19.67.14 is not in switches.csv", text)

    def test_examples_folder_is_never_read(self):
        self.inventory("172.19.67.13")
        (self.settings.input_dir / "examples").mkdir()
        shutil.copy(EXAMPLES / "172.19.67.13.csv", self.settings.input_dir / "examples" / "172.19.67.13.csv")
        with self.assertRaises(ValidationError):
            plan_batch(self.settings)

    def test_partial_failure_summary_and_exit_code(self):
        self.inventory("172.19.67.13", "172.19.67.14")
        self.add_input("172.19.67.13")
        self.add_input("172.19.67.14")
        states = {"172.19.67.13": SwitchState(), "172.19.67.14": SwitchState()}
        states["172.19.67.14"].reject_line = " switchport access vlan 1969"
        self.settings.batch_max_workers = 2

        def connector(ip, settings):
            return connector_for(states[ip])(ip, settings)

        out = io.StringIO()
        with redirect_stdout(out):
            code = run_batch(self.settings, rollback_minutes=None, set_hostname=False, yes=True, backup=False, connector=connector)
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("succeeded: 1", text)
        self.assertIn("failed: 1", text)
        self.assertIn("write memory", states["172.19.67.13"].history)
        self.assertEqual(len(list(self.settings.logs_dir.glob("*_apply.log"))), 2)


class TftpTests(unittest.TestCase):
    def test_receives_multi_block_file(self):
        tmp = tempdir()
        payload = bytes(range(256)) * 9            # 2304 bytes = 4 full blocks + 1 short
        receiver = TftpReceiver("127.0.0.1", 0, "sw.cfg", tmp / "sw.cfg", allowed_host="127.0.0.1").start()
        try:
            tftp_upload("127.0.0.1", receiver.port, "sw.cfg", payload)
            self.assertTrue(receiver.wait(3))
        finally:
            receiver.close()
        self.assertIsNone(receiver.error)
        self.assertEqual((tmp / "sw.cfg").read_bytes(), payload)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_backup_only(self):
        tmp = tempdir()
        settings = make_settings(tmp)
        state = SwitchState(hostname="BST-D-2-118")
        path = run_backup("127.0.0.1", settings, conn=connector_for(state)("127.0.0.1", settings), hostname="BST-D-2-118")
        self.assertTrue(path.name.startswith("127.0.0.1_BST-D-2-118_"))
        self.assertEqual(path.read_bytes(), state.tftp_payload)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

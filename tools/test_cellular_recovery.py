#!/usr/bin/env python3
"""Offline cellular-supervisor tests. No modem, routes, sudo, or network used."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("cellular_supervisor", str(ROOT / "scripts/serv-cellular-supervisor"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cell = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = cell
loader.exec_module(cell)

SETTINGS = """IPv4 address: 100.64.17.243
IPv4 subnet mask: 255.255.255.248
IPv4 gateway address: 100.64.17.244
IPv4 primary DNS: 1.1.1.1
IPv4 secondary DNS: 1.0.0.1
MTU: 1428
"""

# qmicli 1.36.0 output shape; CTL/WDS versions observed on the bench modem.
CONTROL_INFO = """[/dev/cdc-wdm0] Supported versions:
\tctl (1.5)
\twds (1.67)
\tdms (1.13)
"""
ALLOCATED_CLIENT = """[/dev/cdc-wdm0] Client ID not released:
\tService: 'wds'
\t    CID: '17'
"""


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.addresses = []
        self.probes = []
        self.default_probe = cell.Result(0, "404")
        self.fail = {}
        self.metrics = set()
        self.allocated = 0

    def run(self, args, timeout=10):
        self.calls.append((list(args), timeout))
        for token, result in self.fail.items():
            if token in args:
                return result
        if args[0].endswith("qmicli"):
            if "--version" in args:
                return cell.Result(0, "qmicli 1.36.0\nCopyright (C) libqmi authors\n")
            if "--get-service-version-info" in args:
                return cell.Result(0, CONTROL_INFO)
            option = next(a for a in args if a.startswith("--wds-"))
            if option == "--wds-noop":
                if not any(a.startswith("--client-cid=") for a in args):
                    self.allocated += 1
                    return cell.Result(0, ALLOCATED_CLIENT)
                return cell.Result(0)
            if option.startswith("--wds-start-network="):
                return cell.Result(0, "Network started\n Packet data handle: '123456789'\n")
            if option == "--wds-get-current-settings":
                return cell.Result(0, SETTINGS)
            if option == "--wds-get-packet-service-status":
                return cell.Result(0, "Connection status: 'connected'")
            return cell.Result(0)
        if args[0].endswith("curl"):
            return self.probes.pop(0) if self.probes else self.default_probe
        if args[0].endswith("ip"):
            if "-j" in args:
                return cell.Result(0, json.dumps([{"addr_info": self.addresses}]))
            if "addr" in args and "replace" in args:
                addr, prefix = args[args.index("replace") + 1].split("/")
                self.addresses = [{"family":"inet", "local":addr, "prefixlen":int(prefix)}]
            if "addr" in args and "del" in args:
                addr = args[args.index("del") + 1].split("/")[0]
                self.addresses = [a for a in self.addresses if a["local"] != addr]
            if "route" in args:
                if "flush" in args:
                    self.metrics.clear()
                elif "metric" in args:
                    metric = int(args[args.index("metric") + 1])
                    if "replace" in args:
                        self.metrics.add(metric)
                    elif "del" in args:
                        self.metrics.discard(metric)
            return cell.Result(0)
        return cell.Result(0)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="serv-cellular-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sys/wwan0/qmi").mkdir(parents=True)
        (self.root / "sys/wwan0/qmi/raw_ip").write_text("Y\n")
        self.cfg = cell.Config(runtime=str(self.root), qmi_lock=str(self.root / "qmi.lock"),
            sysfs=str(self.root / "sys"), wifi_only=str(self.root / ".wifi_only"))
        self.fake = FakeRunner()
        self.supervisor = self.new_supervisor()

    def new_supervisor(self):
        supervisor = cell.Supervisor(self.cfg, self.fake)
        supervisor.modem_fingerprint = lambda: {"boot_id":"one", "interface_index":"4"}
        return supervisor

    def start(self):
        self.supervisor.step()
        self.assertEqual(self.supervisor.state["cid"], 17)
        self.assertEqual(self.supervisor.state["pdh"], 123456789)

    def qmi_calls(self):
        # Version introspection does not communicate with the modem.
        return [a for a, _ in self.fake.calls if a[0].endswith("qmicli") and "--version" not in a]

    def stage(self):
        return json.loads((self.root / "status.json").read_text())["stage"]

    def test_parse_realistic_settings(self):
        settings = cell.Settings.parse(SETTINGS)
        self.assertEqual((settings.address, settings.prefix, settings.gateway, settings.mtu),
                         ("100.64.17.243", 29, "100.64.17.244", 1428))

    def test_invalid_settings_rejected(self):
        for old, new in [("255.255.255.248", "255.0.255.0"), ("100.64.17.243", "999.1.1.1"),
                         ("100.64.17.244", "0.0.0.0"), ("1428", "100"),
                         ("255.255.255.248", "0.0.0.0"), ("100.64.17.244", "100.64.17.243"),
                         ("255.255.255.248", "0.0.0.7"), ("100.64.17.244", "192.168.1.1")]:
            with self.subTest(new=new), self.assertRaises(cell.RecoveryError):
                cell.Settings.parse(SETTINGS.replace(old, new))

    def test_raw_ip_32_gateway_can_be_off_prefix(self):
        settings = cell.Settings.parse(SETTINGS.replace("255.255.255.248", "255.255.255.255"))
        self.assertEqual(settings.prefix, 32)

    def test_owned_client_used_for_start_settings_stop_release(self):
        self.start()
        calls = [a for a in self.qmi_calls() if "--get-service-version-info" not in a]
        self.assertIn("--client-no-release-cid", calls[0])
        self.assertTrue(all("--client-cid=17" in a for a in calls[1:]))
        self.supervisor.cleanup()
        calls = self.qmi_calls()
        self.assertIn("--wds-stop-network=123456789", calls[-2])
        self.assertIn("--client-cid=17", calls[-2])
        self.assertNotIn("--client-no-release-cid", calls[-1])
        self.assertNotIn("--wds-stop-network=0", str(calls))

    def test_start_timeout_releases_known_client(self):
        self.fake.fail["--wds-start-network=apn=super,ip-type=4"] = cell.Result(124)
        self.supervisor.step()
        self.assertIsNone(self.supervisor.state)
        self.assertEqual(self.fake.allocated, 1)
        self.assertIn("--client-cid=17", self.qmi_calls()[-1])
        self.assertNotIn("--client-no-release-cid", self.qmi_calls()[-1])
        self.assertEqual(self.fake.metrics, set())

    def test_start_timeout_with_handle_stops_exact_session(self):
        self.fake.fail["--wds-start-network=apn=super,ip-type=4"] = cell.Result(124, "Packet data handle: '987654321'")
        self.supervisor.step()
        self.assertTrue(any("--wds-stop-network=987654321" in a for a in self.qmi_calls()))
        self.assertIsNone(self.supervisor.state)

    def test_allocation_timeout_without_cid_latches_not_leaks(self):
        self.fake.fail["--wds-noop"] = cell.Result(124)
        self.supervisor.step()
        attempts = len(self.qmi_calls())
        for _ in range(4):
            self.supervisor.step()
        self.assertEqual(len(self.qmi_calls()), attempts)
        self.assertEqual(self.stage(), "allocation_uncertain_operator_recovery_required")
        replacement = self.new_supervisor()
        replacement.step()
        self.assertEqual(len(self.qmi_calls()), attempts)
        self.assertTrue(replacement.allocation_uncertain)
        replacement.modem_fingerprint = lambda: {"boot_id":"new", "interface_index":"6"}
        del self.fake.fail["--wds-noop"]
        replacement.step()
        self.assertEqual(replacement.state["pdh"], 123456789)

    def test_legacy_noeffect_does_not_invent_stop_handle(self):
        self.fake.fail["--wds-start-network=apn=super,ip-type=4"] = cell.Result(1, "", "NoEffect")
        self.supervisor.step()
        self.assertEqual(self.stage(), "connect_failed")
        self.assertFalse(any(any(v.startswith("--wds-stop-network=") for v in a) for a in self.qmi_calls()))
        self.assertIsNone(self.supervisor.state)

    def test_start_nonzero_with_cid_still_releases(self):
        self.fake.fail["--wds-noop"] = cell.Result(1, ALLOCATED_CLIENT,
            "error: couldn't open the QmiDevice: misleading extra error")
        self.supervisor.step()
        self.assertEqual(self.supervisor.state["cid"], 17)
        self.assertFalse(self.supervisor.allocation_uncertain)
        self.assertEqual(self.supervisor.last_failure["classification"], "known_cid_allocation_error")
        allocations = len([a for a in self.qmi_calls() if "--wds-noop" in a and not any(x.startswith("--client-cid=") for x in a)])
        self.supervisor.step()
        self.assertEqual(allocations, len([a for a in self.qmi_calls() if "--wds-noop" in a and not any(x.startswith("--client-cid=") for x in a)]))

    def test_control_not_ready_retries_without_retained_allocation_or_latch(self):
        self.fake.fail["--get-service-version-info"] = cell.Result(124, "", "control query timed out", True)
        delays = [self.supervisor.step() for _ in range(6)]
        self.assertEqual(self.fake.allocated, 0)
        self.assertFalse(self.supervisor.allocation_uncertain)
        self.assertFalse((self.root / "allocation-uncertain.json").exists())
        self.assertEqual(self.stage(), "modem_readiness_pending")
        self.assertEqual(delays, sorted(delays))
        self.assertLessEqual(max(delays), self.cfg.max_backoff)
        for args in self.qmi_calls():
            self.assertIn("--get-service-version-info", args)
            self.assertNotIn("--client-no-release-cid", args)
            self.assertNotIn("--device-open-sync", args)
            self.assertFalse(any(a.startswith("--client-cid=") for a in args))
        self.assertFalse(any(args[:3] == ["/usr/bin/ip", "link", "set"] for args, _ in self.fake.calls))

    def test_control_success_requires_valid_advertised_wds_version(self):
        for output in ["", "ctl (1.5)\n", "notwds (1.67)\n", "wds (0.67)\n",
                       "wds (65536.67)\n", "wds (1.65536)\n", "wds (1.67) garbage\n"]:
            with self.subTest(output=output):
                self.fake.fail["--get-service-version-info"] = cell.Result(0, output)
                self.supervisor.step()
                self.assertFalse(self.supervisor.allocation_uncertain)
                self.assertEqual(self.fake.allocated, 0)
                self.assertEqual(self.supervisor.last_failure["classification"], "wds_service_not_ready")

    def test_control_becomes_ready_then_allocates_and_connects(self):
        self.fake.fail["--get-service-version-info"] = cell.Result(1, CONTROL_INFO, "query failed")
        self.supervisor.step()
        self.assertEqual(self.fake.allocated, 0)
        del self.fake.fail["--get-service-version-info"]
        self.start()
        self.assertEqual(self.fake.allocated, 1)
        self.assertEqual(self.stage(), "probation")
        self.assertFalse(self.supervisor.allocation_uncertain)

    def test_control_readiness_precedes_every_new_retained_allocation(self):
        self.start()
        self.assertTrue(self.supervisor.cleanup())
        self.supervisor.step()
        calls = self.qmi_calls()
        allocations = [i for i, args in enumerate(calls) if "--wds-noop" in args
                       and not any(a.startswith("--client-cid=") for a in args)]
        self.assertEqual(len(allocations), 2)
        for index in allocations:
            self.assertIn("--get-service-version-info", calls[index - 1])
        # A healthy owned session does not need another readiness/allocation.
        before = len([a for a in calls if "--get-service-version-info" in a])
        self.supervisor.step()
        self.assertEqual(before, len([a for a in self.qmi_calls() if "--get-service-version-info" in a]))

    def test_definite_preallocation_device_errors_retry_without_latch(self):
        for operation in ("create QmiDevice", "open the QmiDevice"):
            self.fake.fail["--wds-noop"] = cell.Result(1, "",
                f"error: couldn't {operation}: device is not ready\n")
            self.supervisor.step()
            self.assertFalse(self.supervisor.allocation_uncertain)
            self.assertEqual(self.supervisor.last_failure["classification"], "failed_before_allocation")
            self.assertEqual(self.stage(), "modem_readiness_pending")
        self.assertEqual(self.fake.allocated, 0)
        self.assertFalse((self.root / "allocation-uncertain.json").exists())
        del self.fake.fail["--wds-noop"]
        self.start()
        self.assertEqual(self.fake.allocated, 1)

    def test_preallocation_allowlist_does_not_excuse_unknown_client_errors(self):
        open_error = "error: couldn't open the QmiDevice: endpoint failure\n"
        for result in [cell.Result(124, "", open_error), cell.Result(1, "", open_error, True),
                       cell.Result(-15, "", open_error), cell.Result(1, "", "timeout"),
                       cell.Result(1, "", "error: couldn't create client for the 'wds' service: timeout"),
                       cell.Result(1, "", "prefix " + open_error),
                       cell.Result(1, "", open_error + "error: couldn't create client for the 'wds' service: timeout\n"),
                       cell.Result(1, "CID: malformed", open_error), cell.Result(1, "other output", open_error)]:
            with self.subTest(result=result):
                self.assertFalse(cell.Supervisor.failed_before_allocation(result, "1.36.0"))
        self.assertTrue(cell.Supervisor.failed_before_allocation(cell.Result(1, "", open_error), "1.36.0"))
        for version in ("", "1.38.0", "1.36.0-custom"):
            self.assertFalse(cell.Supervisor.failed_before_allocation(cell.Result(1, "", open_error), version))
        self.fake.fail["--wds-noop"] = cell.Result(1, "",
            "error: couldn't create client for the 'wds' service: timeout")
        self.supervisor.step()
        self.assertTrue(self.supervisor.allocation_uncertain)
        self.assertEqual(self.supervisor.last_failure["classification"], "unknown_cid_allocation_uncertain")

    def test_preallocation_allowlist_requires_runtime_version_evidence(self):
        self.fake.fail["--wds-noop"] = cell.Result(1, "", "error: couldn't open the QmiDevice: not ready\n")
        self.fake.fail["--version"] = cell.Result(0, "qmicli 1.38.0\n")
        self.supervisor.step()
        self.assertTrue(self.supervisor.allocation_uncertain)
        self.assertEqual(self.supervisor.cli_version(), "1.38.0")
        self.assertEqual(len([a for a, _ in self.fake.calls if "--version" in a]), 1)

    def test_existing_uncertain_latch_not_cleared_by_working_control(self):
        self.supervisor.atomic_json("allocation-uncertain.json",
                                   {"fingerprint":self.supervisor.modem_fingerprint()})
        self.supervisor.step()
        replacement = self.new_supervisor()
        replacement.step()
        self.assertTrue(replacement.allocation_uncertain)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.stage(), "allocation_uncertain_operator_recovery_required")

    def test_busy_readiness_does_not_allocate_or_create_uncertain_latch(self):
        with mock.patch.object(self.supervisor, "qmi", side_effect=cell.Busy("busy")) as query:
            with self.assertRaises(cell.Busy):
                self.supervisor.step()
        query.assert_called_once_with("--get-service-version-info", retain=False)
        self.assertFalse(self.supervisor.allocation_uncertain)
        self.assertEqual(self.fake.allocated, 0)
        self.assertFalse((self.root / "last-failure.json").exists())
        self.start()

    def test_diagnostics_bounded_private_and_not_in_status_or_logs(self):
        sensitive = "PRIVATE-DIAGNOSTIC-CANARY " + "\U0001f4f7" * 5000
        self.fake.fail["--get-service-version-info"] = cell.Result(1, sensitive, sensitive)
        with mock.patch.object(self.supervisor, "log") as log:
            self.supervisor.step()
        path = self.root / "last-failure.json"
        record = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLess(path.stat().st_size, 30000)
        for field in ("stdout", "stderr"):
            self.assertIn("PRIVATE-DIAGNOSTIC-CANARY", record[field])
            self.assertLessEqual(len(record[field].encode("utf-8")), 4096)
        self.assertEqual((record["phase"], record["code"], record["classification"]),
                         ("readiness", 1, "control_query_failed"))
        self.assertNotIn("PRIVATE-DIAGNOSTIC-CANARY", (self.root / "status.json").read_text())
        self.assertNotIn("PRIVATE-DIAGNOSTIC-CANARY", str(log.call_args_list))
        self.assertEqual(list(self.root.glob("last-failure.json*")), [path])

    def test_probation_promoted_only_after_two_successes(self):
        self.start()
        self.assertEqual(self.fake.metrics, {self.cfg.probation_metric})
        self.supervisor.step()
        self.assertEqual(self.fake.metrics, {self.cfg.probation_metric})
        self.supervisor.step()
        self.assertEqual(self.fake.metrics, {50})

    def test_one_failure_does_not_reconnect(self):
        self.start()
        self.fake.default_probe = cell.Result(28)
        before = len(self.qmi_calls())
        self.supervisor.step()
        self.assertEqual(len(self.qmi_calls()), before)
        self.assertEqual(self.stage(), "suspect")

    def test_repair_before_stop_then_reconnect_on_persistent_failure(self):
        self.start()
        self.supervisor.step()
        self.supervisor.step()
        self.fake.default_probe = cell.Result(28)
        for _ in range(3):
            self.supervisor.step()
        self.assertEqual(self.stage(), "host_configuration_repaired")
        self.assertEqual(self.fake.metrics, {self.cfg.probation_metric})
        self.assertFalse(any("--wds-stop-network=123456789" in a for a in self.qmi_calls()))
        self.supervisor.step()
        self.assertTrue(any("--wds-stop-network=123456789" in a for a in self.qmi_calls()))
        self.assertEqual(self.fake.allocated, 2)

    def test_missing_address_repaired_without_new_session(self):
        self.start()
        self.fake.addresses = []
        self.supervisor.step()
        self.assertEqual(self.stage(), "missing_host_address_repaired")
        self.assertEqual(self.fake.allocated, 1)

    def test_dns_failure_preserves_session_and_fallback(self):
        self.start()
        self.fake.default_probe = cell.Result(6)
        for _ in range(5):
            self.supervisor.step()
        self.assertEqual(self.stage(), "dns_unhealthy_session_preserved")
        self.assertEqual(self.fake.allocated, 1)
        self.assertEqual(self.fake.metrics, {self.cfg.probation_metric})

    def test_api_outage_with_secondary_success_does_not_reset(self):
        self.start()
        self.fake.probes = [cell.Result(28), cell.Result(0, "200")]
        self.supervisor.step()
        self.assertEqual(self.stage(), "connected")
        self.assertFalse(json.loads((self.root / "status.json").read_text())["primary_https_transport"])
        self.assertEqual(self.fake.allocated, 1)

    def test_probe_bound_to_interface_no_proxy_no_insecure_tls(self):
        self.supervisor.probe()
        args, timeout = self.fake.calls[-1]
        self.assertIn("if!wwan0", args)
        self.assertIn("--noproxy", args)
        self.assertIn("-4", args)
        self.assertNotIn("--insecure", args)
        self.assertEqual(timeout, 10)

    def test_restart_restores_exact_owned_session(self):
        self.start()
        replacement = self.new_supervisor()
        replacement.step()
        self.assertEqual(replacement.state["cid"], 17)
        self.assertEqual(self.fake.allocated, 1)
        self.assertEqual((self.root / "session.json").stat().st_mode & 0o777, 0o600)

    def test_healthy_restored_control_error_does_not_withdraw_good_route(self):
        self.start()
        self.fake.metrics = {50}
        self.fake.fail["--wds-get-current-settings"] = cell.Result(1)
        replacement = self.new_supervisor()
        replacement.step()
        self.assertEqual(self.stage(), "connected_ownership_unverified")
        self.assertEqual(self.fake.metrics, {50})

    def test_healthy_restored_busy_run_preserves_route(self):
        self.start()
        self.fake.metrics = {50}
        replacement = self.new_supervisor()
        with mock.patch.object(replacement, "get_settings", side_effect=cell.Busy("busy")), \
                mock.patch.object(cell.time, "sleep", side_effect=lambda _: setattr(replacement, "stopping", True)):
            replacement.run()
        self.assertEqual(self.fake.metrics, {50})

    def test_busy_host_repair_retries_repair_not_stop(self):
        self.start()
        self.fake.default_probe = cell.Result(28)
        self.supervisor.failed = 2
        with mock.patch.object(self.supervisor, "get_settings", side_effect=cell.Busy("busy")):
            with self.assertRaises(cell.Busy):
                self.supervisor.step()
        self.assertFalse(self.supervisor.repaired)
        self.supervisor.step()
        self.assertEqual(self.stage(), "host_configuration_repaired")
        self.assertFalse(any("--wds-stop-network=123456789" in a for a in self.qmi_calls()))

    def test_changed_fingerprint_discards_stale_client_without_stopping_it(self):
        self.start()
        replacement = self.new_supervisor()
        replacement.modem_fingerprint = lambda: {"boot_id":"two", "interface_index":"5"}
        replacement.step()
        self.assertIsNone(replacement.state)
        self.assertFalse(any("--wds-stop-network=123456789" in a for a in self.qmi_calls()))
        self.assertEqual(self.stage(), "connected_legacy_unowned")

    def test_missing_modem_does_not_loop_in_qmi(self):
        self.supervisor.modem_fingerprint = lambda: None
        self.supervisor.step()
        self.assertEqual(self.stage(), "modem_absent")
        self.assertEqual(self.fake.calls, [])

    def test_corrupt_state_is_not_executed(self):
        path = self.root / "session.json"
        path.write_text('CID="17"; touch /tmp/should-never-exist')
        path.chmod(0o600)
        self.supervisor.step()
        self.assertEqual(self.fake.allocated, 1)

    def test_failed_cleanup_blocks_all_new_allocations(self):
        self.start()
        self.fake.fail["--wds-stop-network=123456789"] = cell.Result(124)
        self.assertFalse(self.supervisor.cleanup())
        self.assertEqual(self.supervisor.state["pdh"], 123456789)
        self.assertEqual(self.fake.metrics, set())
        self.assertEqual(self.fake.allocated, 1)

    def test_disconnected_owned_session_can_release_after_stop_noeffect(self):
        self.start()
        self.fake.fail["--wds-stop-network=123456789"] = cell.Result(1, "", "NoEffect")
        self.fake.fail["--wds-get-packet-service-status"] = cell.Result(0, "Connection status: 'disconnected'")
        self.assertTrue(self.supervisor.cleanup())
        self.assertIsNone(self.supervisor.state)

    def test_invalid_cid_after_modem_internal_reset_is_not_retried_forever(self):
        self.start()
        self.fake.fail["--wds-stop-network=123456789"] = cell.Result(1, "", "InvalidClientId")
        self.assertTrue(self.supervisor.cleanup())
        self.assertIsNone(self.supervisor.state)

    def test_failed_ip_config_cannot_be_reported_connected(self):
        self.fake.fail["replace"] = cell.Result(2)
        self.supervisor.step()
        self.assertNotIn(self.stage(), {"connected", "probation"})
        self.assertEqual(self.fake.metrics, set())

    def test_resolved_is_explicit_and_failure_not_ignored(self):
        self.cfg.dns_mode = "resolved"
        self.fake.fail["systemd-resolved.service"] = cell.Result(3)
        self.supervisor.step()
        self.assertEqual(self.stage(), "host_configuration_failed_session_preserved")
        self.assertEqual(self.fake.metrics, set())
        self.assertEqual(self.fake.allocated, 1)
        with self.assertRaises(cell.RecoveryError):
            self.supervisor.step()
        self.assertEqual(self.fake.metrics, {self.cfg.probation_metric})
        self.assertFalse(self.supervisor.configuration_ready)

    def test_preserve_does_not_write_resolver_or_call_resolvectl(self):
        self.start()
        self.assertFalse(any(a[0].endswith("resolvectl") for a, _ in self.fake.calls))
        self.assertIn("externally_managed", self.supervisor.dns_warning)

    def test_no_routes_outside_owned_interface_or_main_table(self):
        self.start()
        self.supervisor.cleanup()
        for args, _ in self.fake.calls:
            if "route" in args:
                self.assertIn("wwan0", args)
                self.assertNotIn("wlan0", args)
                self.assertNotIn("tailscale0", args)
                self.assertNotIn("52", args)

    def test_single_owner_lock(self):
        lock = str(self.root / "owner.lock")
        with cell.Lock(lock):
            with self.assertRaises(cell.Busy):
                with cell.Lock(lock):
                    self.fail("second owner acquired lock")

    def test_qmi_calls_bounded_and_telemetry_lock_honored(self):
        with cell.Lock(self.cfg.qmi_lock):
            with self.assertRaises(cell.Busy):
                self.supervisor.qmi("--wds-noop")
        self.assertEqual(self.fake.calls, [])
        self.start()
        self.assertTrue(all(timeout == (5 if "--version" in args else 25)
                            for args, timeout in self.fake.calls if args[0].endswith("qmicli")))

    def test_real_hung_child_is_terminated(self):
        start = time.monotonic()
        result = cell.Runner().run([sys.executable, "-c",
            "import sys,time; print('partial output',flush=True); print('original error',file=sys.stderr,flush=True); time.sleep(30)"], timeout=0.2)
        self.assertEqual(result.code, 124)
        self.assertTrue(result.timed_out)
        self.assertIn("partial output", result.out)
        self.assertIn("original error", result.err)
        self.assertLess(time.monotonic() - start, 5)

    def test_wifi_only_sentinel_prevents_new_connections(self):
        (self.root / ".wifi_only").touch()
        self.supervisor.step()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.stage(), "disabled_wifi_only")

    def test_wrong_device_association_cannot_withdraw_healthy_route(self):
        self.fake.metrics = {50}
        self.supervisor.modem_fingerprint = mock.Mock(side_effect=cell.UnsafeDevice("mismatch"))
        with mock.patch.object(cell.time, "sleep", side_effect=lambda _: setattr(self.supervisor, "stopping", True)):
            self.supervisor.run()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fake.metrics, {50})

    def test_network_priority_cannot_promote_cellular(self):
        text = (ROOT / "scripts/network-priority.sh").read_text()
        self.assertNotIn('ip route replace default via "$CURRENT_GW"', text)

    def test_config_rejects_shell_tokens_and_non_tls(self):
        for field, value in [("iface", "wwan0;reboot"), ("iface", "eth0"), ("iface", "wlan0"),
                             ("dev", "/dev/sda"), ("apn", "super,username=x"),
                             ("check_url", "http://example.com"), ("check_url", "https://user:pass@example.com"),
                             ("dns_mode", "rewrite")]:
            cfg = cell.Config()
            setattr(cfg, field, value)
            with self.subTest(field=field), self.assertRaises(cell.RecoveryError):
                cfg.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_monkey_gate", ROOT / "scripts/cloud_monkey_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class MonkeyGateTest(unittest.TestCase):
    def test_adb_identity_is_ready_before_secure_emulator_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path(directory))
            trace = []
            probe.command = lambda args, **kwargs: trace.append(str(args[1])) or ''
            process = Mock()
            process.poll.return_value = None
            probe.background = lambda args, log: trace.append('emulator') or process
            probe.device = lambda *args, **kwargs: '1'
            probe.start()
            self.assertLess(trace.index('start-server'), trace.index('emulator'))
            self.assertNotIn('ANDROID_EMULATOR_HOME', {k: v for k, v in probe.env.items() if k not in gate.os.environ})

    def test_actual_monkey_invocation_targets_only_emulator_and_stops_on_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'logcat.log').write_text('')
            probe = gate.CloudGate(Path('/sdk'), directory / 'app.apk', directory)
            calls = []
            def device(*args, **kwargs):
                calls.append(args)
                if args[:2] == ('shell', 'getprop'):
                    return {'ro.debuggable': '0', 'ro.build.tags': 'release-keys',
                            'ro.product.cpu.abilist': 'x86_64,arm64-v8a'}[args[2]]
                return 'Status: ok'
            probe.device = device
            probe.pid = lambda: '12'
            process = Mock()
            process.poll.return_value = None
            probe.background = lambda *args: process
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                kwargs['stdout'].write(b'Events injected: 1000\n// Monkey finished')
                return SimpleNamespace(returncode=0)
            with patch.object(gate.time, 'sleep'), patch.object(gate.subprocess, 'run', side_effect=run):
                result = probe.test()
            self.assertEqual('passed', result['status'])
            self.assertEqual(['/sdk/platform-tools/adb', '-s', 'emulator-5554'], commands[0][:3])
            self.assertFalse(any(arg.startswith('--ignore-') for arg in commands[0]))
            self.assertNotIn('root', commands[0])
            self.assertNotIn('setprop', str(calls))
            self.assertIn('--monitor-native-crashes', commands[0])
            self.assertEqual('0', commands[0][commands[0].index('--pct-syskeys') + 1])
            self.assertEqual('1000', commands[0][-1])

    def test_debug_image_is_rejected_before_install_or_monkey(self):
        probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path('/private'))
        probe.device = Mock(return_value='1')
        with self.assertRaises(gate.GateFailure):
            probe.test()
        self.assertEqual(1, probe.device.call_count)

    def test_complete_stable_clean_run_is_required(self):
        output = "Events injected: 1000\n// Monkey finished"
        self.assertEqual("passed", gate.evaluate(output, "", 0, 1000, "12", "12")["status"])
        for text, code, pid in (("Events injected: 999\n// Monkey finished", 0, "12"),
                                ("Events injected: 1000", 0, "12"), (output, 1, "12"),
                                (output, 0, "13"), (output, 0, "")):
            with self.subTest(text=text, code=code, pid=pid):
                self.assertEqual("failed", gate.evaluate(text, "", code, 1000, "12", pid)["status"])

    def test_crashes_anrs_native_tombstones_and_early_death_fail_even_on_zero_exit(self):
        output = "Events injected: 1000\n// Monkey finished"
        for text in ("// CRASH: info.nightscout.androidaps (pid 12)",
                     "// NOT RESPONDING: info.nightscout.androidaps", "ANR in info.nightscout.androidaps",
                     "am_crash: [0,12,info.nightscout.androidaps,1,secret]",
                     "am_anr: [0,12,info.nightscout.androidaps,1,secret]",
                     "am_proc_died: [0,12,info.nightscout.androidaps,0,0]",
                     ">>> info.nightscout.androidaps <<<",
                     ">>> info.nightscout.androidaps:worker <<<",
                     "Process: info.nightscout.androidaps, PID: 12"):
            with self.subTest(text=text):
                self.assertEqual("failed", gate.evaluate(output, text, 0, 1000, "12", "12")["status"])
        self.assertEqual("passed", gate.evaluate(output, "am_crash: [0,13,com.other.app,1]", 0, 1000, "12", "12")["status"])
        self.assertEqual("passed", gate.evaluate(output, "am_crash: [0,13,info.nightscout.androidaps.other,1]", 0, 1000, "12", "12")["status"])

    def test_ignored_or_aborted_monkey_is_not_admitted(self):
        output = "Events injected: 1000\n// Monkey finished"
        for failure in ("Monkey aborted", "System appears to have crashed", "native crash", "SecurityException"):
            self.assertEqual("failed", gate.evaluate(output + failure, "", 0, 1000, "12", "12")["status"])

    def test_upload_requires_matching_apk_bytes_and_complete_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            apk, result = Path(directory) / "app.apk", Path(directory) / "result.json"
            apk.write_bytes(b"protected fixture")
            record = gate.evaluate("Events injected: 1000\n// Monkey finished", "", 0, 1000, "12", "12")
            record.update(apk_sha256=gate.sha256(apk), coverage="unauthenticated_startup_ui")
            result.write_text(json.dumps(record))
            gate.verify_upload(apk, result)
            for patch in ({"status": "failed"}, {"events_injected": 0}, {"process_stable": False},
                          {"monkey_exit_code": 1}, {"crash_or_anr_detected": True}, {"monkey_aborted": True},
                          {"coverage": "untested"}, {"apk_sha256": "wrong"}, {"process_stable": "false"}):
                with self.subTest(patch=patch), self.assertRaises(gate.GateFailure):
                    result.write_text(json.dumps({**record, **patch}))
                    gate.verify_upload(apk, result)
            result.write_text(json.dumps(record))
            apk.write_bytes(b"replacement fixture")
            with self.assertRaises(gate.GateFailure):
                gate.verify_upload(apk, result)

    def test_raw_diagnostics_are_not_in_result(self):
        secret = "private-token-and-source-fragment"
        result = gate.evaluate("Events injected: 1000\n// Monkey finished " + secret,
                               "Process: info.nightscout.androidaps, PID: 12 " + secret, 0, 1000, "12", "12")
        self.assertNotIn(secret, json.dumps(result))

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


def complete(dropped=0):
    return (':Switch: main\n' + ':Sending Touch (ACTION_UP): 0:(50,50)\n' * 1199
            + '    // Injection Failed\n' * dropped
            + ':Dropped: keys=0 pointers=%d trackballs=0 flips=0 rotations=0\n' % dropped
            + 'Events injected: 1200\n// Monkey finished')


class MonkeyGateTest(unittest.TestCase):
    def test_adb_identity_is_ready_before_secure_emulator_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path(directory))
            trace = []
            probe.command = lambda args, **kwargs: trace.append(str(args[1])) or ''
            process = Mock()
            process.poll.return_value = None
            def start_emulator(args, log):
                self.assertEqual('4096', args[args.index('-memory') + 1])
                trace.append('emulator')
                return process
            probe.background = start_emulator
            probe.device = lambda *args, **kwargs: 'package:/system/framework/framework-res.apk' if args[:3] == ('shell', 'cmd', 'package') else '1'
            with patch.object(gate.time, 'sleep'):
                probe.start()
            self.assertLess(trace.index('start-server'), trace.index('emulator'))
            self.assertNotIn('ANDROID_EMULATOR_HOME', {k: v for k, v in probe.env.items() if k not in gate.os.environ})

    def test_boot_waits_through_adb_reconnect_and_package_service_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path(directory))
            probe.command = Mock(return_value='')
            process = Mock(); process.poll.return_value = None
            probe.background = Mock(return_value=process)
            probe.device = Mock(side_effect=[gate.GateFailure('offline'), '1', 'package:framework',
                                            gate.GateFailure('offline'), '1', 'package:framework',
                                            '1', 'package:framework', '1', 'package:framework'])
            with patch.object(gate.time, 'sleep'):
                probe.start()
            self.assertEqual(10, probe.device.call_count)

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
                if args[:4] == ('shell', 'cmd', 'package', 'resolve-activity'):
                    return 'priority=0 preferredOrder=0\ninfo.nightscout.androidaps/fixture.obfuscated.RenamedLauncher\n'
                if args[:4] == ('shell', 'dumpsys', 'window', 'displays'):
                    return 'mCurrentFocus=Window{abc u0 info.nightscout.androidaps/fixture.RenamedLauncher}'
                if args[:3] == ('shell', 'dumpsys', 'gfxinfo'):
                    return 'Graphics info for pid 12 [info.nightscout.androidaps]\nTotal frames rendered: 1'
                return 'Status: timeout'
            probe.device = device
            probe.pid = lambda: '12'
            process = Mock()
            process.poll.return_value = None
            probe.background = lambda *args: process
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                kwargs['stdout'].write(complete().encode())
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
            self.assertEqual('1200', commands[0][-1])
            launch = next(args for args in calls if args[:4] == ('shell', 'am', 'start', '-W'))
            self.assertEqual('info.nightscout.androidaps/fixture.obfuscated.RenamedLauncher', launch[-1])
            self.assertEqual('android.intent.action.MAIN', launch[launch.index('-a') + 1])
            self.assertEqual('android.intent.category.LAUNCHER', launch[launch.index('-c') + 1])
            self.assertEqual('0x10200000', launch[launch.index('-f') + 1])

    def test_startup_needs_real_app_window_and_frames_in_same_process(self):
        window = 'mCurrentFocus=Window{abc u0 info.nightscout.androidaps/fixture.RenamedLauncher}'
        frames = 'Graphics info for pid 12 [info.nightscout.androidaps]\nTotal frames rendered: 1'
        self.assertTrue(gate.ui_ready(window, frames, '12'))
        for w, f, pid in ((window, frames, '13'),
                          (window, frames.replace('rendered: 1', 'rendered: 0'), '12'),
                          (window.replace('info.nightscout.androidaps/', 'com.other.app/'), frames, '12'),
                          ('mCurrentFocus=Window{abc u0 Splash Screen info.nightscout.androidaps}', frames, '12')):
            self.assertFalse(gate.ui_ready(w, f, pid))

    def test_hung_or_restarted_startup_is_rejected(self):
        probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path('/private'))
        probe.pid = Mock(return_value='12')
        probe.device = Mock(return_value='no app frames')
        with patch.object(gate.time, 'monotonic', side_effect=[0, 0, 121]), patch.object(gate.time, 'sleep'):
            with self.assertRaisesRegex(gate.GateFailure, 'startup_ui_not_ready'):
                probe.wait_for_ui('12')
        probe.pid = Mock(return_value='13')
        with self.assertRaisesRegex(gate.GateFailure, 'app_exited_or_restarted_during_startup'):
            probe.wait_for_ui('12')

    def test_debug_image_is_rejected_before_install_or_monkey(self):
        probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path('/private'))
        probe.device = Mock(return_value='1')
        with self.assertRaises(gate.GateFailure):
            probe.test()
        self.assertEqual(1, probe.device.call_count)

    def test_complete_stable_clean_run_is_required(self):
        output = complete()
        self.assertEqual("passed", gate.evaluate(output, "", 0, 1200, "12", "12")["status"])
        for text, code, pid in ((output.replace("injected: 1200", "injected: 1199"), 0, "12"),
                                (output.replace("// Monkey finished", ""), 0, "12"), (output, 1, "12"),
                                (output, 0, "13"), (output, 0, "")):
            with self.subTest(text=text, code=code, pid=pid):
                self.assertEqual("failed", gate.evaluate(text, "", code, 1200, "12", pid)["status"])

    def test_crashes_anrs_native_tombstones_and_early_death_fail_even_on_zero_exit(self):
        output = complete()
        for text in ("// CRASH: info.nightscout.androidaps (pid 12)",
                     "// NOT RESPONDING: info.nightscout.androidaps", "ANR in info.nightscout.androidaps",
                     "am_crash: [0,12,info.nightscout.androidaps,1,secret]",
                     "am_anr: [0,12,info.nightscout.androidaps,1,secret]",
                     "am_proc_died: [0,12,info.nightscout.androidaps,0,0]",
                     ">>> info.nightscout.androidaps <<<",
                     ">>> info.nightscout.androidaps:worker <<<",
                     "Process: info.nightscout.androidaps, PID: 12"):
            with self.subTest(text=text):
                self.assertEqual("failed", gate.evaluate(output, text, 0, 1200, "12", "12")["status"])
        self.assertEqual("passed", gate.evaluate(output, "am_crash: [0,13,com.other.app,1]", 0, 1200, "12", "12")["status"])
        self.assertEqual("passed", gate.evaluate(output, "am_crash: [0,13,info.nightscout.androidaps.other,1]", 0, 1200, "12", "12")["status"])

    def test_reported_count_needs_at_least_1000_successful_touch_injections(self):
        for dropped in (0, 6, 199):
            result = gate.evaluate(complete(dropped), '', 0, 1200, '12', '12')
            self.assertEqual('passed', result['status'])
            self.assertEqual(1199 - dropped, result['touch_events_delivered'])
        for output in (complete(200), complete(6).replace('pointers=6', 'pointers=0'),
                       complete().replace(':Sending Touch', ':Uncounted'),
                       complete().replace('keys=0', 'keys=1')):
            self.assertEqual('failed', gate.evaluate(output, '', 0, 1200, '12', '12')['status'])

    def test_ignored_or_aborted_monkey_is_not_admitted(self):
        output = complete()
        for failure in ("Monkey aborted", "System appears to have crashed", "native crash", "SecurityException"):
            self.assertEqual("failed", gate.evaluate(output + failure, "", 0, 1200, "12", "12")["status"])

    def test_upload_requires_matching_apk_bytes_and_complete_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            apk, result = Path(directory) / "app.apk", Path(directory) / "result.json"
            apk.write_bytes(b"protected fixture")
            record = gate.evaluate(complete(), "", 0, 1200, "12", "12")
            record.update(apk_sha256=gate.sha256(apk), coverage="unauthenticated_startup_ui", startup_ui_ready=True)
            result.write_text(json.dumps(record))
            gate.verify_upload(apk, result)
            for patch in ({"status": "failed"}, {"events_injected": 0}, {"process_stable": False},
                          {"monkey_exit_code": 1}, {"crash_or_anr_detected": True}, {"monkey_aborted": True},
                          {"startup_ui_ready": False}, {"input_injection_failed": True}, {"touch_events_delivered": 999}, {"input_delivery_verified": False}, {"coverage": "untested"}, {"apk_sha256": "wrong"}, {"process_stable": "false"}):
                with self.subTest(patch=patch), self.assertRaises(gate.GateFailure):
                    result.write_text(json.dumps({**record, **patch}))
                    gate.verify_upload(apk, result)
            result.write_text(json.dumps(record))
            apk.write_bytes(b"replacement fixture")
            with self.assertRaises(gate.GateFailure):
                gate.verify_upload(apk, result)

    def test_raw_diagnostics_are_not_in_result(self):
        secret = "private-token-and-source-fragment"
        result = gate.evaluate(complete() + " " + secret,
                               "Process: info.nightscout.androidaps, PID: 12 " + secret, 0, 1200, "12", "12")
        self.assertNotIn(secret, json.dumps(result))

    def test_idle_resume_waits_full_ttl_and_never_restarts_a_dead_process(self):
        probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path('/private'), idle_resume_seconds=330)
        probe.device = Mock(return_value='Status: ok')
        probe.pid = Mock(return_value='12')
        probe.wait_for_ui = Mock(return_value=0)
        with patch.object(gate.time, 'monotonic', side_effect=[0, 0, 100, 330, 330]), patch.object(gate.time, 'sleep'):
            self.assertEqual(330, probe.idle_and_resume(gate.PACKAGE + '/Main', '12'))
        self.assertEqual(('shell', 'input', 'keyevent', 'KEYCODE_HOME'), probe.device.call_args_list[0].args)
        probe.pid = Mock(return_value='13')
        probe.device.reset_mock()
        with patch.object(gate.time, 'monotonic', side_effect=[0, 0]), patch.object(gate.time, 'sleep'):
            with self.assertRaisesRegex(gate.GateFailure, 'during_idle'):
                probe.idle_and_resume(gate.PACKAGE + '/Main', '12')
        self.assertEqual(1, probe.device.call_count)  # No launch/restart after death.

    def test_resume_hang_or_process_change_is_rejected(self):
        probe = gate.CloudGate(Path('/sdk'), Path('/app.apk'), Path('/private'), idle_resume_seconds=330)
        probe.device = Mock(return_value='Status: ok')
        probe.wait_for_ui = Mock(side_effect=gate.GateFailure('startup_ui_not_ready'))
        with patch.object(gate.time, 'monotonic', side_effect=[0, 330, 330]):
            with self.assertRaisesRegex(gate.GateFailure, 'startup_ui_not_ready'):
                probe.idle_and_resume(gate.PACKAGE + '/Main', '12')
        probe.wait_for_ui = Mock(return_value=0)
        probe.pid = Mock(return_value='13')
        with patch.object(gate.time, 'monotonic', side_effect=[0, 330, 330]), patch.object(gate.time, 'sleep'):
            with self.assertRaisesRegex(gate.GateFailure, 'after_resume'):
                probe.idle_and_resume(gate.PACKAGE + '/Main', '12')

    def test_production_upload_requires_same_apk_and_verified_full_idle_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            apk, result = Path(directory) / 'app.apk', Path(directory) / 'result.json'
            apk.write_bytes(b'protected fixture')
            record = gate.evaluate(complete(), '', 0, 1200, '12', '12')
            record.update(apk_sha256=gate.sha256(apk), coverage='unauthenticated_startup_ui', startup_ui_ready=True,
                          idle_resume_seconds=330, idle_observed_seconds=330.2, idle_resume_passed=True)
            result.write_text(json.dumps(record))
            gate.verify_upload(apk, result, 330)
            for changes in ({'idle_resume_passed': False}, {'idle_resume_seconds': 0},
                            {'idle_observed_seconds': 329.9}, {'idle_observed_seconds': '330'},
                            {'idle_observed_seconds': float('nan')}, {'crash_or_anr_detected': True}):
                with self.subTest(changes=changes), self.assertRaises(gate.GateFailure):
                    result.write_text(json.dumps({**record, **changes}))
                    gate.verify_upload(apk, result, 330)

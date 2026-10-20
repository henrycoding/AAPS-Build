#!/usr/bin/env python3
"""Test the exact protected APK on a fresh, isolated Google Play emulator.

Raw emulator/Monkey/logcat output stays in RUNNER_TEMP, never in public artifacts.
This is an unauthenticated startup/UI smoke test, not pump or logged-in coverage.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

PACKAGE = "info.nightscout.androidaps"
SERIAL = "emulator-5554"
IMAGE = "system-images;android-30;google_apis_playstore;x86_64"
REQUESTED_EVENTS = 1200
MIN_DELIVERED_TOUCH_EVENTS = 1000


class GateFailure(Exception):
    pass


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evaluate(monkey, logcat, returncode, requested, before_pid, after_pid):
    """Fail even when Monkey exits zero after a partial run or process restart."""
    counts = re.findall(r"Events injected:\s*(\d+)", monkey)
    injected = int(counts[-1]) if counts else 0
    app = re.escape(PACKAGE) + r"(?=[:\s,\]<]|$)"
    crash = bool(re.search(r"(?im)(?:CRASH:|NOT RESPONDING:|ANR in)\s*" + app, monkey + "\n" + logcat))
    crash |= any(re.search(r"\b(?:am_crash|am_anr|am_proc_died)\b", line) and re.search(app, line)
                 for line in logcat.splitlines())
    crash |= bool(re.search(r">>>\s*" + re.escape(PACKAGE) + r"(?::[^\s]+)?\s*<<<", logcat))
    crash |= bool(re.search(r"(?im)\bProcess:\s*" + app, logcat))
    aborted = bool(re.search(r"(?i)monkey aborted|system appears to have crashed|native crash|securityexception", monkey))
    stable = bool(before_pid and before_pid == after_pid)
    # Android may drop a gesture when SystemUI takes over an edge swipe.
    # Count actual successful touch injections, not the misleading "injected"
    # total (which also includes the initial Activity switch and failed inputs).
    touch_attempts = len(re.findall(r"(?m)^:Sending Touch ", monkey))
    switches = len(re.findall(r"(?m)^:Switch:", monkey))
    failures = len(re.findall(r"(?m)^\s*// Injection Failed", monkey))
    summaries = re.findall(r"(?m)^:Dropped: keys=(\d+) pointers=(\d+) trackballs=(\d+) flips=(\d+) rotations=(\d+)", monkey)
    drops = tuple(map(int, summaries[-1])) if summaries else None
    dropped = drops[1] if drops else 0
    delivered = max(0, touch_attempts - dropped)
    delivery_verified = bool(drops and drops[0] == drops[2] == drops[3] == drops[4] == 0
                             and dropped == failures and switches == 1
                             and touch_attempts + switches == injected
                             and delivered >= MIN_DELIVERED_TOUCH_EVENTS)
    passed = (returncode == 0 and injected == requested and "Monkey finished" in monkey
              and stable and not crash and not aborted and delivery_verified)
    return {"status": "passed" if passed else "failed", "events_requested": requested, "events_injected": injected,
            "process_stable": stable, "crash_or_anr_detected": crash, "monkey_aborted": aborted,
            "monkey_exit_code": returncode, "input_injection_failed": failures > 0,
            "touch_events_attempted": touch_attempts, "touch_events_dropped": dropped,
            "touch_events_delivered": delivered, "input_delivery_verified": delivery_verified}



def ui_ready(windows, graphics, pid):
    # A system splash/starting window is not an app-rendered view. Require an
    # actual focused app window and frames from this very process.
    focused = re.search(r"mCurrentFocus=Window\{[^\n}]*\bu\d+ " + re.escape(PACKAGE) + r"/[\w.$]+\}", windows)
    own_process = re.search(r"Graphics info for pid " + re.escape(pid) + r" \[" + re.escape(PACKAGE) + r"\]", graphics)
    frames = re.search(r"Total frames rendered:\s*(\d+)", graphics)
    return bool(focused and own_process and frames and int(frames[1]) > 0)


class CloudGate:
    def __init__(self, sdk, apk, directory, count=REQUESTED_EVENTS, throttle=300, idle_resume_seconds=0):
        self.sdk, self.apk, self.directory = sdk, apk, directory
        self.count, self.throttle = count, throttle
        self.idle_resume_seconds = idle_resume_seconds
        self.adb = sdk / "platform-tools/adb"
        self.emulator = None
        self.collector = None
        self.handles = []
        # Keep emulator and adb on the same default Android user/key directory.
        # Isolating EMULATOR_HOME alone can separate Play-image ADB auth keys.
        self.env = dict(os.environ, ANDROID_AVD_HOME=str(directory / "avds"))

    def command(self, args, timeout=30, log=None, data=None):
        try:
            result = subprocess.run([str(arg) for arg in args], input=data, capture_output=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired as error:
            raise GateFailure("command_timeout") from error
        if log:
            (self.directory / log).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            if not log:
                (self.directory / "last-command-failure.log").write_bytes(result.stdout + result.stderr)
            if b"device offline" in result.stderr or b"device not found" in result.stderr:
                raise GateFailure("emulator_adb_offline")
            raise GateFailure("command_failed")
        return result.stdout.decode("utf-8", errors="replace").replace("\r", "")

    def device(self, *args, **kwargs):
        # Never select the user's USB device, including if emulator boot failed.
        return self.command([self.adb, "-s", SERIAL, *args], **kwargs)

    def pid(self):
        try:
            value = self.device("shell", "pidof", PACKAGE).strip()
            return value if re.fullmatch(r"[0-9]+", value) else ""
        except GateFailure:
            return ""

    def background(self, args, name):
        handle = (self.directory / name).open("wb")
        self.handles.append(handle)
        return subprocess.Popen([str(arg) for arg in args], stdout=handle, stderr=subprocess.STDOUT, env=self.env)

    def idle_and_resume(self, component, expected_pid):
        # HOME backgrounds only this isolated emulator. Keep logcat collecting;
        # a process death or restart at any point is a failure, never a retry.
        self.device("shell", "input", "keyevent", "KEYCODE_HOME")
        started = time.monotonic()
        while time.monotonic() - started < self.idle_resume_seconds:
            time.sleep(5)
            if self.pid() != expected_pid:
                raise GateFailure("app_exited_or_restarted_during_idle")
        elapsed = time.monotonic() - started
        self.device("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self.device("shell", "input", "keyevent", "82")
        # The startup frame counter must not make a frozen resume look ready.
        self.device("shell", "dumpsys", "gfxinfo", PACKAGE, "reset", log="resume-reset-graphics.log")
        output = self.device("shell", "am", "start", "-W", "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", "-f", "0x10200000",
                             "-n", component, timeout=120, log="resume.log")
        if not re.search(r"(?m)^Status: (?:ok|timeout)$", output):
            raise GateFailure("app_resume_failed")
        self.wait_for_ui(expected_pid)
        time.sleep(20)
        if self.pid() != expected_pid:
            raise GateFailure("app_exited_or_restarted_after_resume")
        return elapsed

    def start(self):
        (self.directory / "avds").mkdir()
        # Generate/load host ADB identity before the secure Play image boots.
        self.command([self.adb, "start-server"], log="adb-server.log")
        self.command([self.sdk / "cmdline-tools/latest/bin/avdmanager", "create", "avd", "--force", "--name", "aaps-monkey",
                      "--package", IMAGE, "--device", "pixel_2"], log="avd.log", data=b"no\n", timeout=90)
        self.emulator = self.background([self.sdk / "emulator/emulator", "-avd", "aaps-monkey", "-port", "5554",
            "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot", "-gpu", "swiftshader_indirect",
            "-feature", "-Vulkan", "-memory", "2048", "-cores", "2", "-accel", "on", "-camera-back", "none", "-camera-front", "none"], "emulator.log")
        deadline = time.monotonic() + 300
        ready_checks = 0
        while time.monotonic() < deadline:
            if self.emulator.poll() is not None:
                raise GateFailure("emulator_stopped_during_boot")
            try:
                booted = self.device("shell", "getprop", "sys.boot_completed", timeout=10, log="boot-adb.log").strip() == "1"
                package = self.device("shell", "cmd", "package", "path", "android", timeout=10, log="boot-package.log") if booted else ""
                ready_checks = ready_checks + 1 if booted and package.startswith("package:") else 0
                # The Play image briefly reconnects ADB after setting boot_completed.
                # Require consecutive successful probes and a live package service
                # before any install or app operation; no retries hide app failures.
                if ready_checks >= 3:
                    return
            except GateFailure:
                ready_checks = 0
            time.sleep(3)
        raise GateFailure("emulator_boot_timeout")

    def wait_for_ui(self, expected_pid):
        started = time.monotonic()
        deadline = started + 120
        while time.monotonic() < deadline:
            if self.pid() != expected_pid:
                raise GateFailure("app_exited_or_restarted_during_startup")
            try:
                # On Android 11 the focus belongs to DisplayContent; the
                # "windows" subcommand omits mCurrentFocus.
                windows = self.device("shell", "dumpsys", "window", "displays", timeout=10, log="startup-window.log")
                graphics = self.device("shell", "dumpsys", "gfxinfo", PACKAGE, timeout=10, log="startup-graphics.log")
                if ui_ready(windows, graphics, expected_pid):
                    return round(time.monotonic() - started, 1)
            except GateFailure:
                pass
            time.sleep(3)
        raise GateFailure("startup_ui_not_ready")

    def test(self):
        # Inspect genuine image properties; never set properties or relax app protection.
        if self.device("shell", "getprop", "ro.debuggable").strip() != "0":
            raise GateFailure("emulator_is_debuggable")
        if "release-keys" not in self.device("shell", "getprop", "ro.build.tags"):
            raise GateFailure("emulator_is_not_release_signed")
        if "arm64-v8a" not in self.device("shell", "getprop", "ro.product.cpu.abilist"):
            raise GateFailure("emulator_has_no_arm64_translation")
        self.device("shell", "input", "keyevent", "82")
        self.device("install", "-g", str(self.apk), timeout=120, log="install.log")
        # Only this newly created virtual device's buffers are cleared.
        self.device("logcat", "-c")
        self.collector = self.background([self.adb, "-s", SERIAL, "logcat", "-b", "main,system,events,crash", "-v", "threadtime"], "logcat.log")
        # Release R8 may rename launcher classes. Resolve the installed manifest
        # instead of assuming the source/debug Activity name still exists.
        resolved = self.device("shell", "cmd", "package", "resolve-activity", "--brief",
                               "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                               PACKAGE, log="resolve-launcher.log").strip().splitlines()
        component = resolved[-1] if resolved else ""
        if not re.fullmatch(re.escape(PACKAGE) + r"/[A-Za-z0-9_.$]+", component):
            raise GateFailure("launcher_not_resolved")
        # Match Monkey's initial MAIN/LAUNCHER intent and task flags. Starting
        # only by component creates a different base intent; Monkey would then
        # create another Activity and inject touches during its second startup.
        output = self.device("shell", "am", "start", "-W", "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", "-f", "0x10200000",
                             "-n", component, timeout=120, log="launch.log")
        # am -W has its own short Android launch wait. A timeout is pending,
        # never a pass: independently require a drawn app UI within 120 seconds.
        if not re.search(r"(?m)^Status: (?:ok|timeout)$", output):
            raise GateFailure("app_launch_failed")
        before = self.pid()
        if not before:
            raise GateFailure("app_exited_after_launch")
        startup_wait = self.wait_for_ui(before)
        time.sleep(10)
        if self.pid() != before:
            raise GateFailure("app_exited_or_restarted_during_startup")
        # No keys, permission events, app switching, or arbitrary text input.
        args = [self.adb, "-s", SERIAL, "shell", "monkey", "-p", PACKAGE, "--throttle", str(self.throttle),
                "--monitor-native-crashes", "--pct-touch", "75", "--pct-motion", "15", "--pct-pinchzoom", "10", "--pct-trackball", "0"]
        for kind in ("syskeys", "nav", "majornav", "appswitch", "flip", "anyevent", "permission"):
            args += ["--pct-" + kind, "0"]
        args += ["-s", "42", "-v", "-v", str(self.count)]
        try:
            with (self.directory / "monkey.log").open("wb") as stream:
                result = subprocess.run([str(arg) for arg in args], stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=self.count * self.throttle / 1000 + 180, env=self.env)
        except subprocess.TimeoutExpired as error:
            raise GateFailure("monkey_timeout") from error
        time.sleep(3)
        after = self.pid()
        idle_elapsed = 0
        preliminary = evaluate((self.directory / "monkey.log").read_text(errors="replace"),
                               (self.directory / "logcat.log").read_text(errors="replace"), result.returncode, self.count, before, after)
        if preliminary["status"] == "passed" and self.idle_resume_seconds:
            idle_elapsed = self.idle_and_resume(component, before)
            after = self.pid()
        if self.collector.poll() is not None:
            raise GateFailure("logcat_collection_stopped")
        self.collector.terminate()
        self.collector.wait(timeout=10)
        self.collector = None
        for handle in self.handles:
            handle.flush()
        record = evaluate((self.directory / "monkey.log").read_text(errors="replace"),
                          (self.directory / "logcat.log").read_text(errors="replace"), result.returncode, self.count, before, after)
        return dict(record, startup_ui_ready=True, startup_wait_seconds=startup_wait,
                    idle_resume_seconds=self.idle_resume_seconds,
                    idle_observed_seconds=round(idle_elapsed, 1),
                    idle_resume_passed=idle_elapsed >= self.idle_resume_seconds > 0 and record["status"] == "passed")

    def close(self):
        for process in (self.collector, self.emulator):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for handle in self.handles:
            handle.close()


def verify_upload(apk, result, require_idle_seconds=0):
    record = json.loads(result.read_text())
    if (record.get("status") != "passed" or record.get("apk_sha256") != sha256(apk)
            or record.get("events_requested") != REQUESTED_EVENTS or record.get("events_injected") != REQUESTED_EVENTS
            or record.get("input_delivery_verified") is not True
            or type(record.get("touch_events_attempted")) is not int or record["touch_events_attempted"] != REQUESTED_EVENTS - 1
            or type(record.get("touch_events_dropped")) is not int or record["touch_events_dropped"] < 0
            or type(record.get("touch_events_delivered")) is not int
            or record["touch_events_delivered"] != record["touch_events_attempted"] - record["touch_events_dropped"]
            or record["touch_events_delivered"] < MIN_DELIVERED_TOUCH_EVENTS
            or record.get("input_injection_failed") is not (record["touch_events_dropped"] > 0)
            or record.get("coverage") != "unauthenticated_startup_ui" or record.get("process_stable") is not True
            or record.get("startup_ui_ready") is not True
            or record.get("crash_or_anr_detected") is not False or record.get("monkey_aborted") is not False
            or record.get("monkey_exit_code") != 0):
        raise GateFailure("missing_or_mismatched_monkey_pass")
    if require_idle_seconds and (record.get("idle_resume_passed") is not True
            or record.get("idle_resume_seconds") != require_idle_seconds
            or type(record.get("idle_observed_seconds")) not in (int, float)
            or not require_idle_seconds <= record["idle_observed_seconds"] <= 1200):
        raise GateFailure("missing_idle_resume_pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--verify-upload", action="store_true")
    parser.add_argument("--idle-resume-seconds", type=int, choices=(0, 330), default=0)
    args = parser.parse_args()
    os.umask(0o077)
    gate = None
    try:
        if args.verify_upload:
            verify_upload(args.apk, args.result, args.idle_resume_seconds)
            print("Exact protected APK has a passing cloud Monkey smoke result.")
            return 0
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" or not args.sdk:
            raise GateFailure("requires_isolated_linux_github_runner")
        directory = args.result.parent / "cloud-monkey-private"
        directory.mkdir(mode=0o700, exist_ok=True)  # May contain sealed-regression input logs; start() still requires a fresh AVD directory.
        gate = CloudGate(args.sdk, args.apk.resolve(), directory, idle_resume_seconds=args.idle_resume_seconds)
        record = {"status": "failed", "apk_sha256": sha256(args.apk), "coverage": "unauthenticated_startup_ui", "system_image": IMAGE}
        try:
            gate.start()
            record.update(gate.test())
        except GateFailure as error:
            record["reason"] = str(error)
        except Exception:
            record["reason"] = "unexpected_test_error"
        args.result.write_text(json.dumps(record, sort_keys=True) + "\n")
        print(json.dumps(record, sort_keys=True))
        return 0 if record["status"] == "passed" else 1
    except (GateFailure, OSError, ValueError):
        print("Cloud Monkey gate failed; Odoo upload is blocked.", file=sys.stderr)
        return 1
    finally:
        if gate:
            gate.close()


if __name__ == "__main__":
    sys.exit(main())

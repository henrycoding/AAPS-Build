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
    passed = returncode == 0 and injected == requested and "Monkey finished" in monkey and stable and not crash and not aborted
    return {"status": "passed" if passed else "failed", "events_requested": requested, "events_injected": injected,
            "process_stable": stable, "crash_or_anr_detected": crash, "monkey_aborted": aborted,
            "monkey_exit_code": returncode}


def ui_ready(windows, graphics, pid):
    # A system splash/starting window is not an app-rendered view. Require an
    # actual focused app window and frames from this very process.
    focused = re.search(r"mCurrentFocus=Window\{[^\n}]*\bu\d+ " + re.escape(PACKAGE) + r"/[\w.$]+\}", windows)
    own_process = re.search(r"Graphics info for pid " + re.escape(pid) + r" \[" + re.escape(PACKAGE) + r"\]", graphics)
    frames = re.search(r"Total frames rendered:\s*(\d+)", graphics)
    return bool(focused and own_process and frames and int(frames[1]) > 0)


class CloudGate:
    def __init__(self, sdk, apk, directory, count=1000, throttle=300):
        self.sdk, self.apk, self.directory = sdk, apk, directory
        self.count, self.throttle = count, throttle
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
        while time.monotonic() < deadline:
            if self.emulator.poll() is not None:
                raise GateFailure("emulator_stopped_during_boot")
            try:
                if self.device("shell", "getprop", "sys.boot_completed", timeout=10, log="boot-adb.log").strip() == "1":
                    return
            except GateFailure:
                pass
            time.sleep(3)
        raise GateFailure("emulator_boot_timeout")

    def wait_for_ui(self, expected_pid):
        started = time.monotonic()
        deadline = started + 120
        while time.monotonic() < deadline:
            if self.pid() != expected_pid:
                raise GateFailure("app_exited_or_restarted_during_startup")
            try:
                windows = self.device("shell", "dumpsys", "window", "windows", timeout=10, log="startup-window.log")
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
        output = self.device("shell", "am", "start", "-W", "-n", component, timeout=120, log="launch.log")
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
        if self.collector.poll() is not None:
            raise GateFailure("logcat_collection_stopped")
        self.collector.terminate()
        self.collector.wait(timeout=10)
        self.collector = None
        for handle in self.handles:
            handle.flush()
        record = evaluate((self.directory / "monkey.log").read_text(errors="replace"),
                          (self.directory / "logcat.log").read_text(errors="replace"), result.returncode, self.count, before, after)
        return dict(record, startup_ui_ready=True, startup_wait_seconds=startup_wait)

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


def verify_upload(apk, result):
    record = json.loads(result.read_text())
    if (record.get("status") != "passed" or record.get("apk_sha256") != sha256(apk)
            or record.get("events_requested") != 1000 or record.get("events_injected") != 1000
            or record.get("coverage") != "unauthenticated_startup_ui" or record.get("process_stable") is not True
            or record.get("startup_ui_ready") is not True
            or record.get("crash_or_anr_detected") is not False or record.get("monkey_aborted") is not False
            or record.get("monkey_exit_code") != 0):
        raise GateFailure("missing_or_mismatched_monkey_pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--verify-upload", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    gate = None
    try:
        if args.verify_upload:
            verify_upload(args.apk, args.result)
            print("Exact protected APK has a passing cloud Monkey smoke result.")
            return 0
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" or not args.sdk:
            raise GateFailure("requires_isolated_linux_github_runner")
        directory = args.result.parent / "cloud-monkey-private"
        directory.mkdir(mode=0o700)
        gate = CloudGate(args.sdk, args.apk.resolve(), directory)
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

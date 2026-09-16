"""Workflow regression tests. Run: python3 -m unittest discover -s tests -v."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/private-gradle-build.yml"
DOCUMENT = yaml.safe_load(WORKFLOW.read_text())
STEPS = DOCUMENT["jobs"]["build"]["steps"]


def script(name):
    return next(step["run"] for step in STEPS if step["name"] == name)


FAKE_GRADLE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
stage = ("stop" if "--stop" in args else "prep" if ":app:syncProtectPackerConfig" in args
         else "shell" if ":shell:exportShellDex" in args else "app")
def record(event):
    with open(os.environ["BUILD_TRACE"], "a") as stream:
        stream.write(json.dumps({"stage": stage, "event": event, "args": args,
                                 "cwd": os.getcwd(), "java": os.environ.get("JAVA_HOME")}) + "\n")
record("start")
time.sleep(0.05)
if os.environ.get("FAIL_STAGE") == stage:
    print("private-build-diagnostic")
    sys.exit(7)
root = pathlib.Path(os.environ["SOURCE_ROOT"])
outputs = {
    "prep": ["app/protect-packer.runtime.toml",
             "third_party/nmmp/nmm-protect/build/libs/tools/config.json",
             "third_party/nmmp/nmm-protect/build/libs/vm-protect-test.jar"],
    "shell": ["third_party/protectPacker/shell/build/exported-shell/classes.dex",
              "third_party/protectPacker/shell/build/exported-shell/lib/arm64-v8a/libandroidx.graphics.path.so"],
}.get(stage, [])
if os.environ.get("OMIT_ARTIFACT"):
    outputs = [path for path in outputs if not path.endswith("classes.dex")]
for path in outputs:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("test fixture")
record("end")
'''


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in (".", "third_party/protectPacker", "third_party/nmmp/nmm-protect"):
            wrapper = self.root / directory / "gradlew"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(FAKE_GRADLE)
            wrapper.chmod(0o755)
        self.trace = self.root / "trace.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        free = bin_dir / "free"
        free.write_text("#!/bin/sh\nprintf 'test memory snapshot\\n'\n")
        free.chmod(0o755)
        self.env = {
            **os.environ, "SOURCE_ROOT": str(self.root), "RUNNER_TEMP": str(self.root),
            "BUILD_TRACE": str(self.trace), "FLAVOR": "full", "SHOW_FAILURE_LOG_TAIL": "false",
            "JAVA_HOME_17_X64": "/test/jdk17",
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        }

    def run_step(self, name, **env):
        # The heartbeat's sleep child can outlive its shell briefly. Capture to
        # files so an inherited pipe cannot keep communicate() waiting for it.
        with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(mode="w+") as stderr:
            result = subprocess.run(
                ["bash", "-c", script(name)], cwd=self.root, env={**self.env, **env},
                stdout=stdout, stderr=stderr, text=True, timeout=15,
            )
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(result.args, result.returncode, stdout.read(), stderr.read())

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def prebuild(self, **env):
        return self.run_step("Prebuild release and protected shell sequentially", **env)

    def test_all_shell_steps_parse(self):
        for step in STEPS:
            if "run" not in step:
                continue
            with self.subTest(step=step["name"]):
                rendered = re.sub(r"\$\{\{.*?\}\}", "test-value", step["run"])
                result = subprocess.run(["bash", "-n"], input=rendered, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_prebuild_stages_do_not_overlap_for_either_flavor(self):
        for flavor in ("full", "mednovo"):
            with self.subTest(flavor=flavor):
                if self.trace.exists():
                    self.trace.unlink()
                result = self.prebuild(FLAVOR=flavor)
                self.assertEqual(result.returncode, 0, result.stderr)
                events = self.events()
                self.assertEqual([(e["stage"], e["event"]) for e in events], [
                    (stage, event) for stage in ("prep", "app", "shell") for event in ("start", "end")
                ])
                for event in events:
                    self.assertIn("--no-daemon", event["args"])
                    self.assertIn("--no-parallel", event["args"])
                    self.assertIn("--max-workers=2", event["args"])
                self.assertIn(f":app:assemble{flavor.capitalize()}Release", events[2]["args"])

    def test_failure_stops_later_stages_without_printing_private_log(self):
        for failure, expected in (("prep", ["prep"]), ("app", ["prep", "app"]), ("shell", ["prep", "app", "shell"])):
            with self.subTest(stage=failure):
                if self.trace.exists():
                    self.trace.unlink()
                result = self.prebuild(FAIL_STAGE=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([e["stage"] for e in self.events() if e["event"] == "start"], expected)
                self.assertNotIn("private-build-diagnostic", result.stdout + result.stderr)

    def test_failure_logs_remain_opt_in(self):
        result = self.prebuild(FAIL_STAGE="app", SHOW_FAILURE_LOG_TAIL="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private-build-diagnostic", result.stdout)

    def test_missing_protected_shell_is_not_success(self):
        result = self.prebuild(OMIT_ARTIFACT="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not produce", result.stdout)

    def test_cleanup_uses_each_wrapper_and_java17_for_nmmp(self):
        result = self.run_step("Release prebuild daemons before final protection")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [e for e in self.events() if e["event"] == "start"]
        self.assertEqual(len(events), 3, result.stderr)
        self.assertTrue(all(e["args"] == ["--stop"] for e in events))
        self.assertEqual(events[-1]["java"], "/test/jdk17")
        self.assertTrue(events[-1]["cwd"].endswith("third_party/nmmp/nmm-protect"))

    def test_nested_and_final_build_memory_limits(self):
        nested = script("Bound nested Gradle and tool memory")
        for value in ("org.gradle.daemon=false", "org.gradle.parallel=false", "org.gradle.workers.max=2",
                      "kotlin.compiler.execution.strategy=in-process", "maxHeapSize = '1536m'"):
            self.assertIn(value, nested)
        final = script("Build without publishing public artifacts")
        self.assertIn("--no-daemon --no-parallel", final)
        self.assertNotIn("-Xmx5120m", WORKFLOW.read_text())
        names = [step["name"] for step in STEPS]
        self.assertLess(names.index("Release prebuild daemons before final protection"),
                        names.index("Build without publishing public artifacts"))
        self.assertEqual(DOCUMENT["jobs"]["build"]["env"]["CMAKE_BUILD_PARALLEL_LEVEL"], "2")


if __name__ == "__main__":
    unittest.main()

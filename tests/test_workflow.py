"""Workflow regression tests. Run: python3 -m unittest discover -s tests -v."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
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
    def test_cloud_controls_must_pass_before_expensive_private_build(self):
        self.assertEqual('monkey_controls', DOCUMENT['jobs']['build']['needs'])
        self.assertEqual('./.github/workflows/cloud-monkey-self-test.yml', DOCUMENT['jobs']['monkey_controls']['uses'])
        control = yaml.safe_load((WORKFLOW.parent / 'cloud-monkey-self-test.yml').read_text())
        self.assertIn('workflow_call', control.get('on', control.get(True)))

    def test_monkey_pass_and_apk_hash_verification_precede_upload(self):
        names = [step['name'] for step in STEPS]
        prepare = names.index('Prepare Google Play cloud emulator')
        monkey = names.index('Monkey test exact protected APK in cloud')
        upload = names.index('Upload APK draft to Odoo AAPS update manager')
        self.assertLess(names.index('Verify protected APK output'), prepare)
        self.assertLess(prepare, monkey)
        self.assertLess(monkey, upload)
        self.assertIn("steps.monkey.outcome == 'success'", STEPS[upload]['if'])
        admission = script('Upload APK draft to Odoo AAPS update manager')
        self.assertLess(admission.index('--verify-upload'), admission.index('curl --fail'))
        self.assertIn('app-${FLAVOR}-release-protected.apk', script('Monkey test exact protected APK in cloud'))
        self.assertNotIn('continue-on-error', STEPS[monkey])
        artifacts = [step for step in STEPS if 'upload-artifact' in step.get('uses', '')]
        self.assertEqual(1, len(artifacts))
        self.assertEqual('${{ runner.temp }}/monkey-diagnostics.cms', artifacts[0]['with']['path'])
        self.assertIn("steps.monkey.outcome == 'failure'", artifacts[0]['if'])
        encrypted = script('Encrypt failed Monkey diagnostics for operator')
        self.assertIn('encrypt_monkey_diagnostics.py', encrypted)
        self.assertIn('--certificate', encrypted)

    def test_retest_uses_identical_cloud_gate_and_upload_with_exclusive_job(self):
        self.assertEqual("${{ inputs.retest_run == '' }}", DOCUMENT['jobs']['build']['if'])
        retest = DOCUMENT['jobs']['retest']
        self.assertEqual("${{ inputs.retest_run != '' }}", retest['if'])
        self.assertEqual('monkey_controls', retest['needs'])
        self.assertEqual({'contents': 'write', 'actions': 'read'}, retest['permissions'])
        self.assertEqual('read', DOCUMENT['permissions']['contents'])
        self.assertNotIn('GH_TOKEN', retest['env'])
        self.assertIn('GH_TOKEN', retest['steps'][1]['env'])
        shared = next(i for i, step in enumerate(STEPS) if step['name'] == 'Prepare Google Play cloud emulator')
        self.assertEqual(STEPS[shared:], retest['steps'][2:])
        recovery = retest['steps'][1]['run']
        self.assertIn('validate_retest_source.py', recovery)
        self.assertIn('"${signer}" verify', recovery)
        self.assertNotIn('diagnostics.key', recovery)

    def test_failed_missing_or_stale_monkey_result_never_reaches_odoo(self):
        import hashlib
        import shutil
        tools = self.root / 'build-tools/scripts'
        tools.mkdir(parents=True)
        shutil.copyfile(WORKFLOW.parents[2] / 'scripts/cloud_monkey_gate.py', tools / 'cloud_monkey_gate.py')
        apk = self.root / 'app/full/app-full-release-protected.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'protected-apk-fixture')
        curl = self.root / 'bin/curl'
        curl.write_text('#!/bin/sh\n: > "$UPLOAD_MARKER"\n')
        curl.chmod(0o755)
        marker = self.root / 'uploaded'
        record = {'status': 'passed', 'apk_sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
                  'coverage': 'unauthenticated_startup_ui', 'startup_ui_ready': True, 'input_injection_failed': False, 'events_requested': 1200, 'events_injected': 1200,
                  'touch_events_attempted': 1199, 'touch_events_dropped': 0, 'touch_events_delivered': 1199, 'input_delivery_verified': True,
                  'process_stable': True, 'crash_or_anr_detected': False, 'monkey_aborted': False, 'monkey_exit_code': 0}
        result_file = self.root / 'monkey-result.json'
        for patch in (None, {'status': 'failed'}, {'apk_sha256': 'stale'}, {'events_injected': 999}, {}):
            with self.subTest(patch=patch):
                if result_file.exists():
                    result_file.unlink()
                if marker.exists():
                    marker.unlink()
                if patch is not None:
                    result_file.write_text(json.dumps({**record, **patch}))
                run = self.run_step('Upload APK draft to Odoo AAPS update manager', GITHUB_WORKSPACE=str(self.root),
                                    HOST='https://invalid.test', AAPS_UPDATE_TOKEN='test-only-token', FORCE_UPDATE='false',
                                    CHANGELOG='', UPLOAD_MARKER=str(marker))
                allowed = patch == {}
                self.assertEqual(run.returncode == 0, allowed, run.stdout + run.stderr)
                self.assertEqual(marker.exists(), allowed)

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
        if sys.platform == "darwin":
            # The workflow runs GNU sed; adapt its in-place syntax for local BSD sed.
            sed = bin_dir / "sed"
            sed.write_text('#!/bin/sh\nif [ "$1" = "-i" ]; then shift; exec /usr/bin/sed -i "" "$@"; fi\nexec /usr/bin/sed "$@"\n')
            sed.chmod(0o755)
        self.env = {
            **os.environ, "SOURCE_ROOT": str(self.root), "RUNNER_TEMP": str(self.root),
            "BUILD_TRACE": str(self.trace), "FLAVOR": "full", "SHOW_FAILURE_LOG_TAIL": "false",
            "JAVA_HOME_17_X64": "/test/jdk17",
            "GITHUB_ENV": str(self.root / "github-env"),
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
        workflows = [yaml.safe_load(path.read_text()) for path in WORKFLOW.parent.glob('*.yml')]
        all_steps = [step for document in workflows for job in document['jobs'].values() for step in job.get('steps', [])]
        for step in all_steps:
            if "run" not in step:
                continue
            with self.subTest(step=step.get("name", "unnamed")):
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

    def configure_packer(self, invocation):
        app_build = self.root / "app/build.gradle.kts"
        app_build.parent.mkdir(exist_ok=True)
        app_build.write_text('fun generateBuildVersion(): String {\n    return dateFormat.format(Date())\n}\n' + invocation)
        result = self.run_step("Configure final protection to reuse the prebuilt shell")
        return result, app_build.read_text()

    def test_cli_packer_is_accepted_without_restoring_nested_gradle(self):
        result, patched = self.configure_packer(
            'dependsOn(gradle.includedBuild("protectPacker").task(":packer:installDist"))\n'
            'commandLine(protectPackerRoot.file("packer/build/install/packer/bin/packer").asFile.absolutePath)\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(':packer:run', patched)
        self.assertNotIn(':shell:exportShellDex', patched)
        self.assertIn('System.getenv("AAPS_CI_BUILD_VERSION")', patched)
        self.assertIn('AAPS_PACKER_MODE=cli', (self.root / "github-env").read_text())

    def test_legacy_packer_keeps_prebuilt_shell_exclusion(self):
        result, patched = self.configure_packer('commandLine(\n    ":packer:run",\n)\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"-x",\n    ":shell:exportShellDex",\n    ":packer:run",', patched)
        self.assertIn('AAPS_PACKER_MODE=legacy', (self.root / "github-env").read_text())

    def test_unknown_or_incomplete_packer_invocations_are_rejected(self):
        for invocation in ('commandLine("unknown")',
                           'dependsOn(gradle.includedBuild("protectPacker").task(":packer:installDist"))',
                           'commandLine(protectPackerRoot.file("packer/build/install/packer/bin/packer"))',
                           '    ":packer:run",\n    ":packer:run",\n'):
            with self.subTest(invocation=invocation):
                result, _ = self.configure_packer(invocation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsupported packer invocation', result.stdout)

    def test_final_build_reuses_shell_only_for_cli_mode(self):
        for mode in ('cli', 'legacy'):
            with self.subTest(mode=mode):
                if self.trace.exists():
                    self.trace.unlink()
                result = self.run_step("Build without publishing public artifacts",
                                       AAPS_PACKER_MODE=mode, GRADLE_TASK_INPUT="")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = self.events()[0]['args']
                self.assertIn(':app:assembleFullRelease', args)
                self.assertEqual(':protectPacker:shell:exportShellDex' in args, mode == 'cli')
                self.assertNotIn(':app:protectPackerFullRelease', args)
                self.assertNotIn(':packer:installDist', args)


if __name__ == "__main__":
    unittest.main()

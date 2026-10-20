# AAPS private build

The `Private Gradle Build` workflow checks out the source selected by
`private_ref`, builds and protects the APK, runs a cloud Monkey gate on that exact
APK, and optionally uploads an Odoo draft only after the gate passes.

## Cloud Monkey gate

The same Ubuntu runner starts a fresh Android 11 Google Play x86_64 emulator
with ARM translation. Its genuine user/release-key properties are checked;
no app protection, authentication, system properties, or APK bytes are weakened
for emulator compatibility. If translation or the protected runtime cannot start,
the gate fails and upload is blocked. This first hosted run must establish actual
compatibility; unit tests alone do not prove it.

The gate injects 1000 touch/motion events with seed 42 and a 300 ms throttle.
Crashes, ANRs, partial event counts, a missing/dead/restarted app process, a
timeout, or missing log collection fail the gate. It never uses Monkey's
ignore-crashes/ignore-timeouts flags. Before uploading, the APK SHA-256 must match
the passing result. The existing manual script in the private source repository
is separate and is not used as the admission check.

Coverage is **unauthenticated startup and UI smoke only** on a clean emulator.
No production customer credentials, logged-in functionality, BLE connections,
pump commands, or dosing paths are exercised. This does not replace physical
device/native ARM or pump validation. The emulator never selects an attached USB
device. Broader UI coverage requires a separately prepared test account and flow.

Only a fixed summary/result is printed to the public workflow log. Raw logcat,
Monkey output, and emulator logs stay in the runner's private temporary directory;
no APK, screenshots or raw logs are published as public Actions artifacts.

The manual `Cloud Monkey gate self-test` workflow runs the same gate against
disposable public fixtures: a healthy app must complete 1000 events, while apps
which deliberately crash or ANR on touch must fail. It also rejects a different
APK paired with the healthy result. These controls validate the gate in Actions;
they do not substitute for the protected APK's own run.

## Runner memory budget

The 16 GiB runner must accommodate Gradle, Kotlin, the packer, VMP/dex2c,
Python, and native compilers/linkers together. JVM heap limits are per process,
not a limit on total runner memory.

- Run tool preparation, app prebuild, and protected-shell prebuild sequentially.
- Use single-use Gradle daemons, no project parallelism, and two workers.
- Main build JVM heap: 3 GiB; nested Gradle and shell heap: 2 GiB.
- Kotlin compilation runs in-process instead of retaining extra Kotlin daemons.
- JavaExec and standalone Java tools default to 1.5 GiB heaps.
- Limit JVM-reported CPUs and CMake build parallelism to two.
- Stop daemons by wrapper/version before the final protection stage.

These limits leave room for native and non-heap memory; they are not a hard
aggregate memory guarantee. Protection, signing, and APK verification remain
enabled. Sequential phases may take longer; validate peak memory and completion
on the next hosted run before increasing concurrency.

Both the legacy nested `:packer:run` invocation and the current included-build
`installDist` plus packer CLI are supported. For the CLI path, final protection
excludes only `:protectPacker:shell:exportShellDex` after the sequential prebuild
has checked the protected shell outputs. The distribution still packages those
outputs and runs the complete APK protection and signing stages.

## Workflow regression tests

```sh
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
```

Tests parse the workflow, syntax-check every shell step, and execute prebuild
scripts with fake Gradle wrappers to check sequencing, both flavors, failure
propagation, artifact checks, log privacy, and daemon cleanup. They do not compile
or protect a real APK.

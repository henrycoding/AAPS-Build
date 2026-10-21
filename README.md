# AAPS private build

The `Private Gradle Build` workflow checks out the source selected by
`private_ref`, builds and protects the APK, runs a cloud Monkey gate on that exact
APK, and optionally uploads an Odoo draft only after the gate passes.

## Cloud Monkey gate

The same Ubuntu runner starts a fresh Android 11 Google Play x86_64 emulator
with ARM translation and 4 GiB guest RAM. Its genuine user/release-key properties are checked;
no app protection, authentication, system properties, or APK bytes are weakened
for emulator compatibility. If translation or the protected runtime cannot start,
the gate fails and upload is blocked. This first hosted run must establish actual
compatibility; unit tests alone do not prove it.

The gate schedules 1200 events and requires at least 1000 successful touch/motion injections with seed 42 and a 300 ms throttle.
Crashes, ANRs, partial event counts, a missing/dead/restarted app process, a
timeout, or missing log collection fail the gate. It never uses Monkey's
ignore-crashes/ignore-timeouts flags. Before uploading, the APK SHA-256 must match
the passing result. The existing manual script in the private source repository
is separate and is not used as the admission check.

The protected APK must also survive 330 seconds in the background after Monkey,
then resume with the same PID and a focused application window with freshly
rendered frames. Frame counters are reset before resume to reject stale startup
evidence, and logcat remains active throughout. A further 20-second observation
checks the asynchronous resume work. This crosses the 300-second integrity trust
lifetime without weakening it; it is not an authenticated or OEM suspend test.
Both build and retest upload steps require the verified 330-second result as well
as the APK hash. The healthy public control exercises this path before building.
Any low-memory process kill also fails this same-PID check. The 4 GiB Play image
coverage does not establish background survival on low-RAM watches or phones.

After a new protected APK is built, real Java state/publication fault cases,
native JNI inventory and tamper cases, and native instrumentation fixtures run
before Monkey. Full builds also run the packer suite using the freshly built
Release Manifest and protected shell. Failures block Monkey/upload and keep the
exact APK plus regression log in the same encrypted operator diagnostics.

Coverage is **unauthenticated startup and UI smoke only** on a clean emulator.
No production customer credentials, logged-in functionality, BLE connections,
pump commands, or dosing paths are exercised. This does not replace physical
device/native ARM or pump validation. The emulator never selects an attached USB
device. Broader UI coverage requires a separately prepared test account and flow.

Only a fixed summary/result is printed to the public workflow log. Raw logcat,
Monkey output, and emulator logs stay in the runner's private temporary directory;
no plaintext APK, screenshots or raw logs are published as public Actions artifacts.
On gate failure, the exact APK and diagnostic logs are streamed directly into an
OpenSSL CMS encrypted archive with a 3-day artifact retention. The decryption key
stays solely on the operator machine; CI holds only the public certificate.
The archive never contains signing keys, checkout credentials, or Odoo secrets.
The launcher is resolved from the installed manifest, including R8-renamed classes.

The manual `Cloud Monkey gate self-test` workflow runs the same gate against
disposable public fixtures: a healthy app must complete 1200 scheduled events and at least 1000 successful touch injections, while apps
which deliberately crash or ANR on touch must fail. It also rejects a different
APK paired with the healthy result. These controls validate the gate in Actions;
they do not substitute for the protected APK's own run.
The private build calls this reusable workflow as a required dependency first,
so a broken cloud test environment blocks the expensive build before checkout.

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

## Exact APK retest

An Android `am start -W` timeout remains pending until the same process has a
focused app window and rendered frames, with a 120-second readiness deadline.
A stalled, dead or restarted process is rejected before Monkey begins. Prelaunch
uses the same MAIN/LAUNCHER intent and task flags as Monkey, avoiding an extra
Activity instance at the start of injection. The result counts successful touch injections separately: one initial Activity
switch and dropped inputs never count toward the 1000-success minimum. The
reported drop summary must match observed injection failures; missing/inconsistent
traces, incomplete runs or fewer than 1000 successful touches block admission.
Drops remain visible in the result; crashes and ANRs always fail the gate. Cloud
controls cover a 12-second healthy startup and a deliberately hung startup.

For a gate-only correction, `retest_run` and `retest_apk_sha256` select an existing
failed build's unchanged signed APK. The mutually exclusive retest job requires
successful original protected-output and runtime-regression verification, a failed original Monkey gate,
a skipped original Odoo upload, matching source/flavor and original gate SHA-256,
a valid APK signature, and the same mandatory controls/Monkey/upload gate.
A temporary encrypted draft-release asset transfers those bytes, using a separate
disposable retest key in a repository secret. The operator diagnostic private key
never enters CI. Remove the temporary release and secret after retesting.

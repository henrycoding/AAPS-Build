# AAPS private build

The `Private Gradle Build` workflow checks out the source selected by
`private_ref`, builds and protects the APK, and optionally uploads an Odoo draft.

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

## Workflow regression tests

```sh
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
```

Tests parse the workflow, syntax-check every shell step, and execute prebuild
scripts with fake Gradle wrappers to check sequencing, both flavors, failure
propagation, artifact checks, log privacy, and daemon cleanup. They do not compile
or protect a real APK.

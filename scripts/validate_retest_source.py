#!/usr/bin/env python3
"""Admit retests only for bytes from a verified build blocked at the Monkey gate."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile


def validate(run, jobs, log, source_ref, flavor, expected_sha):
    if run.get('workflow_id') != 306791256 or run.get('conclusion') != 'failure':
        raise ValueError('not_a_failed_private_build')
    build = next(j for j in jobs if j['name'] == 'Build private Gradle project')
    steps = {s['name']: s['conclusion'] for s in build['steps']}
    if (steps.get('Verify protected APK output') != 'success'
            or steps.get('Verify protected runtime regressions') != 'success'
            or steps.get('Monkey test exact protected APK in cloud') != 'failure'
            or steps.get('Upload APK draft to Odoo AAPS update manager') != 'skipped'):
        raise ValueError('source_build_not_verified_and_blocked_at_monkey')
    if 'PRIVATE_REF: ' + source_ref not in log or 'FLAVOR: ' + flavor not in log:
        raise ValueError('source_ref_or_flavor_mismatch')
    records = [json.loads(m[0]) for m in re.finditer(r'\{"apk_sha256":[^\n]+\}', log)]
    if not any(r.get('apk_sha256') == expected_sha and r.get('status') == 'failed' for r in records):
        raise ValueError('original_gate_apk_hash_mismatch')


def unpack(archive, destination, expected_sha):
    import hashlib
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if len(members) != 1 or members[0].name != 'tested.apk' or not members[0].isfile():
            raise ValueError('unexpected_archive_content')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(members[0]) as source, destination.open('wb') as target:
            import shutil
            shutil.copyfileobj(source, target)
    with destination.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected_sha:
        destination.unlink()
        raise ValueError('retest_apk_bytes_mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--apk', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    run_id = os.environ['RETEST_RUN']
    sha = os.environ['RETEST_APK_SHA256']
    source = os.environ['PRIVATE_REF']
    if not re.fullmatch(r'[0-9]+', run_id) or not re.fullmatch(r'[0-9a-f]{64}', sha) or not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('invalid_retest_reference')
    repo = os.environ['GITHUB_REPOSITORY']
    def gh(*command):
        return subprocess.check_output(['gh', *command], text=True)
    run = json.loads(gh('api', f'repos/{repo}/actions/runs/{run_id}'))
    jobs = json.loads(gh('run', 'view', run_id, '--repo', repo, '--json', 'jobs'))['jobs']
    log = gh('run', 'view', run_id, '--repo', repo, '--log-failed')
    validate(run, jobs, log, source, os.environ['FLAVOR'], sha)
    unpack(args.archive, args.apk, sha)
    print('Retest APK matches the verified original build SHA-256; no APK bytes changed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Protected APK retest provenance validation failed; upload is blocked.', file=sys.stderr)
        sys.exit(1)

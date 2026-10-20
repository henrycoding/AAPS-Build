#!/usr/bin/env python3
"""Seal failure diagnostics to an operator certificate before artifact upload.

The private decryption key exists only on the operator machine, never on CI.
Only APK bytes, the gate result and its isolated logs enter this archive.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tarfile


def seal(apk, result, certificate, output):
    os.umask(0o077)
    subprocess.run(['openssl', 'x509', '-in', str(certificate), '-checkend', '0', '-noout'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command = ['openssl', 'cms', '-encrypt', '-binary', '-aes-256-cbc', '-outform', 'DER',
               '-out', str(output), str(certificate)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=process.stdin, mode='w|gz') as archive:
            archive.add(apk, arcname='tested.apk', recursive=False)
            if result.is_file():
                archive.add(result, arcname='result.json', recursive=False)
            logs = result.parent / 'cloud-monkey-private'
            if logs.is_dir():
                archive.add(logs, arcname='diagnostics', recursive=False)
                # Exclude emulator userdata/AVD disk images; only diagnostic
                # text logs belong in the sealed failure bundle.
                for log in sorted(logs.glob('*.log')):
                    if log.is_file() and not log.is_symlink():
                        archive.add(log, arcname='diagnostics/' + log.name, recursive=False)
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('diagnostic encryption failed')
    except BaseException:
        process.kill()
        process.wait()
        output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('apk', 'result', 'certificate', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    seal(args.apk, args.result, args.certificate, args.output)
    print('Failure diagnostics sealed; only encrypted ciphertext may be uploaded.')


if __name__ == '__main__':
    main()

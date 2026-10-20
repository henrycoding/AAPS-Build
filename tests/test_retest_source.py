import importlib.util
import io
import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('retest', Path(__file__).resolve().parents[1] / 'scripts/validate_retest_source.py')
retest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retest)


class RetestTest(unittest.TestCase):
    def test_original_build_and_hash_must_prove_a_gate_only_failure(self):
        run = {'workflow_id': 306791256, 'conclusion': 'failure'}
        steps = {'Verify protected APK output': 'success', 'Monkey test exact protected APK in cloud': 'failure',
                 'Upload APK draft to Odoo AAPS update manager': 'skipped'}
        jobs = lambda s: [{'name': 'Build private Gradle project', 'steps': [{'name': k, 'conclusion': v} for k, v in s.items()]}]
        log = 'PRIVATE_REF: source\nFLAVOR: full\n{"apk_sha256": "expected", "status": "failed"}'
        retest.validate(run, jobs(steps), log, 'source', 'full', 'expected')
        for changes in ({'Verify protected APK output': 'failure'},
                        {'Monkey test exact protected APK in cloud': 'skipped'},
                        {'Upload APK draft to Odoo AAPS update manager': 'success'}):
            with self.assertRaises(ValueError):
                retest.validate(run, jobs({**steps, **changes}), log, 'source', 'full', 'expected')
        for source, flavor, sha in (('other', 'full', 'expected'), ('source', 'mednovo', 'expected'), ('source', 'full', 'other')):
            with self.assertRaises(ValueError):
                retest.validate(run, jobs(steps), log, source, flavor, sha)
        with self.assertRaises(ValueError):
            retest.validate({**run, 'workflow_id': 1}, jobs(steps), log, 'source', 'full', 'expected')

    def test_only_original_unchanged_apk_can_be_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'signed protected fixture'
            digest = hashlib.sha256(data).hexdigest()
            for filename, sha, allowed in (('tested.apk', digest, True), ('tested.apk', 'wrong', False), ('../escape', digest, False)):
                archive = root / 'sealed.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    item = tarfile.TarInfo(filename); item.size = len(data)
                    tar.addfile(item, io.BytesIO(data))
                target = root / 'apk.apk'
                if allowed:
                    retest.unpack(archive, target, sha)
                    self.assertEqual(data, target.read_bytes())
                else:
                    with self.assertRaises(ValueError):
                        retest.unpack(archive, target, sha)

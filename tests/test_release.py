import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT/'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
FILES = ('pyproject.toml', 'daemon/Cargo.toml', 'daemon/Cargo.lock', 'cwm_panel/__init__.py')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in FILES:
            destination = self.root/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, destination)

    def test_normalizes_optional_v_and_rejects_unsafe_or_ambiguous_versions(self):
        self.assertEqual(release.normalize('v2.10.0'), '2.10.0')
        self.assertEqual(release.normalize('0.0.0'), '0.0.0')
        for invalid in ('', '1.2', '01.2.3', '1.02.3', ' 1.2.3', '1.2.3\n',
                        '1.2.3-rc.1', '1.2.3;id', '$(id)', '1.2.3/other',
                        '18446744073709551616.0.0', '１.２.３'):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                release.normalize(invalid)

    def test_stamps_python_cargo_and_lockfile_consistently(self):
        release.stamp(self.root, 'v2.10.0')
        project = tomllib.loads((self.root/'pyproject.toml').read_text())
        cargo = tomllib.loads((self.root/'daemon/Cargo.toml').read_text())
        lock = tomllib.loads((self.root/'daemon/Cargo.lock').read_text())
        self.assertEqual(project['project']['version'], '2.10.0')
        self.assertEqual(cargo['package']['version'], '2.10.0')
        self.assertEqual(lock['package'][0]['version'], '2.10.0')
        self.assertIn('__version__ = "2.10.0"', (self.root/'cwm_panel/__init__.py').read_text())

    def test_metadata_error_leaves_all_files_unchanged(self):
        (self.root/'daemon/Cargo.lock').write_text('invalid fixture')
        before = {name: (self.root/name).read_bytes() for name in FILES}
        with self.assertRaises(ValueError):
            release.stamp(self.root, '2.0.0')
        self.assertEqual(before, {name: (self.root/name).read_bytes() for name in FILES})

    def test_check_mode_does_not_change_source(self):
        before = {name: (ROOT/name).read_bytes() for name in FILES}
        result = subprocess.run(['python3', str(ROOT/'scripts/release.py'), 'v3.2.1', '--check'],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), '3.2.1')
        self.assertEqual(before, {name: (ROOT/name).read_bytes() for name in FILES})

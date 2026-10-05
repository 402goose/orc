"""Worker test runs must leave the inherited control workspace untouched."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class EnvironmentIsolationTest(unittest.TestCase):
    def test_direct_unittest_ignores_inherited_control_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory)
            sentinel = decoy / 'sentinel'
            sentinel.write_text('untouched')
            result = subprocess.run(
                [sys.executable, '-m', 'unittest', 'test.route_explain_test'],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'FUSION_CONTROL_WORKSPACE': str(decoy),
                     'FUSION_LAYA_PYTHON': shutil.which('false') or '/usr/bin/false'},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sorted(p.name for p in decoy.iterdir()), ['sentinel'])
            self.assertEqual(sentinel.read_text(), 'untouched')

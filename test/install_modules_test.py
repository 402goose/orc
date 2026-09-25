"""install.sh copies Python modules by name; a new module left off the list
installs cleanly and then fails at import on first use."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallModulesTest(unittest.TestCase):
    def test_every_fusion_module_is_installed(self):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        # Copying every fusion_*.py covers new modules by construction.
        if '"/fusion_*.py' in script:
            return
        installed = set(re.findall(r"\bfusion_\w+", script))
        modules = {path.stem for path in ROOT.glob("fusion_*.py")}
        self.assertEqual(sorted(modules - installed), [])


if __name__ == "__main__":
    unittest.main()

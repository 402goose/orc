"""Per-run reader temp directories carry an owner marker, and the sweep removes only stale ones:
plain directories owned by this user, named as ORC names them, older than the limit, carrying
ORC's marker, whose owner is gone."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_tmp

DAY = 24 * 3600


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TmpSweepTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = time.time()

    def make(self, name, owner="dead", age=2 * DAY):
        path = self.root / name
        path.mkdir()
        (path / "claude-1").mkdir()
        (path / "claude-1" / "scratch.txt").write_text("x")
        if owner == "dead":
            (path / fusion_tmp.MARKER).write_text(json.dumps({"pid": dead_pid(), "start": "x"}))
        elif owner == "live":
            fusion_tmp.mark(path)
        elif owner == "recycled":
            (path / fusion_tmp.MARKER).write_text(json.dumps({"pid": os.getpid(), "start": "Mon Jan  1 00:00:00 2001"}))
        when = self.now - age
        os.utime(path, (when, when))
        return path

    def sweep(self, **kwargs):
        return fusion_tmp.sweep(self.root, now=self.now, **kwargs)

    def test_an_old_directory_whose_owner_is_dead_is_removed(self):
        stale = self.make("orc-abcd1234")
        result = self.sweep()
        self.assertFalse(stale.exists())
        self.assertEqual(result["removed"], [str(stale)])

    def test_an_old_directory_whose_owner_is_running_is_kept(self):
        live = self.make("orc-livedir1", owner="live")
        result = self.sweep()
        self.assertTrue(live.exists())
        self.assertEqual(result["kept"], [{"path": str(live), "reason": "owner still running"}])

    def test_a_recycled_pid_does_not_keep_a_directory(self):
        recycled = self.make("orc-0123456789ab", owner="recycled")
        self.sweep()
        self.assertFalse(recycled.exists())

    def test_an_old_directory_without_a_marker_is_kept(self):
        bare = self.make("orc-blockers", owner=None)
        result = self.sweep()
        self.assertTrue(bare.exists())
        self.assertEqual(result["kept"], [{"path": str(bare), "reason": "no ORC owner marker"}])

    def test_a_run_prefixed_directory_whose_owner_is_dead_is_removed(self):
        stale = self.make("orc-run-abcd1234")
        result = self.sweep()
        self.assertFalse(stale.exists())
        self.assertEqual(result["removed"], [str(stale)])

    def test_the_cap_counts_only_names_orc_creates(self):
        for index in range(fusion_tmp.LIMIT + 100):
            (self.root / f"aaa-{index:04d}").write_text("x")
        stale = self.make("orc-zz991234")
        result = self.sweep()
        self.assertFalse(stale.exists())
        self.assertEqual(result["removed"], [str(stale)])

    def test_a_young_directory_is_kept_even_when_its_owner_is_dead(self):
        young = self.make("orc-young123", age=60)
        result = self.sweep()
        self.assertTrue(young.exists())
        self.assertEqual(result["removed"], [])

    def test_a_symlink_that_matches_the_pattern_is_never_followed_or_removed(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link = self.root / "orc-linkdir1"
        link.symlink_to(target, target_is_directory=True)
        result = self.sweep()
        self.assertTrue(link.is_symlink())
        self.assertEqual((target / "keep.txt").read_text(), "keep")
        self.assertIn({"path": str(link), "reason": "not a directory"}, result["kept"])

    def test_a_directory_owned_by_another_user_is_skipped(self):
        other = self.make("orc-otheruid")
        with patch.object(fusion_tmp.os, "getuid", return_value=os.getuid() + 1):
            result = self.sweep()
        self.assertTrue(other.exists())
        self.assertEqual(result["kept"], [{"path": str(other), "reason": "owned by another user"}])

    def test_names_orc_does_not_create_are_left_alone(self):
        names = ["orc-ui-tools", "orc-rebuild-a", "orc-abc", "orc-ABCDEFGH", "orc-abcd12345", "other-abcd1234"]
        paths = [self.make(name) for name in names]
        result = self.sweep()
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(result["removed"], [])

    def test_a_marker_that_is_a_symlink_is_not_read(self):
        stale = self.make("orc-marklink", owner=None)
        decoy = self.root / "decoy.json"
        decoy.write_text(json.dumps({"pid": os.getpid(), "start": fusion_tmp.process_start(os.getpid())}))
        (stale / fusion_tmp.MARKER).symlink_to(decoy)
        os.utime(stale, (self.now - 2 * DAY, self.now - 2 * DAY))
        result = self.sweep()
        self.assertTrue(stale.exists())
        self.assertTrue(decoy.exists())
        self.assertIn({"path": str(stale), "reason": "no ORC owner marker"}, result["kept"])

    def test_a_dry_run_lists_without_removing(self):
        stale = self.make("orc-dryrun12")
        out = io.StringIO()
        with patch.object(fusion_tmp, "ROOT", self.root), contextlib.redirect_stdout(out):
            code = core.main(["tmp", "sweep", "--dry-run", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(stale.exists())
        self.assertEqual(payload["removed"], [str(stale)])
        self.assertTrue(payload["dry_run"])

    def test_the_cli_removes_and_reports(self):
        stale = self.make("orc-cliremov")
        out = io.StringIO()
        with patch.object(fusion_tmp, "ROOT", self.root), contextlib.redirect_stdout(out):
            code = core.main(["tmp", "sweep"])
        self.assertEqual(code, 0)
        self.assertFalse(stale.exists())
        self.assertIn(f"removed {stale}", out.getvalue())

    def test_a_new_reader_directory_sweeps_first_and_carries_its_owner(self):
        stale = self.make("orc-oldcodex")
        env = {}
        with patch.object(fusion_tmp, "ROOT", self.root), patch.dict(os.environ, {"FUSION_TMP_SWEEP": "1"}):
            directory = core.codex_reader_tmpdir(env)
        self.addCleanup(shutil.rmtree, directory, True)
        self.assertFalse(stale.exists())
        owner = json.loads((Path(directory) / fusion_tmp.MARKER).read_text())
        self.assertEqual(owner["pid"], os.getpid())
        self.assertEqual(owner["start"], fusion_tmp.process_start(os.getpid()))

    def test_the_sweep_can_be_switched_off(self):
        stale = self.make("orc-switchof")
        with patch.object(fusion_tmp, "ROOT", self.root), patch.dict(os.environ, {"FUSION_TMP_SWEEP": "0"}):
            fusion_tmp.sweep_quietly()
        self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main()

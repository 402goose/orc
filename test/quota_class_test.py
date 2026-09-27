import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class QuotaClassTest(unittest.TestCase):
    def test_worker_prose_about_quota_is_not_a_quota_failure(self):
        result = {"status": "error", "provider_failure": None,
                  "summary": "Reviewed the quota routing; rate limit windows reset at resets_at.",
                  "blockers": ["Files changed during review; review a stable tree before publishing"]}
        self.assertFalse(core.quota_failure(result))
        self.assertEqual(core.failure_class(result), "worker_error")

    def test_provider_session_limit_is_a_quota_failure(self):
        result = {"status": "error", "provider_failure": "You've hit your session limit · resets 6:40pm",
                  "summary": "", "blockers": ["You've hit your session limit · resets 6:40pm"]}
        self.assertEqual(core.failure_class(result), "quota")

    def test_structural_rejection_is_a_quota_failure(self):
        result = {"status": "error", "provider_failure": "worker exited with code 1", "blockers": [],
                  "quota": {"status": "rejected", "windows": {"five_hour": {"used": 1.0, "resets_at": 1790551200}}}}
        self.assertEqual(core.failure_class(result), "quota")

    def test_legacy_results_without_provider_failure_keep_text_matching(self):
        self.assertTrue(core.quota_failure({"status": "error", "summary": "usage limit reached", "blockers": []}))


if __name__ == "__main__":
    unittest.main()

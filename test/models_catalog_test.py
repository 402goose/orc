import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_models as models


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ORC_HOME": str(self.root)})
        self.env.start()
        self.raw = json.dumps({"data": [{"id": "provider/new-model", "name": "New model", "supported_parameters": ["tools"]}]}).encode()
        (self.root / "quality.json").write_text(json.dumps({"source": "benchmark.example", "fetchedAt": "2026-08-28T13:32:43Z", "records": [{"name": "Old model"}]}))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_catalog_does_not_use_benchmark_as_model_availability(self):
        with patch("fusion_models.urllib.request.urlopen") as fetch:
            result = models.catalog(self.root)
        fetch.assert_not_called()
        self.assertEqual(result["catalog"]["models"], [])
        self.assertEqual(result["benchmark"]["record_count"], 1)
        self.assertEqual(result["benchmark"]["fetched_at"], "2026-08-28T13:32:43Z")

    def test_refresh_adds_unranked_model_without_inventing_account_fit(self):
        with patch("fusion_models.urllib.request.urlopen", return_value=io.BytesIO(self.raw)):
            result = models.refresh(self.root)
        self.assertTrue(result["refresh"]["ok"])
        self.assertEqual(result["catalog"]["models"][0]["id"], "provider/new-model")
        self.assertTrue(result["catalog"]["models"][0]["tools"])
        self.assertEqual(result["catalog"]["account_access"], "unchecked")
        self.assertEqual(result["benchmark"]["record_count"], 1)

    def test_invalid_or_failed_refresh_preserves_previous_catalog(self):
        (self.root / "models.json").write_bytes(self.raw)
        for raw in (b'{}', b'{"data":[]}', b'not JSON', b' ' * (models.MAX_BYTES + 1)):
            with patch("fusion_models.urllib.request.urlopen", return_value=io.BytesIO(raw)):
                result = models.refresh(self.root)
            self.assertFalse(result["refresh"]["ok"])
            self.assertEqual((self.root / "models.json").read_bytes(), self.raw)
        with patch("fusion_models.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertFalse(models.refresh(self.root)["refresh"]["ok"])

    def test_bad_or_duplicate_identifiers_cannot_replace_catalog(self):
        for rows in ([{"name": "no identity"}], [{"id": "x"}, {"id": "x"}]):
            with self.assertRaises(ValueError):
                models.parsed_models(json.dumps({"data": rows}).encode())


if __name__ == "__main__":
    unittest.main()

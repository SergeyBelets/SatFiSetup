"""Integrity/adoption boundary checks, using only temporary source copies."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from apply_patches import DEFAULT_SOURCE, ROOT, digest, patched_source


class PatchApplicationTests(unittest.TestCase):
    def test_exact_series_applies_without_modifying_vendor(self):
        original = DEFAULT_SOURCE.read_bytes()
        result = patched_source()
        manifest = json.loads((ROOT / "series.json").read_text())
        self.assertEqual(digest(result), manifest["patched_sha256"])
        self.assertEqual(DEFAULT_SOURCE.read_bytes(), original)
        self.assertNotEqual(result, original)

    def test_changed_source_is_rejected_before_application(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "verifier.rs"
            source.write_bytes(DEFAULT_SOURCE.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "Pinned upstream source hash mismatch"):
                patched_source(source)

    def test_cli_does_not_overwrite_an_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "preserve.rs"
            target.write_bytes(b"preserved")
            result = subprocess.run([sys.executable, str(ROOT / "apply_patches.py"),
                                     "--output", str(target)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), b"preserved")


if __name__ == "__main__":
    unittest.main()

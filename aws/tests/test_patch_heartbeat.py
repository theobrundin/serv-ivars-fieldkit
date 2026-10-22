import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "patch_heartbeat.py"
spec = importlib.util.spec_from_file_location("patch_heartbeat", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class HeartbeatPatchTests(unittest.TestCase):
    def test_exact_anchor_is_patched_and_compiles(self):
        source = "def handler():\n    health = {\n" + module.ANCHOR + "        }\n"
        patched = module.patch_source(source)
        compile(patched, "heartbeat.py", "exec")
        self.assertIn('"build_git_sha":', patched)
        self.assertIn('"dead_letter_depth":', patched)

    def test_exact_managed_patch_is_idempotent(self):
        source = "def handler():\n    health = {\n" + module.ANCHOR + "        }\n"
        patched = module.patch_source(source)
        self.assertEqual(module.patch_source(patched), patched)

    def test_unknown_or_partial_patch_fails_closed(self):
        with self.assertRaises(ValueError):
            module.patch_source("def handler():\n    pass\n")
        with self.assertRaises(ValueError):
            module.patch_source(module.ANCHOR + '"runtime_code_sha256": "x"\n')


if __name__ == "__main__":
    unittest.main()

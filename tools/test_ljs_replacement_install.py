#!/usr/bin/env python3
"""Local, unprivileged tests: never invoke installation or a remote device."""
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("replacement_guard", ROOT / "scripts/ljs_replacement_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
INSTALLER = ROOT / "scripts/install_ljs_replacement.sh"


def make_tar(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, kind, data in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.mode = 0o6755
            if kind == tarfile.REGTYPE:
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            else:
                entry.linkname = data.decode() if data else ""
                archive.addfile(entry)
    return output.getvalue()


class ReplacementGuardTests(unittest.TestCase):
    def setUp(self):
        # resolve handles macOS's /var -> /private/var symlink outside input.
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def bench(self, **changes):
        config = dict(device_id="serv-unit-bench", location_id="bench-servcamog",
                      api_key="a" * 64, api_base=guard.API_BASE,
                      alpr={"engine": "pr_stream"})
        config.update(changes)
        return json.dumps(config).encode()

    def test_only_bench_identity_allowed(self):
        self.assertEqual(guard.validate_bench(self.bench())["device_id"], "serv-unit-bench")
        for key, value in (("device_id", "serv-unit-001"), ("location_id", "ljs-louisville-001")):
            with self.subTest(key=key), self.assertRaises(guard.InvalidInput):
                guard.validate_bench(self.bench(**{key: value}))

    def test_engine_and_key_required(self):
        for changes in ({"alpr": {"engine": "ultimatealpr"}}, {"api_key": "placeholder"}, {"api_base": "http://example.invalid"}):
            with self.subTest(changes=changes), self.assertRaises(guard.InvalidInput):
                guard.validate_bench(self.bench(**changes))

    def test_runtime_hold_rejects_production_config(self):
        config = self.base / "config.json"
        marker = self.base / "hold"
        config.write_bytes(self.bench())
        config.chmod(0o600)
        marker.write_text("bench_only")
        marker.chmod(0o000)  # only existence, never privileged record contents
        guard.service_check(config, marker)
        config.write_bytes(self.bench(device_id="serv-unit-001", location_id="ljs-louisville-001"))
        with self.assertRaises(guard.InvalidInput):
            guard.service_check(config, marker)
        marker.unlink()
        guard.service_check(config, marker)

    def test_runtime_hold_rejects_missing_or_changed_endpoint(self):
        marker = self.base / "hold"
        marker.touch()
        with self.assertRaises(OSError):
            guard.service_check(self.base / "missing", marker)
        config = self.base / "config.json"
        config.write_bytes(self.bench(api_base="https://wrong.invalid"))
        config.chmod(0o600)
        with self.assertRaises(guard.InvalidInput):
            guard.service_check(config, marker)

    def test_malformed_config_is_generic(self):
        for data in (b"secret not json", b"[]", b"null"):
            with self.assertRaises(guard.InvalidInput) as result:
                guard.validate_bench(data)
            self.assertNotIn("secret", str(result.exception))

    def test_private_read_and_permissions(self):
        target = self.base / "key"
        target.write_bytes(b"private")
        target.chmod(0o600)
        self.assertEqual(guard.read_regular(target, private=True), b"private")
        target.chmod(0o644)
        with self.assertRaises(guard.InvalidInput):
            guard.read_regular(target, private=True)

    def test_symlink_input_rejected(self):
        target = self.base / "real"
        target.write_bytes(b"private")
        link = self.base / "link"
        link.symlink_to(target)
        with self.assertRaises(guard.InvalidInput):
            guard.read_regular(link)

    def test_symlink_ancestor_rejected(self):
        real = self.base / "real"
        real.mkdir()
        (real / "key").write_bytes(b"private")
        (self.base / "alias").symlink_to(real)
        with self.assertRaises(guard.InvalidInput):
            guard.read_regular(self.base / "alias/key")

    def test_fifo_rejected_without_blocking(self):
        path = self.base / "fifo"
        os.mkfifo(path)
        with self.assertRaises(guard.InvalidInput):
            guard.read_regular(path)

    def test_input_size_limited(self):
        path = self.base / "big"
        path.write_bytes(b"12345")
        with self.assertRaises(guard.InvalidInput):
            guard.read_regular(path, max_size=4)

    def test_stream_env_is_data_and_owner_checked(self):
        path = self.base / "pr.env"
        path.write_text("TOKEN=test-token\nLICENSE_KEY=test-license\n")
        path.chmod(0o600)
        guard.validate_pr_env(path, os.getuid())
        with self.assertRaises(guard.InvalidInput):
            guard.validate_pr_env(path, os.getuid() + 1)
        for content in ("TOKEN=x\n", "TOKEN=x\nTOKEN=y\nLICENSE_KEY=z\n",
                        "TOKEN=x\nLICENSE_KEY=  spaced\n", "export TOKEN=x\nLICENSE_KEY=z\n",
                        "TOKEN=x\nLICENSE_KEY=z\nEVIL=bad\n"):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(guard.InvalidInput):
                guard.validate_pr_env(path, os.getuid())

    def test_extract_regular_only_and_strip_privileged_modes(self):
        target = self.base / "extract"
        guard.extract_regular(make_tar([("a", tarfile.DIRTYPE, b""), ("a/code", tarfile.REGTYPE, b"x")]), target)
        self.assertEqual((target / "a/code").read_bytes(), b"x")
        self.assertEqual(stat.S_IMODE((target / "a/code").stat().st_mode), 0o755)

    def test_archive_traversal_rejected_before_any_extraction(self):
        for bad in ("../../escape", "/absolute", "a/../escape"):
            target = self.base / "extract"
            with self.subTest(bad=bad), self.assertRaises(guard.InvalidInput):
                guard.extract_regular(make_tar([("safe", tarfile.REGTYPE, b"x"), (bad, tarfile.REGTYPE, b"x")]), target)
            self.assertFalse(target.exists())

    def test_archive_links_and_special_files_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind), self.assertRaises(guard.InvalidInput):
                guard.archive_members(make_tar([("bad", kind, b"/etc/shadow")]))

    def test_duplicate_member_rejected(self):
        with self.assertRaises(guard.InvalidInput):
            guard.archive_members(make_tar([("a", tarfile.REGTYPE, b"x"), ("a", tarfile.REGTYPE, b"y")]))

    def test_regular_file_cannot_be_parent(self):
        with self.assertRaises(guard.InvalidInput):
            guard.archive_members(make_tar([("a", tarfile.REGTYPE, b"x"), ("a/b", tarfile.REGTYPE, b"y")]))

    def test_extract_refuses_nonempty_destination(self):
        target = self.base / "extract"
        target.mkdir()
        (target / "keep").write_bytes(b"old")
        with self.assertRaises(guard.InvalidInput):
            guard.extract_regular(make_tar([("a", tarfile.REGTYPE, b"x")]), target)
        self.assertEqual((target / "keep").read_bytes(), b"old")

    def test_private_output_no_overwrite(self):
        target = self.base / "config"
        guard.write_private(target, b"new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            guard.write_private(target, b"overwrite")

    def test_shell_syntax_and_help_do_not_install(self):
        subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
        result = subprocess.run(["bash", str(INSTALLER), "--help"], capture_output=True, text=True, check=True)
        self.assertIn("ALREADY be approved", result.stdout)
        self.assertIn("--start-bench", result.stdout)

    def test_missing_reviewed_sha_fails_before_root_or_mutation(self):
        result = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact reviewed", result.stderr)

    def test_installer_security_order_and_boundaries(self):
        source = INSTALLER.read_text()
        self.assertLess(source.index("human approval file is missing"), source.index("mktemp -d"))
        self.assertLess(source.index("bench config differs from the reviewed builder"), source.index("MUTATING=1"))
        self.assertLess(source.index("stop_existing plate-reader.service"), source.index('mv /home/serv/plate_reader'))
        self.assertLess(source.index('mv /home/serv/plate_reader'), source.index('SERV_INSTALL_START_NETWORK=0 bash'))
        self.assertNotIn('docker rm', source)
        self.assertNotIn('docker pull', source)
        self.assertNotIn('hostnamectl', source)
        self.assertNotIn('tailscale up', source)
        self.assertNotIn('rm -rf', source)
        self.assertNotIn('> "$APPROVAL"', source)
        self.assertIn('production_activation_allowed=False', source)
        self.assertIn('ExecStartPre=/usr/bin/python3 /usr/local/libexec/serv-replacement-guard.py service-check', source)
        self.assertLess(source.index('production_activation_allowed=False'), source.index('systemctl start plate-reader.service'))
        self.assertIn('gitc archive --format=tar "$SHA"', source)
        self.assertIn('gitc show "$SHA:scripts/ljs_replacement_guard.py"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

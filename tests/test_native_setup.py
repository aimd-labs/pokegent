"""Synthetic command fixtures: no downloads, installs or emulator execution."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools/setup_mgba_env.sh"
OS_RELEASE = Path("/etc/os-release")
SUPPORTED = OS_RELEASE.exists() and all(
    value in OS_RELEASE.read_text() for value in ('ID=ubuntu', 'VERSION_ID="24.04"')
)
HOST_LIBS = (
    "libedit.so.2 libGL.so.1 libavcodec.so.60 libavfilter.so.9 libavformat.so.60 "
    "libavutil.so.58 libswscale.so.7 libswresample.so.4 libz.so.1 libpng16.so.16 "
    "libsqlite3.so.0 libelf.so.1 libm.so.6 libc.so.6 libbz2.so.1.0 libssl.so.3 "
    "libcrypto.so.3 libstdc++.so.6"
).split()


@unittest.skipUnless(SUPPORTED, "native helper targets Ubuntu 24.04")
class NativeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        self.stub("uname", "echo x86_64")
        self.stub("ldconfig", "printf '%s (libc6) => /host/%s\\n' " + " ".join(
            f"'{name}' '{name}'" for name in HOST_LIBS
        ))
        self.stub("curl", f"echo download >> '{self.root}/calls'; exit 88")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o700)

    def run_setup(self, target=None):
        return subprocess.run(
            ["bash", str(SCRIPT), str(target or self.root / "native")],
            env=self.env, capture_output=True, text=True, check=False,
        )

    def test_unsupported_machine_stops_before_download(self):
        self.stub("uname", "echo aarch64")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supported platform", result.stderr)
        self.assertFalse((self.root / "calls").exists())

    def test_missing_host_dependency_stops_before_writes(self):
        self.stub("ldconfig", "echo 'libc.so.6 (libc6) => /host/libc.so.6'")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing host library", result.stderr)
        self.assertFalse((self.root / "native").exists())

    def test_existing_and_checkout_paths_are_preserved(self):
        sentinel = self.root / "preserve"
        sentinel.write_text("keep")
        for target in (sentinel, SCRIPT.parent.parent / "native-forbidden", self.root / "bad:path"):
            with self.subTest(target=target):
                self.assertNotEqual(self.run_setup(target).returncode, 0)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((self.root / "calls").exists())

    def test_checksum_failure_prevents_extraction_and_export(self):
        self.stub("curl", "exit 0")
        self.stub("sha256sum", "cat >/dev/null; exit 1")
        self.stub("dpkg-deb", f"touch '{self.root}/extracted'")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "extracted").exists())
        self.assertEqual(result.stdout, "")

    def test_normalized_existing_aliases_preserve_bytes_and_mtimes(self):
        for component in ("missing", "file"):
            with self.subTest(component=component):
                prefix = self.root / component
                (prefix / "debs").mkdir(parents=True)
                sentinel = prefix / "sentinel"
                sentinel.write_bytes(b"keep sentinel")
                deb = prefix / "debs/libmgba0.10t64_0.10.2+dfsg-1.1build3_amd64.deb"
                deb.write_bytes(b"keep existing archive")
                if component == "file":
                    (prefix / component).write_bytes(b"regular file, not directory")
                for path in (sentinel, deb, prefix):
                    os.utime(path, ns=(1_600_000_000_000_000_000,) * 2)
                calls = self.root / f"calls-{component}"
                self.stub("curl", f'''
while [[ $1 != --output ]]; do shift; done
printf overwritten > "$2"
echo download >> '{calls}'
exit 88
''')
                before = [(path.read_bytes(), path.stat().st_mtime_ns)
                          for path in (sentinel, deb)]
                directory_mtime = prefix.stat().st_mtime_ns
                result = self.run_setup(prefix / component / "..")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    ([(path.read_bytes(), path.stat().st_mtime_ns)
                      for path in (sentinel, deb)], prefix.stat().st_mtime_ns, calls.exists()),
                    (before, directory_mtime, False),
                )

    def test_success_exports_only_four_libraries(self):
        self.stub("curl", f"echo \"${{@: -1}}\" >> '{self.root}/calls'")
        self.stub("sha256sum", "cat >/dev/null")
        self.stub("dpkg-deb", '''
mkdir -p "$3/usr/lib/x86_64-linux-gnu"
for name in libmgba.so.0.10 libzip.so.4 liblua5.4.so.0 libinih.so.1 libc.so.6; do
    touch "$3/usr/lib/x86_64-linux-gnu/$name"
done
''')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            {path.name for path in (self.root / "native/lib").iterdir()},
            {"libmgba.so.0.10", "libzip.so.4", "liblua5.4.so.0", "libinih.so.1"},
        )
        urls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(len(urls), 4)
        self.assertTrue(all(url.startswith("https://archive.ubuntu.com/ubuntu/pool/")
                            for url in urls))
        self.assertEqual(result.stdout, f"export LD_LIBRARY_PATH={self.root}/native/lib\n")


if __name__ == "__main__":
    unittest.main()

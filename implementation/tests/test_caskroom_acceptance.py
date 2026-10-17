from __future__ import annotations

import contextlib
import io
import os
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import acceptance_caskroom


class CaskroomAcceptanceTests(unittest.TestCase):
    def test_local_run_is_rejected_before_any_brew_or_server_work(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCLEAN_REAL_ENV_ACCEPTANCE": "1"}, clear=True), \
             mock.patch("subprocess.run", side_effect=AssertionError("must not run brew")), \
             mock.patch("http.server.ThreadingHTTPServer", side_effect=AssertionError("must not listen")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(AssertionError, "disposable-runner-only"):
                acceptance_caskroom.run()

    def test_archives_have_manpage_and_controlled_install_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version, broken in (("1.0", False), ("3.0", True)):
                with self.subTest(version=version):
                    archive = acceptance_caskroom.make_archive(root, "fixture", version, broken=broken)
                    with tarfile.open(archive) as stream:
                        names = stream.getnames()
                        self.assertIn("share/man/man1/fixture.1", names)
                        self.assertEqual("bin/fixture" in names, not broken)
                        binary = "bin/missing-install-target" if broken else "bin/fixture"
                        self.assertTrue(stream.getmember(binary).mode & 0o111)


if __name__ == "__main__":
    unittest.main()

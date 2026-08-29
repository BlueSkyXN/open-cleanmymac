from __future__ import annotations

import stat
import tarfile
import unittest
import zipfile

from scripts.check_release_artifacts import (
    _assert_metadata,
    _assert_safe_content,
    _assert_safe_member,
    _assert_tar_member,
    _assert_zip_member,
)


class ReleaseArtifactGuardTests(unittest.TestCase):
    def test_rejects_sensitive_archive_member_names(self) -> None:
        for name in (
            "package/.env.local",
            "package/private.pem",
            "package/signing-private.pem",
            "package/key.p12",
            "package/key.pfx",
            "package/id_ed25519.pub",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "敏感文件名"
            ):
                _assert_safe_member(name)

    def test_rejects_private_key_and_token_content(self) -> None:
        samples = (
            b"-----BEGIN " + b"PRIVATE KEY-----\nnot-a-real-key",
            b"github_pat_" + b"A" * 30,
            b"AKIA" + b"A" * 16,
        )
        for content in samples:
            with self.subTest(content=content[:16]), self.assertRaises(
                ValueError
            ):
                _assert_safe_content("package/data.txt", content)

    def test_rejects_mismatched_package_metadata(self) -> None:
        valid = (
            b"Name: open-cleanmymac\n"
            b"Version: 0.23.0\n"
            b"Requires-Python: >=3.11\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        _assert_metadata(valid, source="test")

        with self.assertRaisesRegex(ValueError, "Version"):
            _assert_metadata(
                valid.replace(b"0.23.0", b"9.9.9"),
                source="test",
            )

        with self.assertRaisesRegex(ValueError, "License-Expression"):
            _assert_metadata(
                valid.replace(b"GPL-3.0-only", b"MIT"),
                source="test",
            )

        with self.assertRaisesRegex(ValueError, "License-File"):
            _assert_metadata(
                valid.replace(b"License-File: LICENSE\n", b""),
                source="test",
            )

    def test_rejects_archive_link_members(self) -> None:
        zip_link = zipfile.ZipInfo("package/link")
        zip_link.create_system = 3
        zip_link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "wheel 不允许 symlink"):
            _assert_zip_member(zip_link)

        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            member = tarfile.TarInfo("package/link")
            member.type = link_type
            with self.subTest(link_type=link_type), self.assertRaisesRegex(
                ValueError, "sdist 不允许 symlink/hardlink"
            ):
                _assert_tar_member(member)


if __name__ == "__main__":
    unittest.main()

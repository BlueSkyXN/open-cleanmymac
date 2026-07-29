from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.engine import scan_domains
from openclean.macos import (
    TrashDiscovery,
    discover_trash_paths,
    nonprivileged_action_block_reason,
)


class MacOSTrashTests(unittest.TestCase):
    def test_nonprivileged_action_boundary_protects_system_and_mount_roots(self) -> None:
        home = Path("/Users/example")
        self.assertIn(
            "系统保护路径",
            nonprivileged_action_block_reason(Path("/System"), home=home),
        )
        self.assertIn(
            "用户目录",
            nonprivileged_action_block_reason(home, home=home),
        )
        self.assertEqual(
            nonprivileged_action_block_reason(
                home / "Projects" / "app", home=home
            ),
            "",
        )
        self.assertEqual(
            nonprivileged_action_block_reason(
                Path("/Volumes/External/Projects/app"), home=home
            ),
            "",
        )

    def test_discovers_home_and_each_mounted_volume_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            volumes = root / "Volumes"
            external = volumes / "External SSD"
            home.mkdir()
            external.mkdir(parents=True)
            (volumes / "not-a-volume.txt").write_text("x", encoding="utf-8")

            discovery = discover_trash_paths(
                home=home, volumes_root=volumes, uid=501
            )

            self.assertEqual(
                set(discovery.paths),
                {
                    home / ".Trash",
                    external / ".Trashes" / "501",
                },
            )
            self.assertEqual(discovery.issues, ())

    def test_discovery_reports_volume_enumeration_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            with mock.patch(
                "openclean.macos.os.scandir",
                side_effect=PermissionError("denied"),
            ):
                discovery = discover_trash_paths(
                    home=home, volumes_root=Path(tmp) / "Volumes", uid=501
                )

            self.assertEqual(discovery.paths, (home / ".Trash",))
            self.assertEqual(len(discovery.issues), 1)
            self.assertEqual(discovery.issues[0].code, "permission_denied")

    def test_trash_domain_scans_dynamic_paths_as_confirm_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_trash = root / "home-trash"
            volume_trash = root / "volume-trash"
            home_trash.mkdir()
            volume_trash.mkdir()
            home_file = home_trash / "one.bin"
            volume_file = volume_trash / "two.bin"
            home_file.write_bytes(b"home")
            volume_file.write_bytes(b"volume")
            discovery = TrashDiscovery(paths=(home_trash, volume_trash))

            with mock.patch(
                "openclean.engine.discover_trash_paths",
                return_value=discovery,
            ):
                result = scan_domains(["trash"], workers=1)

            self.assertEqual(len(result.items), 2)
            self.assertEqual(
                result.total,
                (home_file.stat().st_blocks + volume_file.stat().st_blocks)
                * 512,
            )
            self.assertEqual({item.domain for item in result.items}, {"trash"})
            self.assertTrue(all(item.safety == "confirm" for item in result.items))
            self.assertTrue(all(item.preselected is False for item in result.items))


if __name__ == "__main__":
    unittest.main()

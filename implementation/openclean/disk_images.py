"""磁盘镜像候选的只读格式与挂载证据，不挂载、解包或推断闲置。"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

IMAGE_SUFFIXES = frozenset({".dmg", ".img", ".iso", ".sparseimage"})


def attached_images(*, runner=None) -> list[object] | None:
    run = runner or subprocess.run
    try:
        completed = run(
            ["/usr/bin/hdiutil", "info", "-plist"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        if completed.returncode:
            return None
        payload = plistlib.loads(completed.stdout)
        return (
            payload["images"]
            if isinstance(payload, dict) and isinstance(payload.get("images"), list)
            else None
        )
    except (
        OSError,
        ValueError,
        TypeError,
        plistlib.InvalidFileException,
        subprocess.TimeoutExpired,
    ):
        return None


def image_mount_state(path: Path, images: list[object] | None) -> str:
    if images is None:
        return "unknown"
    if not images:
        return "not_mounted"

    def contains_path(value):
        if isinstance(value, str):
            return value.startswith("/") and os.path.normpath(value) == str(path)
        if isinstance(value, dict):
            return any(contains_path(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_path(v) for v in value)
        return False

    # 不依赖未核实的版本专有键名；非空清单未精确匹配时保留未知。
    return "mounted" if any(contains_path(image) for image in images) else "unknown"


def image_format_description(descriptor: int, *, runner=None) -> str | None:
    run = runner or subprocess.run
    try:
        completed = run(
            ["/usr/bin/file", "-b", "-L", f"/dev/fd/{descriptor}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            pass_fds=(descriptor,),
        )
        if completed.returncode == 0:
            return completed.stdout.strip()[:1024] or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None

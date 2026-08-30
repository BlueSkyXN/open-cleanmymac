"""公开路径约定对应的应用进程保护规则。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import normalize_path


@dataclass(frozen=True)
class ApplicationPathRule:
    relative_path: str
    process_markers: tuple[str, ...]


# 这些规则只表达公开可见的 macOS 缓存路径与应用进程归属，不包含第三方私有规则。
APPLICATION_PATH_RULES: tuple[ApplicationPathRule, ...] = (
    ApplicationPathRule(
        "Library/Caches/com.openai.codex",
        ("ChatGPT.app", "Codex"),
    ),
    ApplicationPathRule(
        "Library/Caches/Codex",
        ("ChatGPT.app", "Codex"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.openai.sky.CUAService",
        ("ChatGPT.app", "Codex Computer Use"),
    ),
    ApplicationPathRule(
        "Library/Caches/Google",
        ("Google Chrome.app",),
    ),
    ApplicationPathRule(
        "Library/Caches/LarkShell",
        ("Lark.app", "Feishu", "Lark Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.electron.lark.helper",
        ("Lark.app", "Feishu", "Lark Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.workbuddy.workbuddy.BundleMigration",
        ("WorkBuddy.app", "WorkBuddy Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/TRAE SOLO CN",
        ("TRAE SOLO CN.app", "TRAE SOLO CN Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/cn.trae.solo.app",
        ("TRAE SOLO CN.app", "TRAE SOLO CN Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.aliyun.lingma.ide.ShipIt",
        ("Qoder CN IDE.app", "ShipIt"),
    ),
    ApplicationPathRule(
        "Library/Caches/QoderCN",
        ("Qoder CN IDE.app", "Qoder CN Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.qodercn.app",
        ("Qoder CN.app",),
    ),
    ApplicationPathRule(
        "Library/Caches/com.qodercn.app.ShipIt",
        ("Qoder CN.app", "ShipIt"),
    ),
    ApplicationPathRule(
        "Library/Caches/qoder-cn-updater",
        ("Qoder CN.app", "qoder-cn-updater"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.microsoft.VSCode.ShipIt",
        ("Visual Studio Code.app", "Code Helper"),
    ),
    ApplicationPathRule(
        "Library/Caches/copilot",
        ("copilot",),
    ),
    ApplicationPathRule(
        "Library/Caches/ms-playwright",
        ("playwright", "headless_shell"),
    ),
)


def _same_or_descendant(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def process_markers_for_path(
    path: str | os.PathLike[str],
    *,
    home: Path | None = None,
    rules: tuple[ApplicationPathRule, ...] = APPLICATION_PATH_RULES,
) -> tuple[str, ...]:
    """返回候选路径所属应用的保守进程标记。"""
    candidate = normalize_path(path)
    base = normalize_path(home or Path.home())
    markers: list[str] = []
    for rule in rules:
        root = normalize_path(base / rule.relative_path)
        if not _same_or_descendant(candidate, root):
            continue
        for marker in rule.process_markers:
            if marker not in markers:
                markers.append(marker)
    return tuple(markers)

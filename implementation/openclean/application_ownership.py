"""公开路径约定对应的应用进程保护规则。"""
from __future__ import annotations

import os
import plistlib
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .macos import scan_symlink_anchor
from .models import FileFacts, normalize_path


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
        "Library/Caches/com.tencent.workbuddy.mac.BundleMigration",
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
    ApplicationPathRule(
        "Library/Caches/com.netease.uuremote",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.netease.uuremote.server",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
    ApplicationPathRule(
        "Library/Caches/com.netease.uuremote.updater",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
)


# ``DARWIN_USER_CACHE_DIR`` 的一级子项以公开 bundle/helper 名称归属应用。
# 只在调用方明确提供该动态根时匹配，避免把任意同名目录误判成应用缓存。
DARWIN_CACHE_PROCESS_MARKERS: dict[str, tuple[str, ...]] = {
    "com.openai.codex": ("ChatGPT.app", "Codex"),
    "com.openai.codex.helper": ("ChatGPT.app", "Codex"),
    "com.openai.sky.CUAService": ("ChatGPT.app", "Codex Computer Use"),
    "com.google.Chrome.helper": ("Google Chrome.app",),
    "com.electron.lark.helper": ("Lark.app", "Feishu", "Lark Helper"),
    "com.electron.lark.iron": ("Lark.app", "Feishu", "Lark Helper"),
    "com.workbuddy.workbuddy": ("WorkBuddy.app", "WorkBuddy Helper"),
    "com.workbuddy.workbuddy.helper.GPU": (
        "WorkBuddy.app",
        "WorkBuddy Helper",
    ),
    "cn.trae.solo.app.helper": (
        "TRAE SOLO CN.app",
        "TRAE SOLO CN Helper",
    ),
    "com.aliyun.lingma.ide": ("Qoder CN IDE.app", "Qoder CN IDE Helper"),
    "com.aliyun.lingma.ide.helper": (
        "Qoder CN IDE.app",
        "Qoder CN IDE Helper",
    ),
    "com.qodercn.app.helper": ("Qoder CN.app", "Qoder CN Helper"),
    "com.qoder.work.cn": ("QoderWork CN.app", "QoderWork CN Helper"),
    "com.qoder.work.cn.helper.GPU": (
        "QoderWork CN.app",
        "QoderWork CN Helper",
    ),
    "com.microsoft.VSCode": ("Visual Studio Code.app", "Code Helper"),
    "com.microsoft.VSCode.helper": ("Visual Studio Code.app", "Code Helper"),
    "com.todesktop.230313mzl4w4u92.helper": (
        "Cursor.app",
        "Cursor Helper",
        "cursor-agent",
    ),
    "com.github.GitHubClient.helper": (
        "GitHub Desktop.app",
        "GitHub Desktop Helper",
    ),
    "com.netease.uuremote": (
        "UURemote.app",
        "UURemoteService",
        "UURemoteServer",
    ),
    "com.netease.uuremote.server": (
        "UURemote.app",
        "UURemoteService",
        "UURemoteServer",
    ),
}


def _same_or_descendant(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def process_markers_for_path(
    path: str | os.PathLike[str],
    *,
    home: Path | None = None,
    darwin_cache_root: Path | None = None,
    rules: tuple[ApplicationPathRule, ...] = APPLICATION_PATH_RULES,
) -> tuple[str, ...]:
    """返回候选路径所属应用的保守进程标记。"""
    candidate = normalize_path(path)
    base = normalize_path(home or Path.home())
    markers: list[str] = []
    if darwin_cache_root is not None:
        dynamic_root = normalize_path(darwin_cache_root)
        if candidate.parent == dynamic_root:
            markers.extend(DARWIN_CACHE_PROCESS_MARKERS.get(candidate.name, ()))
    for rule in rules:
        root = normalize_path(base / rule.relative_path)
        if not _same_or_descendant(candidate, root):
            continue
        for marker in rule.process_markers:
            if marker not in markers:
                markers.append(marker)
    return tuple(markers)


@dataclass(frozen=True)
class ApplicationResolution:
    status: str
    process_markers: tuple[str, ...] = ()

    @property
    def note(self) -> str:
        return {
            "resolved": "已通过应用元数据补充运行进程保护",
            "multiple": "同一 bundle ID 对应多个安装位置，合并运行进程保护",
            "not_found": "未确认应用归属；不表示应用已卸载或缓存可安全删除",
            "unavailable": "应用归属查询未完成；沿用原有保护，不据此判断残留",
            "limit_reached": "应用归属查询达到本次预算；沿用原有保护",
        }.get(self.status, "")


class ApplicationResolver:
    """单次扫描内有界解析精确 bundle ID；不推断 helper 后缀或卸载状态。"""

    def __init__(
        self,
        *,
        home: Path | None = None,
        application_roots: tuple[Path, ...] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        max_queries: int = 8,
        time_budget: float = 4.0,
    ) -> None:
        self.home = normalize_path(home or Path.home())
        self.roots = application_roots if application_roots is not None else (
            Path("/Applications"), Path("/System/Applications"), self.home / "Applications",
        )
        self.runner = runner or subprocess.run
        self.max_queries = max_queries
        self.time_budget = time_budget
        self._spent = 0.0
        self._cache: dict[str, ApplicationResolution] = {}
        self._apps: dict[str, list[Path]] | None = None
        self._inventory_complete = True
        self._lock = threading.Lock()

    def _safe_directory(self, path: Path) -> bool:
        anchor = scan_symlink_anchor(path, home=self.home)
        current = anchor
        # 先检查每级目录再访问其子项，避免穿过 dataless 应用/祖先触发取回。
        for part in ("", *path.relative_to(anchor).parts):
            if part:
                current /= part
            facts = FileFacts(current, current.lstat())
            if not stat.S_ISDIR(facts.stat.st_mode) or facts.is_probable_cloud_placeholder:
                return False
        return True

    def _bundle_id(self, app: Path) -> str | None:
        info = app / "Contents/Info.plist"
        try:
            if not app.is_absolute() or app.suffix != ".app":
                return None
            if not self._safe_directory(app / "Contents"):
                return None
            facts = FileFacts(info, info.lstat())
            if (not stat.S_ISREG(facts.stat.st_mode)
                    or facts.is_probable_cloud_placeholder or facts.stat.st_size > 131072):
                return None
            with info.open("rb") as stream:
                data = stream.read(131073)
            if len(data) > 131072:
                return None
            payload = plistlib.loads(data)
            value = payload.get("CFBundleIdentifier") if isinstance(payload, dict) else None
            return value if isinstance(value, str) else None
        except (OSError, ValueError, TypeError, RecursionError, plistlib.InvalidFileException):
            return None

    def _inventory(self, deadline: float) -> dict[str, list[Path]]:
        if self._apps is not None:
            return self._apps
        self._apps = {}
        remaining = 512
        for root in self.roots:
            try:
                if not self._safe_directory(root):
                    self._inventory_complete = False
                    continue
                with os.scandir(root) as entries:
                    for entry in entries:
                        if remaining <= 0 or time.monotonic() >= deadline:
                            self._inventory_complete = False
                            return self._apps
                        remaining -= 1
                        app = Path(entry.path)
                        if app.suffix != ".app":
                            continue
                        bundle_id = self._bundle_id(app)
                        if bundle_id:
                            self._apps.setdefault(bundle_id, []).append(app)
                        else:
                            self._inventory_complete = False
            except FileNotFoundError:
                continue
            except OSError:
                self._inventory_complete = False
        return self._apps

    def resolve(self, path: Path, *, darwin_cache_root: Path | None = None) -> ApplicationResolution:
        candidate = normalize_path(path)
        roots = (self.home / "Library/Caches",)
        if darwin_cache_root is not None:
            roots += (normalize_path(darwin_cache_root),)
        if candidate.parent not in roots or not re.fullmatch(
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", candidate.name, re.ASCII
        ) or len(candidate.name) > 255:
            return ApplicationResolution("not_applicable")
        bundle_id = candidate.name
        with self._lock:
            if bundle_id in self._cache:
                return self._cache[bundle_id]
            if len(self._cache) >= self.max_queries or self._spent >= self.time_budget:
                return ApplicationResolution("limit_reached")
            started = time.monotonic()
            deadline = started + self.time_budget - self._spent
            complete = True
            matches: set[Path] = set()
            try:
                result = self.runner(
                    ["/usr/bin/mdfind", "-0", f'kMDItemCFBundleIdentifier == "{bundle_id}"'],
                    capture_output=True, text=True, check=False,
                    timeout=min(0.5, self.time_budget - self._spent),
                )
                if result.returncode != 0 or len(result.stdout) > 65536:
                    complete = False
                else:
                    paths = [p for p in result.stdout.split("\0") if p]
                    if len(paths) > 32:
                        complete = False
                    for raw_path in paths[:32]:
                        if time.monotonic() >= deadline:
                            complete = False
                            break
                        app = Path(raw_path)
                        if self._bundle_id(app) == bundle_id:
                            matches.add(app)
                        else:
                            complete = False
            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                complete = False
            matches.update(self._inventory(deadline).get(bundle_id, ()))
            self._spent += time.monotonic() - started
            # 完整 app 路径覆盖其嵌套 helper，避免按短进程名误归属其它应用。
            markers = tuple(str(app / "Contents") + "/" for app in sorted(matches))
            status = ("multiple" if len(markers) > 1 else "resolved") if markers else (
                "not_found" if complete and self._inventory_complete else "unavailable"
            )
            resolution = ApplicationResolution(status, markers)
            self._cache[bundle_id] = resolution
            return resolution

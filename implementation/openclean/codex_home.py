"""Codex HOME 一级条目的只读用途识别；额外材料不等于垃圾。"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import tomllib
from collections.abc import Callable
from pathlib import Path

from .models import FileFacts, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate
from .resource_walk import WalkBudget, open_resource, walk_resources

_TASK = "Codex 用户目录材料审阅"
_MAX_ENTRIES = 512
_MAX_CONFIG_FILES = 32
_MAX_CONFIG_BYTES = 1024 * 1024
_SMALL_FILE_BYTES = 1024 * 1024

# 已观察的 CLI/app 存储名称只用来排除核心数据，不能作为官方布局的封闭 schema。
_KNOWN_NAMES = frozenset({
    "AGENTS.md", "AGENTS.override.md", "config.toml", "managed_config.toml",
    "requirements.toml", "auth.json", ".credentials.json", ".env", "hooks.json",
    "rules", "skills", "agents", "prompts", "sessions", "archived_sessions",
    "history.jsonl", "session_index.jsonl", "models_cache.json", "version.json",
    "installation_id", ".sandbox_migration", ".sqlite-maintenance.lock",
    ".codex-global-state.json", ".codex-global-state.json.bak",
    "cache", "plugins", "packages", "worktrees", "memories", "memories_v2",
    "attachments", "generated_images", "visualizations", "visualization-viewers",
    "plans", "automations", "log", "sqlite", "state", "db-backups", ".tmp", "tmp",
    "app-server-control", "app-server-daemon", "ipc", "thread-writer-locks",
    "mcp-oauth-locks", "project-metadata-locks", "rollout-migrations",
    "rollback-checkpoints", "tui-thread-reference-capabilities",
    "external_agent_session_imports.json", "chrome-native-hosts-v2.json",
    "browser", "computer-use", "process_manager", "node_repl", "editor", "shell_snapshots",
    "dictation-history", "transcription-history.jsonl", "pets", "avatars",
    "code-review-plugin", "vendor_imports", "cloud-config-bundle-cache.json",
})
_DATABASE_NAME = re.compile(
    r"(?:state|logs|goals|queue|thread_history|memories(?:_v2)?)_\d+"
    r"\.sqlite(?:-(?:wal|shm|journal))?\Z"
)
_MATERIAL_DIRS = frozenset({"docs", "suites", "backups", "archives", "local", "scripts"})


def _config_references(facts: FileFacts, parent_fd: int) -> set[Path]:
    if not stat.S_ISREG(facts.stat.st_mode) or facts.logical_size > _MAX_CONFIG_BYTES:
        raise ValueError("配置引用检查要求有界普通文件")
    with open_resource(facts, parent_fd) as descriptor:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            contents = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(contents) > _MAX_CONFIG_BYTES:
            raise ValueError("配置超过读取上限")
        config = tomllib.loads(contents.decode("utf-8"))
    references = set()
    for key in ("model_catalog_json", "model_instructions_file"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(os.path.expanduser(value))
        references.add(normalize_path(path if path.is_absolute() else facts.path.parent / path))
    return references


def _classification(facts: FileFacts, references: set[Path]) -> tuple[str, str] | None:
    name = facts.path.name
    if name in _KNOWN_NAMES or _DATABASE_NAME.fullmatch(name):
        return None
    if any(path == facts.path or facts.path in path.parents for path in references):
        return "configured_reference", "配置/profile 的模型目录或指令路径引用；应保留"
    if fnmatch.fnmatchcase(name, "*.config.toml"):
        return "supported_profile", "Codex 支持的命名 profile；不是任意杂项"
    if name == ".DS_Store" and stat.S_ISREG(facts.stat.st_mode):
        return "finder_metadata", "Finder 视图元数据，不是 Codex 会话或配置"
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in (
        "*-example.*", "*.example.*", "*.bak", "*.bak-*", "*.backup-*",
    )):
        return "backup_or_example", "备份/示例名称线索；未比较正文，不保证重复或失效"
    if stat.S_ISDIR(facts.stat.st_mode) and name in _MATERIAL_DIRS:
        return "supplemental_directory", "额外资料目录名称线索；未递归，不推断内容可删除"
    if stat.S_ISREG(facts.stat.st_mode) and facts.path.suffix.lower() in {".md", ".txt", ".csv"}:
        return "supplemental_document", "额外文本材料；可能仍被脚本或模型目录内嵌采用"
    return "unclassified", "不在当前已知名称集合；可能是用户扩展或新版本数据，归属待确认"


def scan_codex_home(
    protection: Predicate,
    *,
    root: Path | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
) -> ScanResult:
    """仅枚举一级元数据；只读取有界 config/profile 的两个路径键。"""
    result = ScanResult()
    root = normalize_path(root if root is not None else Path.home() / ".codex")
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return result
    except OSError:
        result.issues.append(ScanIssue("filesystem_error", "无法读取 Codex 根目录", _TASK, root))
        return result
    facts_list = []
    references: set[Path] = set()
    reference_scan_complete = True
    config_count = 0
    for facts, parent_fd in walk_resources(
        root, protection, result, _TASK, budget=WalkBudget(_MAX_ENTRIES),
        descend=lambda _facts: False, checkpoint=checkpoint, on_progress=on_progress,
    ):
        if not (stat.S_ISREG(facts.stat.st_mode) or stat.S_ISDIR(facts.stat.st_mode)):
            continue
        facts_list.append(facts)
        if facts.path.name != "config.toml" and not facts.path.name.endswith(".config.toml"):
            continue
        config_count += 1
        try:
            if config_count > _MAX_CONFIG_FILES:
                raise ValueError("配置文件数量超过读取上限")
            references.update(_config_references(facts, parent_fd))
        except (OSError, ValueError, UnicodeError, RecursionError):
            reference_scan_complete = False
            # 不输出 TOML 正文或解析异常中的值，配置可能包含秘密。
            result.issues.append(ScanIssue(
                "config_reference_unavailable", "配置引用检查不完整；不能推断材料未被使用",
                _TASK, facts.path,
            ))
    rows = []
    logical_bytes = allocated_bytes = 0
    seen_files: set[tuple[int, int]] = set()
    for facts in sorted(facts_list, key=lambda item: item.path.name):
        classified = _classification(facts, references)
        if classified is None:
            continue
        kind, note = classified
        regular = stat.S_ISREG(facts.stat.st_mode)
        logical = facts.logical_size if regular else None
        allocated = facts.allocated_size if regular else None
        rows.append({
            "path": str(facts.path), "classification": kind, "note": note,
            "entry_type": "file" if regular else "directory",
            "logical_bytes": logical, "allocated_bytes": allocated,
            "small_file": regular and logical <= _SMALL_FILE_BYTES,
            "latest_mtime": facts.stat.st_mtime,
        })
        identity = (facts.stat.st_dev, facts.stat.st_ino)
        if regular and identity not in seen_files:
            logical_bytes += logical
            allocated_bytes += allocated
            seen_files.add(identity)
    if not rows:
        return result
    result.diagnostics.append({
        "kind": "codex_home", "category": _TASK, "anchor_path": str(root),
        "complete": result.complete, "files": rows,
        "reference_scan_complete": reference_scan_complete and result.complete,
        "reference_scope": "user_config_and_profiles_model_paths_only",
        "measurement_scope": "top_level_regular_files_only",
        "small_file_limit_bytes": _SMALL_FILE_BYTES,
    })
    result.items.append(Item(
        root, allocated_bytes, _TASK, "critical",
        f"{len(rows)} 个一级材料/扩展入口；仅计普通文件，目录未递归；额外或较小不等于无用",
        logical_size=logical_bytes, allocated_size=allocated_bytes,
        identity=FileFacts(root, root_stat).identity,
        actionable=False, preselected=False,
        action_block_reason="仅审阅用途与配置引用，不提供清理动作",
        resource_kind="filesystem_subset", diagnostic_kind="codex_home",
        total_count=len(rows),
    ))
    return result

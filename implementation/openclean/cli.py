"""命令行入口 · 独立实现（依据 specs/00-architecture.md §命令面）。

当前实现提供只读扫描/分析，以及必须显式授权的用户态清理执行。
"""
from __future__ import annotations

import argparse
import json
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

from . import __version__
from .analyzer import AnalyzeError, SpaceAnalysis, analyze_path
from .cleanup import (
    CleanupReport,
    SelectionError,
    execute_cleanup,
    select_cleanup_items,
    with_cleanup_selection,
)
from .config import ConfigError, ConfigStore
from .engine import (
    Control,
    IgnoreRules,
    default_project_search_roots,
    finalize_overlapping_result,
    human,
    scan_domains,
    scan_project_artifacts,
)
from .knowledge_base import (
    DEFAULT_KNOWLEDGE_PATH,
    KnowledgeBase,
    KnowledgeBaseError,
    RulesStore,
)
from .knowledge_update import KnowledgeUpdateError, update_knowledge_base
from .macos import volume_mount_point
from .models import Item, ScanResult, normalize_path
from .navigator import run_space_browser
from .progress import TerminalProgressRenderer
from .redaction import redact_json_payload
from .scanpoints import DOMAINS
from .space_tui import SpaceTUIUnavailable, review_space
from .tui import ReviewGroup, TUIUnavailable, review_cleanup

ALL_DOMAINS = list(DOMAINS.keys()) + ["project"]
CLEAN_CATEGORY_DOMAINS = {
    "junk": "system",
    "dev": "developer",
    "ai": "ai",
    "trash": "trash",
}
CLEAN_DOMAIN_LABELS = {
    "system": "System Junk",
    "developer": "Dev Tools",
    "ai": "AI Junk",
    "trash": "Trash",
}
CLI_SCHEMA_VERSION = 2
MINIMUM_PYTHON = (3, 11)
CAT_ART = " /\\_/\\\n( o.o )\n > ^ <"


class CliUsageError(Exception):
    """argparse 用法错误；由 ``main`` 转成文本或 JSON 契约。"""

    def __init__(self, message: str, usage: str) -> None:
        super().__init__(message)
        self.usage = usage


class CliArgumentParser(argparse.ArgumentParser):
    """保留 argparse help 行为，但不让用法错误提前终止进程。"""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise CliUsageError(message, self.format_usage())


def _command_from_argv(argv: list[str]) -> str:
    commands = {
        "scan",
        "clean",
        "analyze",
        "purge",
        "optimize",
        "ignore",
        "config",
        "cat",
    }
    for index, token in enumerate(argv):
        if token not in commands:
            continue
        if token in {"optimize", "ignore"} and index + 1 < len(argv):
            action = argv[index + 1]
            if not action.startswith("-"):
                return f"{token} {action}"
        return token
    return "openclean"


def _print_json_error(
    command: str,
    code: str,
    message: str,
    *,
    exit_code: int,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    _print_json({
        "schema_version": CLI_SCHEMA_VERSION,
        "command": command,
        "status": "error",
        "executed": False,
        "exit_code": exit_code,
        "error": {
            "code": code,
            "message": message,
        },
    }, redact_paths=redact_paths, path_seeds=path_seeds)


def _print_json(
    payload: dict[str, object],
    *,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    output = (
        redact_json_payload(payload, path_seeds=path_seeds)
        if redact_paths
        else payload
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _fail(
    args: argparse.Namespace | None,
    command: str,
    code: str,
    message: str,
    *,
    exit_code: int = 2,
) -> int:
    if args is not None and getattr(args, "json", False):
        _print_json_error(
            command,
            code,
            message,
            exit_code=exit_code,
            redact_paths=getattr(args, "redact_paths", False),
            path_seeds=getattr(args, "_raw_argv", ()),
        )
    else:
        print(message, file=sys.stderr)
    return exit_code


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是大于或等于 0 的整数")
    return parsed


def _add_rule_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help=(
            "仅本次运行按路径子串临时忽略，可多次指定；"
            "持久规则使用 ignore add"
        ),
    )
    _add_rules_path_option(parser)


def _add_json_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--redact-paths",
        action="store_true",
        help=(
            "仅与 --json 配合；把绝对路径替换为单份文档内的 opaque ref，"
            "输出不能直接用于后续 --select"
        ),
    )


def _add_rules_path_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rules",
        "--ignore-rules",
        dest="rules",
        type=Path,
        help="自建 JSON 规则文件；默认读取 ~/.config/openclean/rules.json（若存在）",
    )


def _add_cleanup_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="执行当前选择；没有该参数时始终只预览",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="跳过审阅并执行默认预选项；安全要求仍必须同时指定 --yes",
    )
    parser.add_argument(
        "--all",
        dest="select_all_safe",
        action="store_true",
        help="选择全部可执行 safe 项，包括默认未选择项",
    )
    parser.add_argument(
        "--include-confirm",
        action="store_true",
        help=(
            "无 --select 时批量选择全部可执行 confirm 项；"
            "精确模式中只作为 confirm 风险授权"
        ),
    )
    parser.add_argument(
        "--include-critical",
        action="store_true",
        help=(
            "无 --select 时批量选择全部可执行 critical 项；"
            "精确模式中只作为 critical 第二重授权"
        ),
    )
    parser.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="PATH_OR_ID",
        help=(
            "精确选择完整路径或资源 identifier，可多次指定；"
            "不继承默认预选，confirm/critical 仍需对应授权"
        ),
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="即使连接 TTY 也使用文本/参数化流程",
    )


def _load_ignore_rules(args: argparse.Namespace) -> IgnoreRules:
    knowledge_base = KnowledgeBase.load_configured(args.rules)
    return IgnoreRules(args.ignore, knowledge_base=knowledge_base)


def _progress_renderer(as_json: bool) -> TerminalProgressRenderer | None:
    if as_json or not sys.stderr.isatty():
        return None
    return TerminalProgressRenderer(stream=sys.stderr)


def _validate_cleanup_execution_args(args: argparse.Namespace) -> str | None:
    if args.force and not args.yes:
        return "--force 需要同时指定 --yes；拒绝执行删除"
    if args.force and (
        args.select_all_safe
        or args.include_confirm
        or args.include_critical
        or args.select
    ):
        return "--force 只执行默认预选项，不能与选择扩展参数同时使用"
    if args.select and args.select_all_safe:
        return "--select 是精确选择模式，不能与批量选择参数 --all 同时使用"
    return None


def _validated_project_roots(raw_roots: list[Path]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw_root in raw_roots:
        root = normalize_path(raw_root)
        if root in seen:
            continue
        seen.add(root)
        try:
            root_stat = root.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"项目扫描根目录不存在：{root}") from exc
        except (PermissionError, OSError) as exc:
            raise ValueError(f"无法访问项目扫描根目录 {root}：{exc}") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise ValueError(f"项目扫描根目录不能是符号链接：{root}")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError(f"项目扫描根路径不是目录：{root}")
        roots.append(root)
    return roots


def _prepare_cleanup_selection(
    result: ScanResult,
    args: argparse.Namespace,
) -> tuple[ScanResult, list[Item]]:
    selected = select_cleanup_items(
        result.items,
        selectors=args.select,
        select_all_safe=args.select_all_safe,
        include_confirm=args.include_confirm,
        include_critical=args.include_critical,
    )
    return with_cleanup_selection(result, selected), selected


def _use_cleanup_tui(args: argparse.Namespace) -> bool:
    return (
        not args.json
        and not args.no_interactive
        and not args.force
        and not args.select_all_safe
        and not args.include_confirm
        and not args.include_critical
        and not args.select
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def _clean_review_groups(result: ScanResult) -> tuple[ReviewGroup, ...]:
    grouped = result.by_domain()
    return tuple(
        ReviewGroup(
            key=domain,
            label=CLEAN_DOMAIN_LABELS.get(domain, domain),
            items=tuple(grouped.get(domain, ())),
        )
        for domain in CLEAN_DOMAIN_LABELS
        if grouped.get(domain)
    )


def _purge_review_groups(result: ScanResult) -> tuple[ReviewGroup, ...]:
    return tuple(
        ReviewGroup(
            key=str(project_root),
            label=project_root.name,
            items=tuple(items),
        )
        for project_root, items in sorted(
            result.by_project().items(), key=lambda pair: str(pair[0])
        )
    )


def _resolve_cleanup_selection(
    result: ScanResult,
    args: argparse.Namespace,
    groups: tuple[ReviewGroup, ...],
    *,
    title: str,
) -> tuple[ScanResult, list[Item], bool, bool]:
    """返回结果快照、选择、是否允许执行、是否由用户取消。"""
    if _use_cleanup_tui(args):
        try:
            review = review_cleanup(
                groups,
                title=title,
                allow_execution=args.yes,
            )
        except TUIUnavailable as exc:
            if args.yes:
                raise SelectionError(
                    f"{exc}；为避免无审阅执行，请改用 --no-interactive"
                ) from exc
            print(f"终端审阅不可用，回退到文本预览：{exc}", file=sys.stderr)
        else:
            if review.cancelled:
                return result, [], False, True
            selected = list(review.selected)
            return (
                with_cleanup_selection(result, selected),
                selected,
                review.execution_confirmed,
                False,
            )
    marked, selected = _prepare_cleanup_selection(result, args)
    return marked, selected, args.yes, False


def _print_issues(result) -> None:
    if not result.issues:
        return
    blocking = sum(issue.blocking for issue in result.issues)
    heading = (
        f"扫描未完整：{blocking} 个问题，{len(result.issues) - blocking} 个提示"
        if blocking
        else f"扫描提示：{len(result.issues)} 项"
    )
    print(f"\n{heading}", file=sys.stderr)
    for issue in result.issues:
        location = f" ({issue.path})" if issue.path is not None else ""
        level = "warning" if issue.blocking else "info"
        print(
            f"- [{level}:{issue.code}] {issue.task}: {issue.message}{location}",
            file=sys.stderr,
        )


def _item_payload(item) -> dict[str, object]:
    return {
        "path": str(item.path) if item.path is not None else None,
        "resource_kind": item.resource_kind,
        "identifier": item.identifier or None,
        "bytes": item.size,
        "potential_bytes": item.size,
        "reclaimable_bytes": item.size if item.actionable else 0,
        "human": human(item.size),
        "resource_total_bytes": item.resource_total_size,
        "total_count": item.total_count,
        "active_count": item.active_count,
        "running_process_markers": list(item.running_process_markers),
        "cleanup_scope": item.cleanup_scope or None,
        "cleanup_root": (
            str(item.cleanup_root) if item.cleanup_root is not None else None
        ),
        "startup_program": item.startup_program or None,
        "startup_program_uses_path": item.startup_program_uses_path,
        "logical_bytes": item.logical_size,
        "allocated_bytes": item.allocated_size,
        "is_cloud_file": item.is_cloud_file,
        "cloud_file_count": item.cloud_file_count,
        "cloud_logical_bytes": item.cloud_logical_size,
        "actionable": item.actionable,
        "action_block_reason": item.action_block_reason or None,
        "requires_privilege": item.requires_privilege,
        "path_source": item.path_source,
        "requires_explicit_selection": item.requires_explicit_selection,
        "category": item.category,
        "safety": item.safety,
        "note": item.note,
        "project_root": (
            str(item.project_root) if item.project_root is not None else None
        ),
        "artifact_name": item.artifact_name or None,
        "latest_mtime": item.latest_mtime,
        "age_days": item.age_days,
        "preselected": item.preselected,
        "excluded_paths": item.excluded_paths,
        "cross_device_paths": item.cross_device_paths,
        "device_id": item.identity.device if item.identity is not None else None,
        "updater_status": item.updater_status or None,
        "installed_version": item.installed_version or None,
        "staged_version": item.staged_version or None,
        "updater_external_install": item.updater_external_install,
        "diagnostic_kind": item.diagnostic_kind or None,
        "open_handle_count": item.open_handle_count,
        "retention_file_count": item.retention_file_count,
        "retention_7d_bytes": item.retention_7d_bytes,
        "retention_14d_bytes": item.retention_14d_bytes,
        "retention_30d_bytes": item.retention_30d_bytes,
        "sqlite_page_size": item.sqlite_page_size,
        "sqlite_page_count": item.sqlite_page_count,
        "sqlite_freelist_count": item.sqlite_freelist_count,
        "sqlite_internal_free_bytes": item.sqlite_internal_free_bytes,
        "sqlite_internal_free_ratio": item.sqlite_internal_free_ratio,
        "sqlite_wal_bytes": item.sqlite_wal_bytes,
        "domain": item.domain or None,
    }


def _volume_summaries(
    items: Iterable[Item],
    *,
    report_reclaimable: bool = True,
) -> list[dict[str, object]]:
    grouped: dict[int, list[Item]] = {}
    for item in items:
        if item.path is None or item.identity is None:
            continue
        grouped.setdefault(item.identity.device, []).append(item)

    summaries: list[dict[str, object]] = []
    for device_id, volume_items in grouped.items():
        try:
            mount_point = volume_mount_point(volume_items[0].path)
        except (FileNotFoundError, PermissionError, OSError):
            mount_point = None
        summaries.append(
            {
                "device_id": device_id,
                "mount_point": (
                    str(mount_point) if mount_point is not None else None
                ),
                "system_disk": mount_point == Path("/"),
                "item_count": len(volume_items),
                "potential_bytes": sum(item.size for item in volume_items),
                "reclaimable_bytes": (
                    sum(item.size for item in volume_items if item.actionable)
                    if report_reclaimable
                    else 0
                ),
                "preselected_bytes": sum(
                    item.size
                    for item in volume_items
                    if item.preselected is True
                ),
                "requires_privilege_bytes": sum(
                    item.size
                    for item in volume_items
                    if item.requires_privilege
                ),
                "unsupported_bytes": sum(
                    item.size
                    for item in volume_items
                    if not item.actionable and not item.requires_privilege
                ),
            }
        )
    return sorted(
        summaries,
        key=lambda summary: -int(summary["potential_bytes"]),
    )


def _item_location(item) -> str:
    if item.path is not None:
        return str(item.path)
    return item.identifier or item.resource_kind


def _item_annotations(item: Item) -> str:
    annotations: list[str] = []
    if item.diagnostic_kind:
        annotations.append(f"[只读诊断: {item.diagnostic_kind}]")
    if item.updater_status:
        versions = (
            f" installed={item.installed_version or 'unknown'}"
            f" staged={item.staged_version or 'unknown'}"
        )
        external = " external-install" if item.updater_external_install else ""
        annotations.append(
            f"[updater: {item.updater_status}{versions}{external}]"
        )
    if item.requires_explicit_selection:
        annotations.append("[需 --select 精确选择]")
    if item.requires_privilege:
        annotations.append("[需要特权 helper]")
    if not item.actionable:
        reason = item.action_block_reason or "该候选不可执行"
        annotations.append(f"[不可执行: {reason}]")
    return f"  {' '.join(annotations)}" if annotations else ""


def _cleanup_payload(report: CleanupReport) -> dict[str, object]:
    return {
        "complete": report.complete,
        "selected_count": report.selected_count,
        "selected_bytes": report.selected_bytes,
        "moved_to_trash_bytes": report.moved_bytes,
        "permanently_deleted_bytes": report.deleted_bytes,
        "outcomes": [
            {
                "path": (
                    str(outcome.item.path)
                    if outcome.item.path is not None
                    else None
                ),
                "identifier": outcome.item.identifier or None,
                "category": outcome.item.category,
                "status": outcome.status,
                "planned_bytes": outcome.item.size,
                "bytes_affected": outcome.bytes_affected,
                "destination": (
                    str(outcome.destination)
                    if outcome.destination is not None
                    else None
                ),
                "message": outcome.message,
            }
            for outcome in report.outcomes
        ],
    }


def _print_cleanup_summary(report: CleanupReport) -> None:
    if not report.outcomes:
        print("没有选中可执行候选；未修改文件。")
        return
    print("\n执行结果")
    print("─" * 88)
    for outcome in report.outcomes:
        location = _item_location(outcome.item)
        destination = (
            f" → {outcome.destination}"
            if outcome.destination is not None
            else ""
        )
        print(
            f"  {outcome.status:<18}{human(outcome.bytes_affected):>10}  "
            f"{location}{destination}"
        )
        if outcome.message:
            print(f"    {outcome.message}")
    print("─" * 88)
    print(
        f"移动到 Trash：{human(report.moved_bytes)}；"
        f"永久释放：{human(report.deleted_bytes)}"
    )


def _run_root_menu() -> int:
    actions = {
        "1": ["clean"],
        "2": ["purge"],
        "3": ["analyze"],
        "4": ["config"],
        "5": ["cat"],
    }
    while True:
        print(
            "\nopenclean · Quick actions\n"
            "  完整命令与参数：openclean --help\n"
            "  1. Clean    扫描并审阅垃圾\n"
            "  2. Purge    查找项目构建产物\n"
            "  3. Analyze  分析磁盘空间\n"
            "  4. Config   查看 CLI 偏好\n"
            "  5. Cat      召唤一位朋友\n"
            "  q. Quit"
        )
        try:
            choice = input("选择：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0
        command = actions.get(choice)
        if command is None:
            print("无效选择。", file=sys.stderr)
            continue
        status = main(command)
        if status not in {0, 1}:
            return status


def _issue_payload(issue) -> dict[str, object]:
    return {
        "code": issue.code,
        "message": issue.message,
        "task": issue.task,
        "path": str(issue.path) if issue.path is not None else None,
        "blocking": issue.blocking,
    }


def _print_report(
    result: ScanResult,
    as_json: bool,
    requested_domains: list[str],
    *,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    if as_json:
        _print_json({
            "schema_version": CLI_SCHEMA_VERSION,
            "command": "scan",
            "mode": "report",
            "requested_domains": requested_domains,
            "complete": result.complete,
            "cancelled": result.cancelled,
            "total_bytes": result.total,
            "potential_bytes": result.total,
            "reclaimable_bytes": result.actionable_total,
            "requires_privilege_bytes": result.requires_privilege_total,
            "unsupported_bytes": result.unsupported_total,
            "total_human": human(result.total),
            "reclaimable_human": human(result.actionable_total),
            "volumes": _volume_summaries(result.items),
            "items": [
                _item_payload(i)
                for i in sorted(result.items, key=lambda x: -x.size)
            ],
            "issues": [_issue_payload(issue) for issue in result.issues],
        }, redact_paths=redact_paths, path_seeds=path_seeds)
        return

    cats = result.by_category()
    print(f"\n{'类别':<22}{'大小':>10}  {'安全':<8} 路径")
    print("─" * 78)
    for cat, items in sorted(cats.items(),
                             key=lambda kv: -sum(i.size for i in kv[1])):
        for i in sorted(items, key=lambda x: -x.size):
            print(
                f"{cat:<22}{human(i.size):>10}  "
                f"{i.safety:<8} {_item_location(i)}{_item_annotations(i)}"
            )
    print("─" * 78)
    print(
        f"{'合计发现':<22}{human(result.total):>10}；"
        f"当前可执行 {human(result.actionable_total)}"
    )
    _print_issues(result)


def _print_purge_report(
    result: ScanResult,
    as_json: bool,
    cleanup: CleanupReport | None = None,
    *,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    projects = result.by_project()
    if as_json:
        project_payload = []
        for project_root, items in sorted(projects.items(), key=lambda pair: str(pair[0])):
            project_payload.append({
                "name": project_root.name,
                "path": str(project_root),
                "total_bytes": sum(item.size for item in items),
                "reclaimable_bytes": sum(
                    item.size for item in items if item.actionable
                ),
                "preselected_bytes": sum(
                    item.size for item in items if item.preselected is True
                ),
                "artifacts": [_item_payload(item) for item in items],
            })
        _print_json({
            "schema_version": CLI_SCHEMA_VERSION,
            "command": "purge",
            "mode": "result" if cleanup is not None else "preview",
            "complete": result.complete,
            "cancelled": result.cancelled,
            "total_bytes": result.total,
            "potential_bytes": result.total,
            "reclaimable_bytes": result.actionable_total,
            "requires_privilege_bytes": result.requires_privilege_total,
            "unsupported_bytes": result.unsupported_total,
            "total_human": human(result.total),
            "reclaimable_human": human(result.actionable_total),
            "preselected_bytes": result.preselected_total,
            "preselected_human": human(result.preselected_total),
            "volumes": _volume_summaries(result.items),
            "projects": project_payload,
            "issues": [_issue_payload(issue) for issue in result.issues],
            "cleanup": (
                _cleanup_payload(cleanup) if cleanup is not None else None
            ),
        }, redact_paths=redact_paths, path_seeds=path_seeds)
        return

    if not projects:
        print("未发现项目构建产物。")
        _print_issues(result)
        return
    title = (
        "项目清理执行结果"
        if cleanup is not None
        else "项目构建产物（只读预览，不会删除）"
    )
    print(f"\n{title}")
    print("─" * 88)
    for project_root, items in sorted(projects.items(), key=lambda pair: str(pair[0])):
        project_total = sum(item.size for item in items)
        print(f"\n{project_root.name}  {human(project_total)}  {project_root}")
        for item in sorted(items, key=lambda candidate: -candidate.size):
            marker = "[x]" if item.preselected else "[ ]"
            age = f"{item.age_days} 天" if item.age_days is not None else "未知"
            cloud = (
                f"  云占位 {item.cloud_file_count}"
                if item.cloud_file_count
                else ""
            )
            print(
                f"  {marker} {item.artifact_name:<20} "
                f"{human(item.size):>10}  {age:>8}  "
                f"{item.safety:<8} {item.path}{cloud}{_item_annotations(item)}"
            )
    print("\n" + "─" * 88)
    print(
        f"发现 {human(result.total)}；当前选择 "
        f"{human(result.preselected_total)}"
    )
    if cleanup is None:
        print("当前是只读预览；添加 --yes 才会执行当前选择。")
    else:
        _print_cleanup_summary(cleanup)
    _print_issues(result)


def _print_clean_report(
    result: ScanResult,
    as_json: bool,
    requested_category: str | None,
    cleanup: CleanupReport | None = None,
    *,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    grouped = result.by_domain()
    domains = [
        domain
        for domain in CLEAN_DOMAIN_LABELS
        if grouped.get(domain)
    ]
    if as_json:
        categories = []
        for domain in domains:
            items = sorted(grouped[domain], key=lambda item: -item.size)
            categories.append({
                "domain": domain,
                "name": CLEAN_DOMAIN_LABELS[domain],
                "total_bytes": sum(item.size for item in items),
                "reclaimable_bytes": sum(
                    item.size for item in items if item.actionable
                ),
                "preselected_bytes": sum(
                    item.size for item in items if item.preselected is True
                ),
                "items": [_item_payload(item) for item in items],
            })
        _print_json({
            "schema_version": CLI_SCHEMA_VERSION,
            "command": "clean",
            "category": requested_category or "all",
            "mode": "result" if cleanup is not None else "preview",
            "complete": result.complete,
            "cancelled": result.cancelled,
            "total_bytes": result.total,
            "potential_bytes": result.total,
            "reclaimable_bytes": result.actionable_total,
            "requires_privilege_bytes": result.requires_privilege_total,
            "unsupported_bytes": result.unsupported_total,
            "total_human": human(result.total),
            "reclaimable_human": human(result.actionable_total),
            "preselected_bytes": result.preselected_total,
            "preselected_human": human(result.preselected_total),
            "volumes": _volume_summaries(result.items),
            "categories": categories,
            "issues": [_issue_payload(issue) for issue in result.issues],
            "cleanup": (
                _cleanup_payload(cleanup) if cleanup is not None else None
            ),
        }, redact_paths=redact_paths, path_seeds=path_seeds)
        return

    if not domains:
        print("未发现可清理项。")
        _print_issues(result)
        return
    title = (
        "清理执行结果"
        if cleanup is not None
        else "清理扫描结果（只读预览，不会删除）"
    )
    print(f"\n{title}")
    print("─" * 88)
    for domain in domains:
        items = grouped[domain]
        domain_total = sum(item.size for item in items)
        print(f"\n{CLEAN_DOMAIN_LABELS[domain]}  {human(domain_total)}")
        for item in sorted(items, key=lambda candidate: -candidate.size):
            marker = "[x]" if item.preselected else "[ ]"
            cloud = (
                f"  云占位 {item.cloud_file_count}"
                if item.cloud_file_count
                else ""
            )
            print(
                f"  {marker} {item.category:<24} "
                f"{human(item.size):>10}  {item.safety:<8} "
                f"{_item_location(item)}{cloud}{_item_annotations(item)}"
            )
    print("\n" + "─" * 88)
    print(
        f"发现 {human(result.total)}；当前选择 "
        f"{human(result.preselected_total)}"
    )
    if cleanup is None:
        print("当前是只读预览；添加 --yes 才会执行当前选择。")
    else:
        _print_cleanup_summary(cleanup)
    _print_issues(result)


def _print_analyze_report(
    analysis: SpaceAnalysis,
    as_json: bool,
    top: int,
    selected: list[Item] | None = None,
    cleanup: CleanupReport | None = None,
    *,
    redact_paths: bool = False,
    path_seeds: tuple[str, ...] = (),
) -> None:
    selected = selected or []
    selected_paths = {item.path for item in selected}
    entries = analysis.entries[:top] if top else analysis.entries
    if as_json:
        entry_count_total = len(analysis.entries)
        entry_count_returned = len(entries)
        _print_json({
            "schema_version": CLI_SCHEMA_VERSION,
            "command": "analyze",
            "mode": (
                "result"
                if cleanup is not None
                else "selection-preview"
                if selected
                else "browse"
            ),
            "top": top,
            "truncated": entry_count_returned < entry_count_total,
            "entry_count_total": entry_count_total,
            "entry_count_returned": entry_count_returned,
            "root": str(analysis.root),
            "complete": analysis.complete,
            "cancelled": analysis.cancelled,
            "total_bytes": analysis.total,
            "potential_bytes": analysis.total,
            # Space Lens 只描述占用，不把任意一级目录归类为垃圾。
            "reclaimable_bytes": 0,
            "allocated_bytes": analysis.total,
            "total_human": human(analysis.total),
            "volumes": _volume_summaries(
                (entry.item for entry in analysis.entries),
                report_reclaimable=False,
            ),
            "volume": {
                "total_bytes": analysis.volume_total,
                "used_bytes": analysis.volume_used,
                "free_bytes": analysis.volume_free,
            },
            "local_snapshots": {
                "checked": analysis.local_snapshots_checked,
                "mount_point": (
                    str(analysis.snapshot_mount_point)
                    if analysis.snapshot_mount_point is not None
                    else None
                ),
                "count": len(analysis.local_snapshots),
                "names": list(analysis.local_snapshots),
                "size_bytes": analysis.local_snapshot_size,
                "size_known": analysis.local_snapshot_size is not None,
                "deletion_supported": False,
            },
            "entries": [
                {
                    **_item_payload(entry.item),
                    "reclaimable_bytes": 0,
                    "percent": entry.percent,
                    "selected": entry.item.path in selected_paths,
                }
                for entry in entries
            ],
            "selection": {
                "count": len(selected),
                "bytes": sum(item.size for item in selected),
                "paths": [str(item.path) for item in selected],
            },
            "cleanup": (
                _cleanup_payload(cleanup) if cleanup is not None else None
            ),
            "issues": [_issue_payload(issue) for issue in analysis.issues],
        }, redact_paths=redact_paths, path_seeds=path_seeds)
        return

    print(f"\n空间分析：{analysis.root}")
    if analysis.volume_total is not None and analysis.volume_free is not None:
        print(
            f"卷空间：{human(analysis.volume_free)} 可用 / "
            f"{human(analysis.volume_total)} 总计"
        )
    if analysis.local_snapshots_checked:
        print(
            f"Time Machine 本地快照：{len(analysis.local_snapshots)} 个；"
            "公开列表不提供精确占用，本工具不会自动删除"
        )
    print("─" * 88)
    for entry in entries:
        marker = "[x]" if entry.item.path in selected_paths else "[ ]"
        cloud = (
            f"  云占位 {entry.item.cloud_file_count}"
            if entry.item.cloud_file_count
            else ""
        )
        print(
            f"{marker} {human(entry.item.size):>10}  {entry.percent:6.1f}%  "
            f"{entry.item.path}{cloud}{_item_annotations(entry.item)}"
        )
    print("─" * 88)
    if len(entries) < len(analysis.entries):
        print(
            f"仅显示最大的 {len(entries)}/{len(analysis.entries)} 项；"
            "当前层级合计仍包含全部项目。"
        )
    print(f"当前层级合计：{human(analysis.total)}")
    if cleanup is not None:
        _print_cleanup_summary(cleanup)
    elif selected:
        print(
            f"已选择 {len(selected)} 项，共 "
            f"{human(sum(item.size for item in selected))}；未执行。"
        )
    else:
        print("当前是只读单层视图；TTY 模式可导航、复选和 Finder reveal。")
    _print_issues(analysis)


def _select_analyze_items(
    analysis: SpaceAnalysis, selectors: list[str]
) -> list[Item]:
    selected: list[Item] = []
    for selector in selectors:
        target = normalize_path(selector)
        matches = [
            entry.item
            for entry in analysis.entries
            if entry.item.path == target
        ]
        if not matches:
            raise SelectionError(f"当前层级未找到分析候选：{selector}")
        item = matches[0]
        if not item.actionable:
            reason = item.action_block_reason or "该候选不可执行"
            raise SelectionError(f"拒绝选择 {selector}：{reason}")
        if item not in selected:
            selected.append(item)
    return selected


def _print_space_tui_result(
    selected: list[Item], cleanup: CleanupReport | None
) -> None:
    if not selected:
        print("没有选择任何空间项；未修改文件。")
        return
    print("\nAnalyze 选择结果")
    print("─" * 88)
    for item in selected:
        print(f"  [x] {human(item.size):>10}  {item.path}")
    print("─" * 88)
    print(
        f"共 {len(selected)} 项，{human(sum(item.size for item in selected))}"
    )
    if cleanup is None:
        print("未指定或未确认 --yes；没有修改文件。")
    else:
        _print_cleanup_summary(cleanup)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    ap = CliArgumentParser(
        prog="openclean",
        description="open-cleanmymac：macOS 清理工具（独立净室实现）")
    ap.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser(
        "scan",
        help="扫描并报告磁盘占用、清理候选和只读诊断（不删除）",
    )
    sp.add_argument("--domain", action="append", choices=ALL_DOMAINS,
                    help=f"扫描域，可多次指定；默认全部：{', '.join(ALL_DOMAINS)}")
    sp.add_argument(
        "--project-root",
        action="append",
        type=Path,
        help=(
            "项目产物扫描根目录，可多次指定；必须是非 symlink 的现有目录，"
            "并配合 --domain project"
        ),
    )
    _add_rule_options(sp)
    sp.add_argument(
        "--workers",
        type=_positive_int,
        default=8,
        help="并发扫描 worker 数；默认 8",
    )
    _add_json_output_options(sp)

    clean = sub.add_parser(
        "clean",
        help="扫描并审阅 junk/dev/ai/trash；仅 --yes 才执行当前选择",
        description=(
            "默认只读扫描并审阅候选。普通文件系统项通常移动到同卷 Trash；"
            "clean trash 与 Docker prune 是永久操作，不经过可恢复的 Trash。"
        ),
    )
    clean.add_argument(
        "category",
        nargs="?",
        choices=tuple(CLEAN_CATEGORY_DOMAINS),
        help="可选清理分类；省略时扫描全部分类",
    )
    _add_cleanup_execution_options(clean)
    _add_rule_options(clean)
    clean.add_argument(
        "--workers",
        type=_positive_int,
        default=8,
        help="并发扫描 worker 数；默认 8",
    )
    _add_json_output_options(clean)

    analyze = sub.add_parser(
        "analyze",
        help="交互分析空间；删除所选项仍必须显式 --yes",
    )
    analyze.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("/"),
        help="待分析目录；省略时使用启动盘根目录 /",
    )
    analyze.add_argument(
        "--top",
        type=_nonnegative_int,
        default=0,
        help="仅显示最大的 N 项；0 表示全部",
    )
    _add_rule_options(analyze)
    analyze_mode = analyze.add_mutually_exclusive_group()
    analyze_mode.add_argument(
        "--no-interactive",
        action="store_true",
        help="即使连接 TTY 也只输出当前层级报告",
    )
    analyze_mode.add_argument(
        "--line-interactive",
        action="store_true",
        help="使用兼容的行式只读导航器，而不是 curses 全屏界面",
    )
    analyze.add_argument(
        "--yes",
        action="store_true",
        help="允许在全屏确认或精确 --select 后移动所选项到同卷 Trash",
    )
    analyze.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="PATH",
        help="非交互模式精确选择当前层级路径，可多次指定",
    )
    _add_json_output_options(analyze)

    purge = sub.add_parser(
        "purge",
        help="按项目扫描可重建产物；仅 --yes 才执行当前选择",
        description=(
            "默认只读扫描项目中的可重建产物。只有明确选择并指定 --yes 后，"
            "普通文件系统项才会移动到同卷 Trash。"
        ),
    )
    purge.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="待扫描目录；默认扫描 ~/Projects、~/Code、~/dev、~/GitHub、~/Workspace",
    )
    _add_cleanup_execution_options(purge)
    purge.add_argument(
        "--max-depth",
        type=_nonnegative_int,
        default=6,
        help="从扫描根向下识别项目的最大目录层数；默认 6",
    )
    _add_rule_options(purge)
    _add_json_output_options(purge)

    optimize = sub.add_parser(
        "optimize",
        help="显示 ram/purgeable 能力状态；安全公开执行器尚不可用",
    )
    optimize_sub = optimize.add_subparsers(
        dest="optimize_cmd",
        required=True,
    )
    optimize_ram = optimize_sub.add_parser(
        "ram",
        help="释放 RAM（当前安全拒绝执行）",
        description=(
            "当前只提供能力探测：macOS 没有已验证的公开无特权 RAM "
            "释放接口；命令返回 unavailable、预期退出码 1，且不执行操作。"
        ),
    )
    _add_json_output_options(optimize_ram)
    optimize_purgeable = optimize_sub.add_parser(
        "purgeable",
        help="释放 purgeable disk space（当前安全拒绝执行）",
        description=(
            "当前只提供能力探测：尚未找到可验证且安全的公开 purgeable "
            "space 释放接口；命令返回 unavailable、预期退出码 1，且不执行操作。"
        ),
    )
    _add_json_output_options(optimize_purgeable)

    ignore_command = sub.add_parser(
        "ignore",
        help="管理 scan、clean、purge 和 analyze 共用的持久忽略路径",
        description=(
            "管理 scan、clean、purge 和 analyze 共用的持久忽略路径。"
        ),
    )
    ignore_sub = ignore_command.add_subparsers(
        dest="ignore_cmd", required=True
    )
    ignore_list = ignore_sub.add_parser("list", help="列出忽略路径")
    _add_rules_path_option(ignore_list)
    _add_json_output_options(ignore_list)
    ignore_add = ignore_sub.add_parser("add", help="添加忽略路径")
    ignore_add.add_argument(
        "path",
        type=Path,
        help="要持久忽略的路径；保存为规范化绝对路径",
    )
    _add_rules_path_option(ignore_add)
    _add_json_output_options(ignore_add)
    ignore_remove = ignore_sub.add_parser("remove", help="移除精确匹配的忽略路径")
    ignore_remove.add_argument(
        "path",
        type=Path,
        help="要移除的路径；必须与持久规则精确匹配",
    )
    _add_rules_path_option(ignore_remove)
    _add_json_output_options(ignore_remove)

    config_command = sub.add_parser(
        "config", help="查看或更新 CLI 偏好设置"
    )
    config_action = config_command.add_mutually_exclusive_group()
    config_action.add_argument(
        "--analytics",
        choices=("on", "off"),
        help="持久化 analytics 偏好；当前版本尚未实现遥测上传",
    )
    config_action.add_argument(
        "--update-knowledge",
        metavar="HTTPS_URL",
        help="从显式 HTTPS URL 下载并安装签名托管知识库",
    )
    config_command.add_argument(
        "--knowledge-public-key",
        type=Path,
        metavar="PEM",
        help="知识库签名公钥；与 --update-knowledge 同时使用",
    )
    config_command.add_argument(
        "--allow-key-rotation",
        action="store_true",
        help="显式允许托管知识库更换已钉住的公钥",
    )
    config_command.add_argument(
        "--knowledge-path",
        type=Path,
        help=argparse.SUPPRESS,
    )
    config_command.add_argument(
        "--config-path",
        type=Path,
        help=argparse.SUPPRESS,
    )
    _add_json_output_options(config_command)

    cat_command = sub.add_parser("cat", help="召唤一位终端朋友")
    _add_json_output_options(cat_command)
    try:
        args = ap.parse_args(raw_argv)
    except CliUsageError as exc:
        if "--json" in raw_argv:
            _print_json_error(
                _command_from_argv(raw_argv),
                "usage_error",
                str(exc),
                exit_code=2,
                redact_paths="--redact-paths" in raw_argv,
                path_seeds=tuple(raw_argv),
            )
        else:
            print(exc.usage, end="", file=sys.stderr)
            print(f"openclean: error: {exc}", file=sys.stderr)
        return 2

    args._raw_argv = tuple(raw_argv)

    if args.cmd is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return _run_root_menu()
        ap.print_help()
        return 0

    if getattr(args, "redact_paths", False) and not args.json:
        return _fail(
            args,
            _command_from_argv(raw_argv),
            "invalid_output_options",
            "--redact-paths 必须与 --json 同时使用。",
        )

    if tuple(sys.version_info[:2]) < MINIMUM_PYTHON:
        running = ".".join(str(part) for part in sys.version_info[:3])
        return _fail(
            args,
            _command_from_argv(raw_argv),
            "unsupported_python",
            "openclean 需要 Python 3.11 或更高版本；"
            f"当前运行时为 Python {running}。",
        )

    if args.cmd == "optimize":
        reason = (
            "macOS 没有已验证的公开无特权 RAM 释放接口"
            if args.optimize_cmd == "ram"
            else "尚未找到可验证且安全的公开 purgeable space 释放接口"
        )
        if args.json:
            _print_json({
                "schema_version": CLI_SCHEMA_VERSION,
                "command": f"optimize {args.optimize_cmd}",
                "mode": "guard",
                "status": "unavailable",
                "executed": False,
                "reason": reason,
            }, redact_paths=args.redact_paths, path_seeds=tuple(raw_argv))
        else:
            print(
                f"optimize {args.optimize_cmd}：{reason}；未执行任何操作。",
                file=sys.stderr,
            )
        return 1

    if args.cmd == "scan":
        domains = list(dict.fromkeys(args.domain or ALL_DOMAINS))
        if (
            args.project_root
            and args.domain is not None
            and "project" not in domains
        ):
            return _fail(
                args,
                "scan",
                "invalid_option_combination",
                "scan：--project-root 需要同时请求 --domain project。",
            )
        explicit_project_roots: list[Path] | None = None
        if args.project_root:
            try:
                explicit_project_roots = _validated_project_roots(
                    args.project_root
                )
            except ValueError as exc:
                return _fail(
                    args,
                    "scan",
                    "invalid_project_root",
                    f"scan：{exc}。",
                )
        ctl = Control()
        try:
            ignore = _load_ignore_rules(args)
        except KnowledgeBaseError as exc:
            return _fail(
                args,
                "scan",
                "rules_error",
                f"规则加载失败：{exc}",
            )
        items_result = ScanResult()
        try:
            regular = [d for d in domains if d != "project"]
            if regular:
                renderer = _progress_renderer(args.json)
                try:
                    regular_result = scan_domains(
                        regular,
                        ctl,
                        ignore,
                        args.workers,
                        on_progress=renderer,
                    )
                finally:
                    if renderer is not None:
                        renderer.finish()
                items_result.items.extend(regular_result.items)
                items_result.issues.extend(regular_result.issues)
                items_result.cancelled = regular_result.cancelled
            if "project" in domains:
                roots = explicit_project_roots or default_project_search_roots()
                if roots:
                    renderer = _progress_renderer(args.json)
                    try:
                        pr = scan_project_artifacts(
                            roots,
                            ctl,
                            ignore,
                            include_unmarked_roots=bool(args.project_root),
                            on_progress=renderer,
                        )
                    finally:
                        if renderer is not None:
                            renderer.finish()
                    items_result.items.extend(pr.items)
                    items_result.issues.extend(pr.issues)
                    items_result.cancelled = items_result.cancelled or pr.cancelled
        except KeyboardInterrupt:
            ctl.cancel()
            return _fail(
                args,
                "scan",
                "cancelled",
                "已取消。",
                exit_code=130,
            )

        items_result = finalize_overlapping_result(items_result)
        if not items_result.items and not args.json:
            print("未发现可清理项。")
            _print_issues(items_result)
        else:
            _print_report(
                items_result,
                args.json,
                domains,
                redact_paths=args.redact_paths,
                path_seeds=tuple(raw_argv),
            )
        return 0 if items_result.complete else 1

    if args.cmd == "clean":
        if error := _validate_cleanup_execution_args(args):
            return _fail(
                args,
                "clean",
                "invalid_selection_options",
                f"clean：{error}。",
            )
        domains = (
            [CLEAN_CATEGORY_DOMAINS[args.category]]
            if args.category is not None
            else list(CLEAN_CATEGORY_DOMAINS.values())
        )
        try:
            ignore = _load_ignore_rules(args)
        except KnowledgeBaseError as exc:
            return _fail(
                args,
                "clean",
                "rules_error",
                f"规则加载失败：{exc}",
            )
        ctl = Control()
        renderer = _progress_renderer(args.json)
        try:
            result = finalize_overlapping_result(
                scan_domains(
                    domains,
                    ctl,
                    ignore,
                    args.workers,
                    on_progress=renderer,
                )
            )
        except KeyboardInterrupt:
            ctl.cancel()
            return _fail(
                args,
                "clean",
                "cancelled",
                "已取消。",
                exit_code=130,
            )
        finally:
            if renderer is not None:
                renderer.finish()
        try:
            result, selected, execution_confirmed, cancelled = (
                _resolve_cleanup_selection(
                    result,
                    args,
                    _clean_review_groups(result),
                    title="Clean · 扫描结果",
                )
            )
        except SelectionError as exc:
            return _fail(
                args,
                "clean",
                "selection_error",
                f"清理选择无效：{exc}",
            )
        if cancelled:
            return 0
        cleanup = (
            execute_cleanup(selected, ignore)
            if args.yes and execution_confirmed
            else None
        )
        _print_clean_report(
            result,
            args.json,
            args.category,
            cleanup,
            redact_paths=args.redact_paths,
            path_seeds=tuple(raw_argv),
        )
        return 0 if (
            result.complete and (cleanup is None or cleanup.complete)
        ) else 1

    if args.cmd == "analyze":
        try:
            ignore = _load_ignore_rules(args)
        except KnowledgeBaseError as exc:
            return _fail(
                args,
                "analyze",
                "rules_error",
                f"规则加载失败：{exc}",
            )
        if args.line_interactive:
            if args.json or args.yes or args.select:
                return _fail(
                    args,
                    "analyze",
                    "invalid_mode_options",
                    "--line-interactive 是只读导航模式，不能与 "
                    "--json、--yes 或 --select 同时使用。",
                )
            return run_space_browser(
                args.path,
                protection=ignore,
                top=args.top,
            )
        if (
            not args.json
            and not args.no_interactive
            and not args.select
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        ):
            try:
                review = review_space(
                    args.path,
                    protection=ignore,
                    top=args.top,
                    allow_execution=args.yes,
                )
            except SpaceTUIUnavailable as exc:
                if args.yes:
                    print(
                        f"{exc}；为避免无审阅执行，请改用 "
                        "--no-interactive --select PATH。",
                        file=sys.stderr,
                    )
                    return 2
                print(f"{exc}；回退到行式只读导航。", file=sys.stderr)
                return run_space_browser(
                    args.path,
                    protection=ignore,
                    top=args.top,
                )
            except AnalyzeError as exc:
                return _fail(
                    args,
                    "analyze",
                    "invalid_path",
                    str(exc),
                )
            if review.cancelled:
                return 0
            selected = list(review.selected)
            cleanup = (
                execute_cleanup(selected, ignore)
                if args.yes and review.execution_confirmed
                else None
            )
            _print_space_tui_result(selected, cleanup)
            return 0 if cleanup is None or cleanup.complete else 1
        if args.yes and not args.select:
            return _fail(
                args,
                "analyze",
                "selection_required",
                "非交互 analyze 执行必须用 --select 精确指定当前层级路径。",
            )
        ctl = Control()
        try:
            analysis = analyze_path(
                args.path,
                protection=ignore,
                control=ctl,
            )
        except AnalyzeError as exc:
            return _fail(
                args,
                "analyze",
                "invalid_path",
                str(exc),
            )
        except KeyboardInterrupt:
            ctl.cancel()
            return _fail(
                args,
                "analyze",
                "cancelled",
                "已取消。",
                exit_code=130,
            )
        try:
            selected = _select_analyze_items(analysis, args.select)
        except SelectionError as exc:
            return _fail(
                args,
                "analyze",
                "selection_error",
                f"分析选择无效：{exc}",
            )
        cleanup = (
            execute_cleanup(selected, ignore) if args.yes else None
        )
        _print_analyze_report(
            analysis,
            args.json,
            args.top,
            selected=selected,
            cleanup=cleanup,
            redact_paths=args.redact_paths,
            path_seeds=tuple(raw_argv),
        )
        return 0 if (
            analysis.complete and (cleanup is None or cleanup.complete)
        ) else 1

    if args.cmd == "purge":
        if error := _validate_cleanup_execution_args(args):
            return _fail(
                args,
                "purge",
                "invalid_selection_options",
                f"purge：{error}。",
            )
        if args.path is not None:
            root = args.path.expanduser()
            if not root.exists() or not root.is_dir() or root.is_symlink():
                return _fail(
                    args,
                    "purge",
                    "invalid_path",
                    f"无效的扫描目录：{root}",
                )
            roots = [root]
            include_unmarked_roots = True
        else:
            roots = default_project_search_roots()
            include_unmarked_roots = False
        try:
            ignore = _load_ignore_rules(args)
        except KnowledgeBaseError as exc:
            return _fail(
                args,
                "purge",
                "rules_error",
                f"规则加载失败：{exc}",
            )
        ctl = Control()
        renderer = _progress_renderer(args.json)
        try:
            result = scan_project_artifacts(
                roots,
                ctl,
                ignore,
                max_depth=args.max_depth,
                include_unmarked_roots=include_unmarked_roots,
                on_progress=renderer,
            )
        except KeyboardInterrupt:
            ctl.cancel()
            return _fail(
                args,
                "purge",
                "cancelled",
                "已取消。",
                exit_code=130,
            )
        finally:
            if renderer is not None:
                renderer.finish()
        try:
            result, selected, execution_confirmed, cancelled = (
                _resolve_cleanup_selection(
                    result,
                    args,
                    _purge_review_groups(result),
                    title="Purge · 项目构建产物",
                )
            )
        except SelectionError as exc:
            return _fail(
                args,
                "purge",
                "selection_error",
                f"清理选择无效：{exc}",
            )
        if cancelled:
            return 0
        cleanup = (
            execute_cleanup(selected, ignore)
            if args.yes and execution_confirmed
            else None
        )
        _print_purge_report(
            result,
            args.json,
            cleanup,
            redact_paths=args.redact_paths,
            path_seeds=tuple(raw_argv),
        )
        return 0 if (
            result.complete and (cleanup is None or cleanup.complete)
        ) else 1

    if args.cmd == "ignore":
        store = RulesStore(args.rules)
        try:
            if args.ignore_cmd == "list":
                paths = store.list_ignored_paths()
                changed = None
            elif args.ignore_cmd == "add":
                changed = store.add_ignored_path(args.path)
                paths = store.list_ignored_paths()
            else:
                changed = store.remove_ignored_path(args.path)
                paths = store.list_ignored_paths()
        except KnowledgeBaseError as exc:
            return _fail(
                args,
                f"ignore {args.ignore_cmd}",
                "rules_error",
                f"规则更新失败：{exc}",
            )

        if args.json:
            payload = {
                "schema_version": CLI_SCHEMA_VERSION,
                "command": f"ignore {args.ignore_cmd}",
                "rules_path": str(store.path),
                "paths": [str(path) for path in paths],
            }
            if changed is not None:
                payload["changed"] = changed
            _print_json(
                payload,
                redact_paths=args.redact_paths,
                path_seeds=tuple(raw_argv),
            )
        elif args.ignore_cmd == "list":
            if paths:
                print("\n".join(str(path) for path in paths))
            else:
                print("忽略列表为空。")
        elif args.ignore_cmd == "add":
            message = "已加入忽略列表" if changed else "该路径已被现有规则覆盖"
            print(f"{message}：{normalize_path(args.path)}")
        else:
            message = "已从忽略列表移除" if changed else "未找到精确匹配的忽略路径"
            print(f"{message}：{normalize_path(args.path)}")
        return 0

    if args.cmd == "config":
        if args.update_knowledge is not None:
            if args.knowledge_public_key is None:
                return _fail(
                    args,
                    "config update-knowledge",
                    "missing_public_key",
                    "配置操作失败：--update-knowledge 必须同时指定 "
                    "--knowledge-public-key。",
                )
            try:
                update = update_knowledge_base(
                    args.update_knowledge,
                    args.knowledge_public_key,
                    destination=(
                        args.knowledge_path or DEFAULT_KNOWLEDGE_PATH
                    ),
                    allow_key_rotation=args.allow_key_rotation,
                )
            except KnowledgeUpdateError as exc:
                return _fail(
                    args,
                    "config update-knowledge",
                    "knowledge_update_error",
                    f"知识库更新失败：{exc}",
                )
            if args.json:
                _print_json({
                    "schema_version": CLI_SCHEMA_VERSION,
                    "command": "config update-knowledge",
                    "destination": str(update.destination),
                    "sequence": update.sequence,
                    "source_url": update.source_url,
                    "public_key_sha256": update.public_key_sha256,
                    "rules_sha256": update.rules_sha256,
                }, redact_paths=args.redact_paths, path_seeds=tuple(raw_argv))
            else:
                print(
                    f"托管知识库已更新到 sequence {update.sequence}："
                    f"{update.destination}"
                )
            return 0
        if (
            args.knowledge_public_key is not None
            or args.allow_key_rotation
            or args.knowledge_path is not None
        ):
            return _fail(
                args,
                "config",
                "invalid_config_options",
                "配置操作失败：知识库公钥和 key rotation 只能与 "
                "--update-knowledge 同时使用。",
            )
        store = ConfigStore(args.config_path)
        try:
            before = store.load()
            config = (
                store.set_analytics(args.analytics == "on")
                if args.analytics is not None
                else before
            )
        except ConfigError as exc:
            return _fail(
                args,
                "config",
                "config_error",
                f"配置操作失败：{exc}",
            )
        changed = config != before
        if args.json:
            _print_json({
                "schema_version": CLI_SCHEMA_VERSION,
                "command": "config",
                "changed": changed,
                "config_path": str(store.path),
                "analytics_enabled": config.analytics_enabled,
                "analytics_implemented": False,
            }, redact_paths=args.redact_paths, path_seeds=tuple(raw_argv))
        else:
            state = "on" if config.analytics_enabled else "off"
            print(f"Analytics 偏好：{state}")
            print("当前版本未实现遥测上传；该开关仅持久化本地偏好。")
        return 0

    if args.cmd == "cat":
        if args.json:
            _print_json({
                "schema_version": CLI_SCHEMA_VERSION,
                "command": "cat",
                "cat": CAT_ART,
            }, redact_paths=args.redact_paths, path_seeds=tuple(raw_argv))
        else:
            print(CAT_ART)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""在隔离临时目录中演示 openclean 的全部命令面。

该脚本不会扫描或修改真实 HOME。所有允许写入的演示都发生在
``TemporaryDirectory`` 中；外部 Docker daemon、网络知识库和特权帮助器不会被调用。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import plistlib
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest import mock

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]
if str(IMPLEMENTATION_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPLEMENTATION_ROOT))


@dataclass(frozen=True)
class PreviewResult:
    identifier: str
    command: str
    status: int
    expected_status: int
    passed: bool
    summary: str


def _write_file(path: Path, content: bytes = b"preview-data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _set_old(path: Path, *, days: int = 10) -> None:
    timestamp = time.time() - days * 86400
    os.utime(path, (timestamp, timestamp))


def _run_cli(cli_main, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            status = cli_main(arguments)
        except SystemExit as exc:
            status = int(exc.code or 0)
    return status, stdout.getvalue(), stderr.getvalue()


def _json_payload(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise TypeError("CLI JSON 顶层不是 object")
    return payload


def _record(
    results: list[PreviewResult],
    *,
    identifier: str,
    command: str,
    status: int,
    expected_status: int,
    condition: bool,
    summary: str,
) -> None:
    results.append(
        PreviewResult(
            identifier=identifier,
            command=command,
            status=status,
            expected_status=expected_status,
            passed=status == expected_status and condition,
            summary=summary,
        )
    )


def _prepare_fixtures(home: Path) -> dict[str, Path]:
    rules = home / "preview-rules.json"
    rules.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    _write_file(home / "Library" / "Caches" / "pip" / "wheel.bin")
    _write_file(home / ".cache" / "uv" / "archive.bin")
    _write_file(home / ".claude" / "cache" / "index.bin")
    _write_file(home / ".codex" / "tmp" / "session.tmp")
    _write_file(home / "Library" / "DemoSystemCache" / "cache.bin")

    trash = home / ".Trash"
    _write_file(trash / "old.tmp")
    trash.chmod(0o700)
    trash_decoy = home / ".PreviewVolumeTrash"
    _write_file(trash_decoy / "keep.tmp")

    project_root = home / "Projects" / "preview-project"
    _write_file(project_root / "pyproject.toml", b"[project]\nname='preview'\n")
    artifact_file = _write_file(project_root / ".venv" / "artifact.bin")
    _set_old(artifact_file)
    _set_old(artifact_file.parent)

    analyze_root = home / "Analyze"
    analyze_file = _write_file(analyze_root / "large-demo.bin", b"x" * 8192)

    applications = home / "Applications"
    app_contents = applications / "Preview.app" / "Contents"
    _write_file(
        app_contents / "Info.plist",
        plistlib.dumps({"CFBundleDevelopmentRegion": "en"}),
    )
    _write_file(
        app_contents / "Resources" / "en.lproj" / "Localizable.strings",
        b"english",
    )
    _write_file(
        app_contents / "Resources" / "de.lproj" / "Localizable.strings",
        b"german",
    )

    return {
        "rules": rules,
        "trash": trash,
        "trash_decoy": trash_decoy,
        "projects": home / "Projects",
        "project_root": project_root,
        "artifact": artifact_file.parent,
        "analyze_root": analyze_root,
        "analyze_file": analyze_file,
        "applications": applications,
        "system_cache": home / "Library" / "DemoSystemCache",
        "ignore_target": home / "KeepMe",
        "config": home / "preview-config.json",
    }


def _run_preview() -> tuple[list[PreviewResult], list[dict[str, str]]]:
    results: list[PreviewResult] = []
    with tempfile.TemporaryDirectory(prefix="openclean-preview-") as temporary:
        home = Path(temporary) / "home"
        home.mkdir()
        environment = {
            "HOME": str(home),
            "POETRY_CACHE_DIR": str(home / "Library" / "Caches" / "pypoetry"),
            "UV_CACHE_DIR": str(home / ".cache" / "uv"),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            # 默认配置路径在模块导入时展开，因此必须先隔离 HOME 再导入。
            from openclean.application_languages import (
                LanguagePreferencesDiscovery,
            )
            from openclean.cli import main as cli_main
            from openclean.macos import TrashDiscovery
            from openclean.models import ScanResult
            from openclean.processes import ProcessSnapshot
            from openclean.scanpoints import DOMAINS, ScanPoint

            paths = _prepare_fixtures(home)
            system_points = [
                ScanPoint(
                    "演示系统缓存",
                    (str(paths["system_cache"]),),
                    "safe",
                    "仅用于隔离 preview",
                ),
                ScanPoint(
                    "应用语言包",
                    (str(paths["applications"]),),
                    "critical",
                    "隔离应用语言审计",
                    scanner="application-languages",
                ),
            ]
            empty_processes = ProcessSnapshot(commands=())
            patches = (
                mock.patch.dict(DOMAINS, {"system": system_points}, clear=False),
                mock.patch(
                    "openclean.engine.capture_process_snapshot",
                    return_value=empty_processes,
                ),
                mock.patch(
                    "openclean.cleanup.capture_process_snapshot",
                    return_value=empty_processes,
                ),
                mock.patch(
                    "openclean.engine.scan_docker_resources",
                    return_value=ScanResult(),
                ),
                mock.patch(
                    "openclean.engine.discover_trash_paths",
                    return_value=TrashDiscovery(
                        paths=(paths["trash"], paths["trash_decoy"])
                    ),
                ),
                mock.patch(
                    "openclean.application_languages.discover_preferred_languages",
                    return_value=LanguagePreferencesDiscovery(languages=("en",)),
                ),
            )
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)

                status, stdout, _ = _run_cli(cli_main, ["--version"])
                _record(
                    results,
                    identifier="version",
                    command="openclean --version",
                    status=status,
                    expected_status=0,
                    condition=stdout.startswith("openclean "),
                    summary=stdout.strip(),
                )

                scan_args = [
                    "scan",
                    "--domain",
                    "system",
                    "--domain",
                    "developer",
                    "--domain",
                    "ai",
                    "--domain",
                    "trash",
                    "--domain",
                    "project",
                    "--project-root",
                    str(paths["projects"]),
                    "--rules",
                    str(paths["rules"]),
                    "--json",
                ]
                status, stdout, _ = _run_cli(cli_main, scan_args)
                scan_payload = _json_payload(stdout)
                scan_items = scan_payload.get("items", [])
                domains = {
                    item.get("domain")
                    for item in scan_items
                    if isinstance(item, dict)
                }
                _record(
                    results,
                    identifier="scan-all-domains",
                    command="openclean scan --domain <all> --json",
                    status=status,
                    expected_status=0,
                    condition={"system", "developer", "ai", "trash", "project"}
                    <= domains,
                    summary=f"隔离扫描得到 {len(scan_items)} 个候选，覆盖五域",
                )

                for category in ("junk", "dev", "ai", "trash"):
                    args = [
                        "clean",
                        category,
                        "--rules",
                        str(paths["rules"]),
                        "--no-interactive",
                        "--json",
                    ]
                    status, stdout, _ = _run_cli(cli_main, args)
                    payload = _json_payload(stdout)
                    categories = payload.get("categories", [])
                    items = [
                        item
                        for group in categories
                        if isinstance(group, dict)
                        for item in group.get("items", [])
                    ]
                    locked_languages = [
                        item
                        for item in items
                        if item.get("category") == "应用语言包"
                    ]
                    condition = (
                        payload.get("mode") == "preview"
                        and payload.get("cleanup") is None
                        and bool(items)
                    )
                    if category == "junk":
                        condition = condition and all(
                            not item.get("actionable")
                            and not item.get("preselected")
                            for item in locked_languages
                        )
                    _record(
                        results,
                        identifier=f"clean-{category}-preview",
                        command=f"openclean clean {category} --json",
                        status=status,
                        expected_status=0,
                        condition=condition,
                        summary=f"{category} 只读预览 {len(items)} 个候选",
                    )

                purge_args = [
                    "purge",
                    str(paths["projects"]),
                    "--rules",
                    str(paths["rules"]),
                    "--no-interactive",
                    "--json",
                ]
                status, stdout, _ = _run_cli(cli_main, purge_args)
                purge_payload = _json_payload(stdout)
                _record(
                    results,
                    identifier="purge-preview",
                    command="openclean purge <TEMP_PROJECTS> --json",
                    status=status,
                    expected_status=0,
                    condition=(
                        purge_payload.get("mode") == "preview"
                        and purge_payload.get("cleanup") is None
                        and bool(purge_payload.get("projects"))
                    ),
                    summary="项目产物按项目分组，只读预览成功",
                )

                analyze_args = [
                    "analyze",
                    str(paths["analyze_root"]),
                    "--rules",
                    str(paths["rules"]),
                    "--no-interactive",
                    "--json",
                ]
                status, stdout, _ = _run_cli(cli_main, analyze_args)
                analyze_payload = _json_payload(stdout)
                _record(
                    results,
                    identifier="analyze-preview",
                    command="openclean analyze <TEMP_PATH> --json",
                    status=status,
                    expected_status=0,
                    condition=(
                        analyze_payload.get("mode") == "browse"
                        and bool(analyze_payload.get("entries"))
                    ),
                    summary="一级空间分析、排序和卷信息预览成功",
                )

                lifecycle_statuses = []
                lifecycle_payloads = []
                for action in ("add", "list", "remove"):
                    args = ["ignore", action]
                    if action != "list":
                        args.append(str(paths["ignore_target"]))
                    args.extend(["--rules", str(paths["rules"]), "--json"])
                    status, stdout, _ = _run_cli(cli_main, args)
                    lifecycle_statuses.append(status)
                    lifecycle_payloads.append(_json_payload(stdout))
                _record(
                    results,
                    identifier="ignore-lifecycle",
                    command="openclean ignore add/list/remove <TEMP_PATH>",
                    status=max(lifecycle_statuses),
                    expected_status=0,
                    condition=(
                        lifecycle_payloads[0].get("changed") is True
                        and len(lifecycle_payloads[1].get("paths", [])) == 1
                        and lifecycle_payloads[2].get("changed") is True
                    ),
                    summary="忽略规则仅在临时 rules.json 中完成增查删",
                )

                config_statuses = []
                config_payloads = []
                for value in ("on", "off"):
                    status, stdout, _ = _run_cli(
                        cli_main,
                        [
                            "config",
                            "--analytics",
                            value,
                            "--config-path",
                            str(paths["config"]),
                            "--json",
                        ],
                    )
                    config_statuses.append(status)
                    config_payloads.append(_json_payload(stdout))
                _record(
                    results,
                    identifier="config-lifecycle",
                    command="openclean config --analytics on/off",
                    status=max(config_statuses),
                    expected_status=0,
                    condition=(
                        config_payloads[0].get("analytics_enabled") is True
                        and config_payloads[1].get("analytics_enabled") is False
                    ),
                    summary="analytics 偏好仅写入临时 0600 配置",
                )

                status, stdout, _ = _run_cli(cli_main, ["cat", "--json"])
                cat_payload = _json_payload(stdout)
                _record(
                    results,
                    identifier="cat",
                    command="openclean cat --json",
                    status=status,
                    expected_status=0,
                    condition=cat_payload.get("command") == "cat",
                    summary="终端猫 JSON 输出成功",
                )

                for category in ("junk", "dev", "ai"):
                    status, stdout, _ = _run_cli(
                        cli_main,
                        [
                            "clean",
                            category,
                            "--force",
                            "--yes",
                            "--no-interactive",
                            "--rules",
                            str(paths["rules"]),
                            "--json",
                        ],
                    )
                    payload = _json_payload(stdout)
                    cleanup = payload.get("cleanup") or {}
                    _record(
                        results,
                        identifier=f"clean-{category}-temp-execution",
                        command=f"openclean clean {category} --force --yes [TEMP ONLY]",
                        status=status,
                        expected_status=0,
                        condition=(
                            payload.get("mode") == "result"
                            and cleanup.get("complete") is True
                            and cleanup.get("moved_to_trash_bytes", 0) > 0
                        ),
                        summary="仅在 TemporaryDirectory 中移动到同卷 Trash",
                    )

                status, stdout, _ = _run_cli(
                    cli_main,
                    [
                        "clean",
                        "trash",
                        "--include-confirm",
                        "--select",
                        str(paths["trash"]),
                        "--yes",
                        "--no-interactive",
                        "--rules",
                        str(paths["rules"]),
                        "--json",
                    ],
                )
                trash_payload = _json_payload(stdout)
                trash_cleanup = trash_payload.get("cleanup") or {}
                _record(
                    results,
                    identifier="clean-trash-temp-execution",
                    command="openclean clean trash --yes [TEMP ONLY]",
                    status=status,
                    expected_status=0,
                    condition=(
                        trash_cleanup.get("complete") is True
                        and trash_cleanup.get("permanently_deleted_bytes", 0) > 0
                        and paths["trash"].is_dir()
                        and not any(paths["trash"].iterdir())
                        and (paths["trash_decoy"] / "keep.tmp").is_file()
                    ),
                    summary="精确清空一个临时 Trash；第二个 Trash 保持未选",
                )

                status, stdout, _ = _run_cli(
                    cli_main,
                    [*purge_args[:-1], "--force", "--yes", "--json"],
                )
                purge_result = _json_payload(stdout)
                purge_cleanup = purge_result.get("cleanup") or {}
                _record(
                    results,
                    identifier="purge-temp-execution",
                    command="openclean purge <TEMP_PROJECTS> --force --yes",
                    status=status,
                    expected_status=0,
                    condition=(
                        purge_cleanup.get("complete") is True
                        and purge_cleanup.get("moved_to_trash_bytes", 0) > 0
                        and not paths["artifact"].exists()
                    ),
                    summary="旧项目产物仅移动到临时同卷 Trash",
                )

                status, stdout, _ = _run_cli(
                    cli_main,
                    [
                        "analyze",
                        str(paths["analyze_root"]),
                        "--select",
                        str(paths["analyze_file"]),
                        "--yes",
                        "--no-interactive",
                        "--rules",
                        str(paths["rules"]),
                        "--json",
                    ],
                )
                analyze_result = _json_payload(stdout)
                analyze_cleanup = analyze_result.get("cleanup") or {}
                _record(
                    results,
                    identifier="analyze-temp-execution",
                    command="openclean analyze <TEMP_PATH> --select ... --yes",
                    status=status,
                    expected_status=0,
                    condition=(
                        analyze_cleanup.get("complete") is True
                        and analyze_cleanup.get("moved_to_trash_bytes", 0) > 0
                        and not paths["analyze_file"].exists()
                    ),
                    summary="精确选择项仅移动到临时同卷 Trash",
                )

                for target in ("ram", "purgeable"):
                    status, stdout, _ = _run_cli(
                        cli_main,
                        ["optimize", target, "--json"],
                    )
                    payload = _json_payload(stdout)
                    _record(
                        results,
                        identifier=f"optimize-{target}-guard",
                        command=f"openclean optimize {target} --json",
                        status=status,
                        expected_status=1,
                        condition=(
                            payload.get("status") == "unavailable"
                            and payload.get("executed") is False
                            and bool(payload.get("reason"))
                        ),
                        summary="无安全公开执行器时明确拒绝且非零退出",
                    )

    guarded = [
        {
            "capability": "SMAppService/XPC privileged cleanup",
            "status": "external-prerequisite",
            "reason": "需要完整 Xcode、签名身份、Team ID、host app 和真实安装验收",
        },
        {
            "capability": "optimize ram/purgeable",
            "status": "guarded-unavailable",
            "reason": "没有已验证、安全、公开的等价执行接口",
        },
        {
            "capability": "Docker prune",
            "status": "external-prerequisite",
            "reason": "真实执行需要用户自己的 Docker CLI/daemon；preview 不连接外部 daemon",
        },
        {
            "capability": "signed managed knowledge update",
            "status": "external-prerequisite",
            "reason": "真实更新需要项目 HTTPS channel 和用户钉住的正式公钥",
        },
        {
            "capability": "universal binary thinning",
            "status": "not-implemented",
            "reason": "修改签名应用风险高，当前不提供瘦身执行",
        },
    ]
    return results, guarded


def _print_human(
    results: list[PreviewResult], guarded: list[dict[str, str]]
) -> None:
    print("open-cleanmymac · 隔离功能预览")
    print("所有写操作均限制在 TemporaryDirectory；不会修改真实 HOME。")
    print("─" * 92)
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        print(
            f"{marker:<4}  {result.identifier:<34} "
            f"exit={result.status:<3} {result.summary}"
        )
    print("─" * 92)
    print("外部依赖或安全锁定能力")
    for item in guarded:
        print(f"- {item['capability']}: {item['status']} · {item['reason']}")
    passed = sum(result.passed for result in results)
    print(f"\n结果：{passed}/{len(results)} 个可执行预览场景通过。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在 TemporaryDirectory 中预览 openclean 全部命令面"
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    results, guarded = _run_preview()
    passed = all(result.passed for result in results)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "preview-all",
                    "workspace": "TemporaryDirectory",
                    "real_user_data_modified": False,
                    "passed": passed,
                    "scenario_count": len(results),
                    "scenarios": [asdict(result) for result in results],
                    "guarded_capabilities": guarded,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_human(results, guarded)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

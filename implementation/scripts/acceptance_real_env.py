"""一次性 macOS runner 上的真实环境验收脚本（acceptance.yml 专用）。

homebrew 套件启动真实 ``brew update``/``brew fetch`` 并向真实
``~/Library/Caches/Homebrew`` 写入合成文件；volumes 套件创建并挂载
APFS 磁盘镜像。两者都假定运行在用完即弃的虚拟机上，不应在真实
用户机器上执行；脚本因此要求显式设置 OPENCLEAN_REAL_ENV_ACCEPTANCE=1。
验收结论以对应 GitHub Actions 运行日志为准，不写入仓库文档。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openclean.cleanup import SelectionError, execute_cleanup, select_cleanup_items  # noqa: E402
from openclean.engine import IgnoreRules, scan_points, scan_project_artifacts  # noqa: E402
from openclean.homebrew import running  # noqa: E402
from openclean.macos import filesystem_case_sensitive  # noqa: E402
from openclean.processes import capture_process_snapshot  # noqa: E402
from openclean.scanpoints import DEVELOPER_JUNK  # noqa: E402

BREW_MARKERS = ("brew", "brew.rb", "brew.sh")
SECONDS_PER_DAY = 24 * 60 * 60

# zig 跨版本 build API 兼容：root_module 写法（0.13+）与旧参数写法各试一次。
ZIG_MAIN = (
    'const std = @import("std");\n'
    "pub fn main() !void {\n"
    '    std.debug.print("acceptance\\n", .{});\n'
    "}\n"
)
ZIG_BUILD_VARIANTS = (
    """const std = @import("std");
pub fn build(b: *std.Build) void {
    const module = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = b.graph.host,
        .optimize = .Debug,
    });
    const exe = b.addExecutable(.{ .name = "acceptance", .root_module = module });
    b.installArtifact(exe);
}
""",
    """const std = @import("std");
pub fn build(b: *std.Build) void {
    const exe = b.addExecutable(.{
        .name = "acceptance",
        .root_source_file = b.path("src/main.zig"),
        .target = b.host,
        .optimize = .Debug,
    });
    b.installArtifact(exe);
}
""",
)


class AcceptanceFailure(AssertionError):
    pass


def check(condition: bool, label: str, detail: str = "") -> None:
    message = f"{label}" + (f"：{detail}" if detail else "")
    if not condition:
        raise AcceptanceFailure(f"FAIL {message}")
    print(f"PASS {message}")


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(root), *args],
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_OPTIONAL_LOCKS": "0"},
        check=True, capture_output=True,
    )


def age_tree(root: Path, days: int = 10) -> None:
    stamp = time.time() - days * SECONDS_PER_DAY
    for directory, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            os.utime(Path(directory) / name, (stamp, stamp), follow_symlinks=False)
        os.utime(directory, (stamp, stamp))


def homebrew_item():
    point = next(p for p in DEVELOPER_JUNK if p.category == "Homebrew 缓存")
    result = scan_points([point], workers=1)
    items = result.items
    if len(items) != 1:
        raise AcceptanceFailure(f"Homebrew 缓存应恰好产生一个候选，实际 {len(items)}")
    return result, items[0]


def _attempt_brew_window(command: list[str]) -> bool:
    """运行一条真实 brew 命令，尝试在它运行期间完成阻断断言。"""
    with tempfile.TemporaryDirectory() as raw:
        with open(Path(raw) / "brew.log", "wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            detected = False
            while process.poll() is None:
                if running(capture_process_snapshot()):
                    detected = True
                    break
                time.sleep(0.2)
            if not detected:
                process.wait()
                return False
            result, item = homebrew_item()
            still_running = running(capture_process_snapshot())
            process.wait()
            if not still_running:
                return False
            check(not item.actionable, "blocked-during-real-brew",
                  f"command={' '.join(command)}")
            check(item.action_block_reason != "", "blocked-reason-present",
                  item.action_block_reason)
            check(any(marker in item.running_process_markers for marker in BREW_MARKERS),
                  "brew-process-markers", ",".join(item.running_process_markers))
            check(result.complete, "blocked-scan-complete")
            return True


def suite_homebrew() -> None:
    cache = Path.home() / "Library/Caches/Homebrew"
    seed = cache / "downloads/acceptance-seed.bin"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_bytes(b"real-env acceptance seed\n" * 64)
    try:
        result, item = homebrew_item()
        check(item.actionable, "baseline-actionable",
              f"size={item.size}B markers={item.running_process_markers}")
        check(result.complete, "baseline-scan-complete")

        succeeded = any(
            _attempt_brew_window(command)
            for command in (["brew", "update"], ["brew", "fetch", "--force", "git"])
        )
        if not succeeded:
            raise AcceptanceFailure(
                "FAIL brew-window：两次真实 brew 命令都未在运行期间完成扫描断言"
            )

        deadline = time.monotonic() + 30
        while running(capture_process_snapshot()):
            if time.monotonic() >= deadline:
                raise AcceptanceFailure("FAIL brew-exit：brew 进程 30 秒内未结束")
            time.sleep(0.5)
        result, item = homebrew_item()
        check(item.actionable, "recovered-actionable", item.action_block_reason)
        check(result.complete, "recovered-scan-complete")

        incomplete = cache / "downloads/acceptance.incomplete"
        incomplete.write_bytes(b"in-flight download fixture\n" * 64)
        try:
            result, item = homebrew_item()
            check(not item.actionable, "incomplete-blocked", item.action_block_reason)
            check("在途下载或锁" in item.action_block_reason, "incomplete-reason",
                  item.action_block_reason)
            check(result.complete, "incomplete-scan-complete")
        finally:
            incomplete.unlink()
        result, item = homebrew_item()
        check(item.actionable, "cleanup-restored", item.action_block_reason)
    finally:
        if seed.exists():
            seed.unlink()
    print("ACCEPT homebrew：真实 brew 并发与在途文件保护全部通过")


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise AcceptanceFailure(f"FAIL tool-missing：runner 缺少 {name}")


def _run_verbose(command: list[str], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if completed.returncode != 0:
        raise AcceptanceFailure(
            f"FAIL {' '.join(command)}：exit={completed.returncode} "
            f"stderr={completed.stderr.strip()[:500]}"
        )


def _attach_volume(image: Path, filesystem: str, volume_name: str, *, sparse: bool = True) -> Path:
    create = ["hdiutil", "create"]
    if sparse:
        create += ["-type", "SPARSE"]
    create += ["-fs", filesystem, "-size", "1g", "-volname", volume_name, "-o", str(image)]
    _run_verbose(create)
    # SPARSE 会在名称后追加 .sparseimage，UDIF 追加 .dmg；ExFAT 不支持 SPARSE。
    image_file = Path(f"{image}.sparseimage" if sparse else f"{image}.dmg")
    _run_verbose(["hdiutil", "attach", "-nobrowse", str(image_file)])
    mount = Path("/Volumes") / volume_name
    if not mount.is_dir():
        raise AcceptanceFailure(f"FAIL volume-mount：{mount} 挂载失败")
    return mount


def _detach_volume(mount: Path) -> None:
    for extra in ([], ["-force"]):
        completed = subprocess.run(
            ["hdiutil", "detach", str(mount), *extra], capture_output=True,
        )
        if completed.returncode == 0 or not mount.exists():
            return
    print(f"WARN volume-detach：{mount} 卸载失败，runner 销毁时自动释放", file=sys.stderr)


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _attach_exfat_volume(image: Path, volume_name: str) -> Path:
    """ExFAT 无法用 hdiutil -fs 直接创建；空白镜像 + diskutil eraseDisk 格式化。"""
    _run_verbose(["hdiutil", "create", "-size", "1g", "-o", str(image)])
    image_file = Path(f"{image}.dmg")
    attach = subprocess.run(
        ["hdiutil", "attach", "-nomount", str(image_file)], capture_output=True, text=True,
    )
    if attach.returncode != 0:
        raise AcceptanceFailure(
            f"FAIL hdiutil attach -nomount：{attach.stderr.strip()[:300]}"
        )
    devices = [
        line.split()[0] for line in attach.stdout.splitlines()
        if line.startswith("/dev/disk")
    ]
    if not devices:
        raise AcceptanceFailure(
            f"FAIL exfat-device：attach 输出无设备：{attach.stdout.strip()[:200]}"
        )
    device = devices[0]
    try:
        _run_verbose(["diskutil", "eraseDisk", "ExFAT", volume_name, device])
    except AcceptanceFailure:
        subprocess.run(["hdiutil", "detach", device, "-force"], capture_output=True)
        raise
    mount = Path("/Volumes") / volume_name
    if not mount.is_dir():
        raise AcceptanceFailure(f"FAIL volume-mount：{mount} 挂载失败")
    return mount


def _case_sensitive_branches(mount: Path) -> None:
    check(filesystem_case_sensitive(mount), "cs-volume-sensitive", str(mount))
    repo = mount / "repo"
    repo.mkdir()
    _write(repo / "package.json", "{}")
    git(repo, "init", "-q")

    alpha = repo / "Alpha"
    _write(alpha / "package.json", "{}")
    _write(alpha / "vendor/source.php", b"tracked source\n" * 64)
    git(repo, "add", "--", "Alpha/vendor/source.php")
    result = scan_project_artifacts([alpha])
    item, = result.items
    check(not item.actionable, "cs-exact-case-tracked-blocks", item.action_block_reason)
    check("Git 索引跟踪" in item.action_block_reason, "cs-exact-case-reason",
          item.action_block_reason)

    web = repo / "Packages/Web[1]"
    _write(web / "package.json", "{}")
    _write(web / "vendor/source.php", b"tracked source\n" * 64)
    git(repo, "add", "--", "Packages/Web[1]/vendor/source.php")
    (repo / "Packages").rename(repo / "packages")
    result = scan_project_artifacts([repo])
    target = next(
        (i for i in result.items if i.path == repo / "packages/Web[1]/vendor"), None,
    )
    if target is None:
        raise AcceptanceFailure(
            f"FAIL cs-lowercase-found：候选缺失，实际 {[str(i.path) for i in result.items]}"
        )
    check(target.actionable, "cs-different-case-not-merged", target.action_block_reason)
    check(target.action_block_reason == "", "cs-different-case-reason",
          target.action_block_reason)
    check(result.complete, "cs-scan-complete")


def _external_volume_trash(mount: Path) -> None:
    project = mount / "work/project"
    _write(project / "package.json", "{}")
    _write(project / "node_modules/payload.bin", b"cross-volume payload\n" * 64)
    artifact = project / "node_modules"
    age_tree(project)
    result = scan_project_artifacts([project])
    item, = result.items
    check(item.actionable and item.preselected, "trash-candidate-ready",
          f"actionable={item.actionable} preselected={item.preselected}")
    selected = select_cleanup_items(result.items)
    check(len(selected) == 1, "trash-selection-size", str(len(selected)))
    (mount / ".Trashes").mkdir(exist_ok=True)
    report = execute_cleanup(selected, IgnoreRules(), home=Path.home())
    outcome = report.outcomes[0]
    check(outcome.status == "moved_to_trash", "trash-moved", outcome.status + " " + outcome.message)
    expected_root = Path("/Volumes") / mount.name / ".Trashes" / str(os.getuid())
    check(outcome.destination is not None and expected_root in outcome.destination.parents,
          "trash-destination", str(outcome.destination))
    check(not artifact.exists(), "trash-source-removed", str(artifact))
    home_trash_copy = Path.home() / ".Trash/node_modules"
    check(not home_trash_copy.exists(), "trash-home-untouched", str(home_trash_copy))


def suite_volumes() -> None:
    if not subprocess.run(["which", "hdiutil"], capture_output=True).stdout:
        raise AcceptanceFailure("FAIL hdiutil-missing：runner 缺少 hdiutil")
    images: list[Path] = []
    mounts: list[Path] = []
    try:
        cs_image = Path(tempfile.gettempdir()) / "openclean-cs-acceptance"
        cs_mount = _attach_volume(cs_image, "Case-sensitive APFS", "openclean-cs")
        images.append(cs_image)
        mounts.append(cs_mount)
        _case_sensitive_branches(cs_mount)

        trash_image = Path(tempfile.gettempdir()) / "openclean-trash-acceptance"
        trash_mount = _attach_volume(trash_image, "APFS", "openclean-trash")
        images.append(trash_image)
        mounts.append(trash_mount)
        _external_volume_trash(trash_mount)
    finally:
        for mount in reversed(mounts):
            _detach_volume(mount)
        for image in images:
            for suffix in ("", ".sparseimage", ".dmg"):
                candidate = Path(str(image) + suffix)
                if candidate.exists():
                    candidate.unlink()
    print("ACCEPT volumes：case-sensitive APFS 与外置卷 .Trashes 语义全部通过")


def suite_zig() -> None:
    _require_tool("brew")
    if shutil.which("zig") is None:
        _run_verbose(["brew", "install", "zig"])
    _require_tool("zig")
    version = subprocess.run(
        ["zig", "version"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f"INFO zig {version}")
    with tempfile.TemporaryDirectory() as raw:
        # 执行链以夹具所在目录为 HOME：临时区位于 /private 下，沿用真实 HOME
        # 会命中"系统保护路径"的 fail-closed 拒绝（与单元测试的隔离 HOME 一致）。
        home = Path(raw).resolve()
        project = home / "project"
        project.mkdir()
        _write(project / "src/main.zig", ZIG_MAIN)
        completed = None
        for variant in ZIG_BUILD_VARIANTS:
            _write(project / "build.zig", variant)
            completed = subprocess.run(
                ["zig", "build"], cwd=project, capture_output=True, text=True,
            )
            if completed.returncode == 0:
                break
            print(f"WARN zig build 失败，换用兼容 API 重试：{completed.stderr.strip()[:200]}")
        if completed is None or completed.returncode != 0:
            raise AcceptanceFailure(
                f"FAIL zig-build：{completed.stderr.strip()[-400:] if completed else 'no run'}"
            )
        cache, out = project / ".zig-cache", project / "zig-out"
        check(cache.is_dir(), "zig-cache-created", str(cache))
        check(out.is_dir() and any(out.iterdir()), "zig-out-created", str(out))
        result = scan_project_artifacts([project])
        names = {item.artifact_name for item in result.items}
        check(names == {".zig-cache", "zig-out"}, "zig-artifacts-recognized",
              ",".join(sorted(names)))
        check(all(item.actionable for item in result.items), "zig-artifacts-actionable")
        check(result.complete, "zig-scan-complete")
        selected = select_cleanup_items(result.items, selectors=[str(cache), str(out)])
        check(len(selected) == 2, "zig-selection-size", str(len(selected)))
        report = execute_cleanup(selected, IgnoreRules(), home=home)
        statuses = {outcome.status for outcome in report.outcomes}
        check(statuses == {"moved_to_trash"}, "zig-moved", ",".join(sorted(statuses)))
        check((project / "build.zig").is_file() and (project / "src/main.zig").is_file(),
              "zig-sources-preserved")
        check(not cache.exists() and not out.exists(), "zig-artifacts-removed")
    print(f"ACCEPT zig：真实 zig {version} 构建产物识别与清理全部通过")


def suite_deriveddata() -> None:
    for tool in ("swift", "xcodebuild"):
        _require_tool(tool)
    with tempfile.TemporaryDirectory() as raw:
        # 同 zig 套件：夹具所在目录作为执行 HOME，避免 /private 临时区的保护性拒绝。
        home = Path(raw).resolve()
        project = home / "project"
        project.mkdir()
        _run_verbose(
            ["swift", "package", "init", "--type", "executable", "--name", "AcceptanceTool"],
            cwd=project,
        )
        check((project / "Package.swift").is_file(), "swift-package-created")
        derived = project / "DerivedData"
        attempts = (
            ["xcodebuild", "-scheme", "AcceptanceTool", "-derivedDataPath", str(derived), "build"],
            ["xcodebuild", "-scheme", "AcceptanceTool", "-destination", "platform=macOS",
             "-derivedDataPath", str(derived), "build"],
        )
        completed = None
        for attempt in attempts:
            completed = subprocess.run(
                attempt, cwd=project, capture_output=True, text=True,
            )
            if completed.returncode == 0:
                break
            print(f"WARN xcodebuild 失败，换参数重试：{completed.stderr.strip()[:200]}")
        if completed is None or completed.returncode != 0:
            raise AcceptanceFailure(
                f"FAIL xcodebuild：{completed.stderr.strip()[-400:] if completed else 'no run'}"
            )
        check(derived.is_dir() and any(derived.rglob("*")), "deriveddata-created", str(derived))
        result = scan_project_artifacts([project])
        items = [item for item in result.items if item.artifact_name == "DerivedData"]
        check(len(items) == 1, "deriveddata-recognized", str(len(items)))
        check(items[0].actionable, "deriveddata-actionable", items[0].action_block_reason)
        selected = select_cleanup_items(result.items, selectors=[str(derived)])
        check(len(selected) == 1, "deriveddata-selection-size", str(len(selected)))
        report = execute_cleanup(selected, IgnoreRules(), home=home)
        outcome = report.outcomes[0]
        check(outcome.status == "moved_to_trash", "deriveddata-moved",
              outcome.status + "：" + outcome.message[:120])
        check((project / "Package.swift").is_file() and (project / "Sources").is_dir(),
              "deriveddata-sources-preserved")
        check(not derived.exists(), "deriveddata-removed")
    print("ACCEPT deriveddata：真实 xcodebuild DerivedData 识别与清理全部通过")


def suite_exfat() -> None:
    _require_tool("hdiutil")
    image = Path(tempfile.gettempdir()) / "openclean-exfat-acceptance"
    mount: Path | None = None
    try:
        mount = _attach_exfat_volume(image, "openclean-exfat")
        check(not filesystem_case_sensitive(mount), "exfat-case-insensitive", str(mount))
        project = mount / "work/project"
        _write(project / "package.json", "{}")
        payload = project / "node_modules/payload.bin"
        _write(payload, b"exfat payload\n" * 512)
        age_tree(project)
        result = scan_project_artifacts([project])
        item, = result.items
        check(item.actionable, "exfat-artifact-actionable", item.action_block_reason)
        check(result.complete, "exfat-scan-complete")
        selected = select_cleanup_items(result.items)
        check(len(selected) == 1, "exfat-selection-size", str(len(selected)))
        (mount / ".Trashes").mkdir(exist_ok=True)
        report = execute_cleanup(selected, IgnoreRules(), home=Path.home())
        outcome = report.outcomes[0]
        if outcome.status == "moved_to_trash":
            check(not payload.exists() and outcome.destination is not None,
                  "exfat-moved", str(outcome.destination))
            print("INFO exfat-trash：成功移动到 ExFAT 卷 .Trashes（分支：moved）")
        else:
            check(payload.exists(), "exfat-fail-closed-source-intact",
                  outcome.status + "：" + outcome.message[:160])
            check(not (Path.home() / ".Trash/node_modules").exists(), "exfat-home-untouched")
            print(f"INFO exfat-trash：当前实现拒绝在 ExFAT 上移动（分支：{outcome.status}，"
                  f"{outcome.message[:160]}）")
    finally:
        if mount is not None:
            _detach_volume(mount)
        for suffix in ("", ".sparseimage", ".dmg"):
            candidate = Path(str(image) + suffix)
            if candidate.exists():
                candidate.unlink()
    print("ACCEPT exfat：ExFAT 卷扫描与安全语义通过（移动分支见上方 INFO）")


def suite_unicode() -> None:
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    with tempfile.TemporaryDirectory(prefix="openclean-unicode-") as raw:
        home = Path(raw).resolve()
        project = home / f"proj-{composed}"
        project.mkdir()
        _write(project / "package.json", "{}")
        artifact = project / "node_modules"
        _write(artifact / "payload.bin", b"unicode payload\n" * 64)
        age_tree(project)
        result = scan_project_artifacts([project])
        item, = result.items
        check(item.path == artifact, "unicode-path-roundtrip", str(item.path))
        selected = select_cleanup_items(result.items, selectors=[str(artifact)])
        check(len(selected) == 1, "unicode-exact-selection")
        decomposed_selector = project.with_name(
            project.name.replace(composed, decomposed),
        ) / "node_modules"
        check(decomposed_selector.exists(),
              "unicode-filesystem-normalization", "APFS 将 NFD 名称解析为同一路径")
        rejected = False
        try:
            select_cleanup_items(result.items, selectors=[str(decomposed_selector)])
        except SelectionError:
            rejected = True
        check(rejected, "unicode-decomposed-rejected",
              "NFD 形式选择器不得命中 NFC 候选（宁可拒绝不可错选）")
        report = execute_cleanup(selected, IgnoreRules(), home=home)
        check(report.outcomes[0].status == "moved_to_trash", "unicode-moved",
              report.outcomes[0].status)
        check((project / "package.json").is_file(), "unicode-sources-preserved")
    print("ACCEPT unicode：APFS 规范化路径的精确选择与拒绝语义全部通过")


SUITES = {
    "homebrew": suite_homebrew,
    "volumes": suite_volumes,
    "zig": suite_zig,
    "deriveddata": suite_deriveddata,
    "exfat": suite_exfat,
    "unicode": suite_unicode,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=(*SUITES, "all"), required=True)
    args = parser.parse_args(argv)
    if os.environ.get("OPENCLEAN_REAL_ENV_ACCEPTANCE") != "1":
        print(
            "拒绝执行：本脚本会修改真实 Homebrew 缓存并挂载磁盘镜像，"
            "只能在一次性 macOS runner 上运行（设置 OPENCLEAN_REAL_ENV_ACCEPTANCE=1）。",
            file=sys.stderr,
        )
        return 2
    suites = SUITES if args.suite == "all" else {args.suite: SUITES[args.suite]}
    for name, suite in suites.items():
        print(f"== suite {name} ==")
        suite()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as failure:
        print(str(failure), file=sys.stderr)
        raise SystemExit(1)

"""Caskroom 专用验收：仅供一次性 GitHub macOS runner 调用。

使用自有本地 tap、HTTP 归档和唯一 token 驱动真实 brew 安装/升级/回滚。
旧版本候选由已安装的测试载荷复制得到，用于可重复验证残留清理；
不声称该残留是 brew 正常升级产生的。所有下载内容都由本脚本生成。
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

from openclean.caskroom import scan_caskroom_versions
from openclean.cleanup import execute_cleanup
from openclean.engine import IgnoreRules


def require(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL caskroom/{label}: {detail}")
    print(f"PASS caskroom/{label}", flush=True)


def make_archive(root: Path, token: str, version: str, *, broken: bool = False) -> Path:
    payload = root / f"payload-{version}"
    binary = payload / "bin" / token
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    binary.chmod(0o755)
    manual = payload / "share/man/man1" / f"{token}.1"
    manual.parent.mkdir(parents=True)
    manual.write_text(f".TH {token} 1\n.SH VERSION\n{version}\n", encoding="utf-8")
    if broken:
        binary.rename(binary.with_name("missing-install-target"))
    archive = root / f"{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for directory in ("bin", "share"):
            stream.add(payload / directory, arcname=directory)
    return archive


def cask_source(token: str, version: str, archive: Path, base_url: str) -> str:
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    return f'''cask "{token}" do
  version "{version}"
  sha256 "{checksum}"
  url "{base_url}/{archive.name}"
  name "OpenClean acceptance fixture"
  desc "Disposable Caskroom acceptance fixture"
  homepage "https://github.com/BlueSkyXN/open-cleanmymac"
  binary "bin/{token}"
  manpage "share/man/man1/{token}.1"
end
'''


class ArchiveHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == self.server.gated_path:
            self.server.download_started.set()
            if not self.server.download_release.wait(timeout=90):
                self.send_error(504, "acceptance gate timed out")
                return
        super().do_GET()

    def log_message(self, *_args) -> None:
        pass


def run() -> None:
    require(os.environ.get("OPENCLEAN_REAL_ENV_ACCEPTANCE") == "1"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "macOS", "disposable-runner-only")
    brew = shutil.which("brew")
    require(brew is not None, "brew-available")
    token = "openclean-acceptance-" + uuid.uuid4().hex[:12]
    tap = "openclean/" + token
    qualified = f"{tap}/{token}"
    with tempfile.TemporaryDirectory(prefix="openclean-cask-acceptance-") as raw:
        root = Path(raw).resolve()
        home = root / "home"
        home.mkdir()
        environment = {
            "HOME": str(home), "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALL_CLEANUP": "1", "HOMEBREW_NO_ANALYTICS": "1",
            "HOMEBREW_CACHE": str(root / "cache"),
        }
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            _run_fixture(root, home, brew, token, tap, qualified)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _run_fixture(root: Path, home: Path, brew: str, token: str, tap: str, qualified: str) -> None:
    def command(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([brew, *args], text=True, capture_output=True, timeout=180)
        require((result.returncode == 0) == success, "brew-" + args[0],
                (result.stdout + result.stderr)[-2500:])
        return result

    prefix = Path(command("--prefix").stdout.strip()).resolve()
    print(command("--version").stdout.strip(), flush=True)
    caskroom = prefix / "Caskroom"
    token_root = caskroom / token
    require(not token_root.exists(), "unique-token")
    archives = {version: make_archive(root, token, version, broken=(version == "3.0"))
                for version in ("1.0", "2.0", "3.0")}
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(ArchiveHandler, directory=str(root)),
    )
    server.daemon_threads = True
    server.gated_path = ""
    server.download_started = threading.Event()
    server.download_release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tap_created = False
    try:
        command("tap-new", "--no-git", tap)
        tap_created = True
        tap_root = Path(command("--repository", tap).stdout.strip())
        definition = tap_root / "Casks" / f"{token}.rb"
        definition.parent.mkdir(exist_ok=True)
        base_url = f"http://127.0.0.1:{server.server_port}"

        def define(version: str) -> None:
            definition.write_text(cask_source(token, version, archives[version], base_url), encoding="utf-8")

        def scan_old():
            started = time.monotonic()
            result = scan_caskroom_versions(IgnoreRules(), roots=(caskroom,))
            print(f"Caskroom scan: {time.monotonic() - started:.2f}s", flush=True)
            require(result.complete, "scan-complete", repr(result.issues))
            old = next((item for item in result.items if item.path == token_root / "0.5"), None)
            require(old is not None, "synthetic-residue-visible")
            return old

        def assert_current(version: str) -> None:
            receipt = json.loads((token_root / ".metadata/INSTALL_RECEIPT.json").read_text())
            require(receipt["source"]["version"] == version, "receipt-" + version)
            for entry, source in ((prefix / "bin" / token, f"bin/{token}"),
                                  (prefix / "share/man/man1" / f"{token}.1", f"share/man/man1/{token}.1")):
                require(entry.is_symlink() and entry.resolve(strict=True) == token_root / version / source,
                        "current-link-" + source)

        define("1.0")
        command("install", "--cask", qualified)
        assert_current("1.0")
        # Homebrew 的正常升级会清理自己的旧版本；显式构造残留，保留真实回执和入口。
        shutil.copytree(token_root / "1.0", token_root / "0.5")
        old = scan_old()
        require(old.actionable, "idle-residue-actionable", old.action_block_reason)

        def upgrade(version: str, *, success: bool) -> None:
            define(version)
            server.gated_path = "/" + archives[version].name
            server.download_started.clear()
            server.download_release.clear()
            with (root / f"brew-{version}.log").open("w+") as log:
                process = subprocess.Popen([brew, "upgrade", "--cask", qualified], stdout=log, stderr=log)
                try:
                    require(server.download_started.wait(timeout=60), "real-upgrade-download-window")
                    require(process.poll() is None, "upgrade-still-running")
                    item = scan_old()
                    require(not item.actionable, "scan-blocks-live-brew", item.action_block_reason)
                    report = execute_cleanup([old], IgnoreRules(), home=home)
                    require(report.outcomes[0].status == "blocked" and old.path.exists(),
                            "execution-blocks-live-brew", report.outcomes[0].message)
                finally:
                    server.download_release.set()
                    try:
                        process.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise
                log.seek(0)
                require((process.returncode == 0) == success, "upgrade-result-" + version, log.read()[-2500:])

        upgrade("2.0", success=True)
        assert_current("2.0")
        old = scan_old()
        require(old.actionable, "after-upgrade-actionable", old.action_block_reason)
        # 3.0 缺少声明的 binary，触发真实安装失败与 Homebrew 回滚。
        upgrade("3.0", success=False)
        assert_current("2.0")
        require(not (token_root / "3.0").exists(), "failed-version-removed")
        old = scan_old()
        require(old.actionable, "after-rollback-actionable", old.action_block_reason)
        with (old.path / "bin" / token).open("rb"):
            held = scan_old()
            require(not held.actionable and bool(held.open_handle_count), "real-lsof-blocks")
            report = execute_cleanup([old], IgnoreRules(), home=home)
            require(report.outcomes[0].status == "blocked" and old.path.exists(), "live-handle-preserved")
        metadata = {path.relative_to(token_root): path.read_bytes()
                    for path in (token_root / ".metadata").rglob("*") if path.is_file()}
        report = execute_cleanup([scan_old()], IgnoreRules(), home=home)
        require(report.complete and report.outcomes[0].status == "moved_to_trash", "idle-residue-moved")
        require(report.outcomes[0].destination == home / ".Trash/0.5", "isolated-trash")
        require(all((token_root / path).read_bytes() == value for path, value in metadata.items()),
                "installation-metadata-preserved")
        assert_current("2.0")
        print("ACCEPT caskroom: real install, upgrade, rollback, live brew/lsof guards and synthetic residue cleanup")
    finally:
        server.download_release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        # 仅卸载本轮唯一 token；不会遍历或卸载 runner 的其它 cask。
        if token_root.exists():
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run([brew, "uninstall", "--cask", qualified], capture_output=True, timeout=120)
        if tap_created:
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run([brew, "untap", tap], capture_output=True, timeout=120)

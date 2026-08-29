"""审计本地 wheel/sdist 内容并输出可核验的 SHA-256。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import stat
import tarfile
import zipfile
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from email.policy import default as default_email_policy
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_VERSION = str(
    runpy.run_path(str(IMPLEMENTATION_ROOT / "openclean" / "__init__.py"))[
        "__version__"
    ]
)
PACKAGE_NAME = "open-cleanmymac"
LICENSE_EXPRESSION = "GPL-3.0-only"
LICENSE_NAME = "LICENSE"
LICENSE_CONTENT = (IMPLEMENTATION_ROOT / LICENSE_NAME).read_bytes()

FORBIDDEN_COMPONENTS = {
    "analysis",
    "local",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
}
FORBIDDEN_NAMES = {".DS_Store", ".env", ".coverage"}
SENSITIVE_NAME_PATTERNS = (
    ".env.*",
    "*.key",
    "*-private.pem",
    "private-*.pem",
    "private.pem",
    "*.p12",
    "*.pfx",
    "*.mobileprovision",
    "id_rsa*",
    "id_ed25519*",
)
PRIVATE_KEY_MARKERS = tuple(
    f"-----BEGIN {kind}PRIVATE KEY-----".encode("ascii")
    for kind in ("", "RSA ", "EC ", "OPENSSH ")
)
TOKEN_PATTERNS = (
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
)


@dataclass(frozen=True)
class ArtifactAudit:
    name: str
    kind: str
    sha256: str
    size_bytes: int
    member_count: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_safe_member(name: str) -> None:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"归档成员路径不安全：{name}")
    if FORBIDDEN_COMPONENTS.intersection(member.parts):
        raise ValueError(f"归档包含禁止目录：{name}")
    if member.name in FORBIDDEN_NAMES:
        raise ValueError(f"归档包含禁止文件：{name}")
    if any(fnmatch(member.name, pattern) for pattern in SENSITIVE_NAME_PATTERNS):
        raise ValueError(f"归档包含敏感文件名：{name}")


def _assert_safe_content(name: str, content: bytes) -> None:
    if any(marker in content for marker in PRIVATE_KEY_MARKERS):
        raise ValueError(f"归档成员包含私钥标记：{name}")
    if any(pattern.search(content) for pattern in TOKEN_PATTERNS):
        raise ValueError(f"归档成员包含高风险凭据模式：{name}")


def _assert_metadata(content: bytes, *, source: str) -> None:
    metadata = BytesParser(policy=default_email_policy).parsebytes(content)
    expected = {
        "Name": PACKAGE_NAME,
        "Version": PACKAGE_VERSION,
        "Requires-Python": ">=3.11",
        "License-Expression": LICENSE_EXPRESSION,
    }
    for field, expected_value in expected.items():
        if metadata.get(field) != expected_value:
            raise ValueError(
                f"{source} 的 {field} 不匹配："
                f"{metadata.get(field)!r} != {expected_value!r}"
            )
    if metadata.get_all("License-File", []) != [LICENSE_NAME]:
        raise ValueError(
            f"{source} 的 License-File 不匹配："
            f"{metadata.get_all('License-File', [])!r} != {[LICENSE_NAME]!r}"
        )


def _assert_zip_member(info: zipfile.ZipInfo) -> None:
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise ValueError(f"wheel 不允许 symlink：{info.filename}")


def _assert_tar_member(member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise ValueError(f"sdist 不允许 symlink/hardlink：{member.name}")


def _wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        for info in archive.infolist():
            _assert_zip_member(info)
            if not info.is_dir():
                _assert_safe_content(info.filename, archive.read(info))
        metadata_members = [
            name for name in members if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_members) != 1:
            raise ValueError("wheel 必须恰好包含一个 METADATA")
        _assert_metadata(
            archive.read(metadata_members[0]),
            source="wheel METADATA",
        )
        license_members = [
            name
            for name in members
            if name.endswith(f".dist-info/licenses/{LICENSE_NAME}")
        ]
        if len(license_members) != 1:
            raise ValueError("wheel 必须恰好包含一份 GPL LICENSE")
        if archive.read(license_members[0]) != LICENSE_CONTENT:
            raise ValueError("wheel 的 LICENSE 与仓库许可证不一致")
        entrypoint_members = [
            name
            for name in members
            if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entrypoint_members) != 1 or (
            b"openclean = openclean.cli:main"
            not in archive.read(entrypoint_members[0])
        ):
            raise ValueError("wheel 缺少 openclean console entry point")
    for member in members:
        _assert_safe_member(member)
        parts = PurePosixPath(member).parts
        if "tests" in parts or "scripts" in parts:
            raise ValueError(f"wheel 不应包含开发目录：{member}")
        if PurePosixPath(member).name == "openclean_cli.py":
            raise ValueError("wheel 不应包含 checkout 便捷入口 openclean_cli.py")
    if "openclean/cli.py" not in members:
        raise ValueError("wheel 缺少 openclean/cli.py")
    return members


def _sdist_members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        archive_members = archive.getmembers()
        members = [member.name for member in archive_members]
        for member in archive_members:
            _assert_tar_member(member)
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                _assert_safe_content(member.name, stream.read())
        package_info = [
            member
            for member in archive_members
            if (
                member.isfile()
                and PurePosixPath(member.name).name == "PKG-INFO"
                and len(PurePosixPath(member.name).parts) == 2
            )
        ]
        if len(package_info) != 1:
            raise ValueError("sdist 必须恰好包含一个根 PKG-INFO")
        stream = archive.extractfile(package_info[0])
        if stream is None:
            raise ValueError("无法读取 sdist PKG-INFO")
        _assert_metadata(stream.read(), source="sdist PKG-INFO")
        license_members = [
            member
            for member in archive_members
            if (
                member.isfile()
                and PurePosixPath(member.name).name == LICENSE_NAME
                and len(PurePosixPath(member.name).parts) == 2
            )
        ]
        if len(license_members) != 1:
            raise ValueError("sdist 必须恰好包含一份根 LICENSE")
        stream = archive.extractfile(license_members[0])
        if stream is None or stream.read() != LICENSE_CONTENT:
            raise ValueError("sdist 的 LICENSE 与仓库许可证不一致")
    for member in members:
        _assert_safe_member(member)
    relative = [PurePosixPath(name).parts[1:] for name in members]
    required = {
        ("LICENSE",),
        ("README.md",),
        ("TODO.md",),
        ("openclean_cli.py",),
        ("scripts", "preview_all.py"),
        ("scripts", "check_release_artifacts.py"),
    }
    missing = required.difference(relative)
    if missing:
        rendered = ", ".join("/".join(parts) for parts in sorted(missing))
        raise ValueError(f"sdist 缺少预期文件：{rendered}")
    if not any(parts[:1] == ("tests",) for parts in relative):
        raise ValueError("sdist 应包含 tests，供源码归档自验证")
    if not any(parts == ("openclean", "cli.py") for parts in relative):
        raise ValueError("sdist 缺少 openclean/cli.py")
    return members


def audit_dist(dist: Path) -> list[ArtifactAudit]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            "dist 必须恰好包含一个 wheel 和一个 sdist；"
            f"当前 wheel={len(wheels)}，sdist={len(sdists)}"
        )
    audits: list[ArtifactAudit] = []
    for path, kind, reader in (
        (wheels[0], "wheel", _wheel_members),
        (sdists[0], "sdist", _sdist_members),
    ):
        expected_prefix = f"open_cleanmymac-{PACKAGE_VERSION}"
        if not path.name.startswith(expected_prefix):
            raise ValueError(
                f"artifact 文件名版本不匹配：{path.name}"
            )
        members = reader(path)
        audits.append(
            ArtifactAudit(
                name=path.name,
                kind=kind,
                sha256=_sha256(path),
                size_bytes=path.stat().st_size,
                member_count=len(members),
            )
        )
    return audits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dist",
        help="构建产物目录；默认 implementation/dist",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    try:
        audits = audit_dist(args.dist)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        if args.json:
            print(json.dumps({
                "status": "error",
                "dist": str(args.dist),
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print(f"release artifact audit failed: {exc}")
        return 1

    payload = {
        "status": "passed",
        "dist": str(args.dist),
        "artifacts": [asdict(audit) for audit in audits],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("release artifact audit: PASS")
        for audit in audits:
            print(
                f"- {audit.name}: {audit.size_bytes} bytes, "
                f"{audit.member_count} members, sha256={audit.sha256}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

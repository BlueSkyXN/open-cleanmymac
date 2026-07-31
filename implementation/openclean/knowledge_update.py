"""显式 HTTPS + 公钥签名的托管知识库更新。"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .knowledge_base import (
    DEFAULT_KNOWLEDGE_PATH,
    KnowledgeBase,
    KnowledgeBaseError,
)
from .models import normalize_path

ENVELOPE_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "openssl-dgst-sha256"
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_PUBLIC_KEY_BYTES = 64 * 1024


class KnowledgeUpdateError(KnowledgeBaseError):
    pass


@dataclass(frozen=True)
class SignedKnowledgeEnvelope:
    sequence: int
    created_at: str
    rules: dict[str, Any]
    signature_algorithm: str
    signature: bytes
    signature_text: str
    canonical_payload: bytes


@dataclass(frozen=True)
class KnowledgeUpdateResult:
    destination: Path
    sequence: int
    source_url: str
    public_key_sha256: str
    rules_sha256: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_envelope_payload(
    sequence: int,
    created_at: str,
    rules: dict[str, Any],
    *,
    signature_algorithm: str = SIGNATURE_ALGORITHM,
) -> bytes:
    """返回发布方与客户端共同签名/验证的规范 JSON 字节。"""
    return _canonical_json(
        {
            "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
            "sequence": sequence,
            "created_at": created_at,
            "signature_algorithm": signature_algorithm,
            "rules": rules,
        }
    )


def _validate_https_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise KnowledgeUpdateError(f"知识库 URL 无效：{exc}") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise KnowledgeUpdateError("知识库更新只允许带主机名的 HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise KnowledgeUpdateError("知识库 URL 不能包含认证信息")
    if parsed.fragment:
        raise KnowledgeUpdateError("知识库 URL 不能包含 fragment")
    if port is not None and not 1 <= port <= 65535:
        raise KnowledgeUpdateError("知识库 URL 端口无效")
    return url


def _default_fetcher(url: str, maximum: int, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "openclean/knowledge"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _validate_https_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as exc:
                    raise KnowledgeUpdateError(
                        "知识库响应 Content-Length 无效"
                    ) from exc
                if declared < 0 or declared > maximum:
                    raise KnowledgeUpdateError("知识库响应超过大小上限")
            content = response.read(maximum + 1)
    except KnowledgeUpdateError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KnowledgeUpdateError(f"知识库下载失败：{exc}") from exc
    if len(content) > maximum:
        raise KnowledgeUpdateError("知识库响应超过大小上限")
    return content


def parse_signed_envelope(raw: bytes) -> SignedKnowledgeEnvelope:
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise KnowledgeUpdateError("知识库 envelope 超过大小上限")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise KnowledgeUpdateError("知识库 envelope 不是 UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeUpdateError(
            f"知识库 envelope 不是有效 JSON：{exc.lineno}:{exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise KnowledgeUpdateError("知识库 envelope 根对象必须是 JSON 对象")
    expected = {
        "envelope_schema_version",
        "sequence",
        "created_at",
        "signature_algorithm",
        "rules",
        "signature",
    }
    unknown = sorted(set(payload) - expected)
    missing = sorted(expected - set(payload))
    if unknown:
        raise KnowledgeUpdateError(
            f"知识库 envelope 包含未知字段：{', '.join(unknown)}"
        )
    if missing:
        raise KnowledgeUpdateError(
            f"知识库 envelope 缺少字段：{', '.join(missing)}"
        )
    version = payload["envelope_schema_version"]
    if type(version) is not int or version != ENVELOPE_SCHEMA_VERSION:
        raise KnowledgeUpdateError(
            f"不支持的 envelope_schema_version：{version!r}"
        )
    sequence = payload["sequence"]
    if type(sequence) is not int or sequence < 1:
        raise KnowledgeUpdateError("知识库 sequence 必须是正整数")
    created_at = payload["created_at"]
    if not isinstance(created_at, str) or not created_at:
        raise KnowledgeUpdateError("知识库 created_at 必须是非空字符串")
    try:
        timestamp = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise KnowledgeUpdateError("知识库 created_at 不是 ISO-8601 时间") from exc
    if timestamp.tzinfo is None:
        raise KnowledgeUpdateError("知识库 created_at 必须包含时区")
    algorithm = payload["signature_algorithm"]
    if algorithm != SIGNATURE_ALGORITHM:
        raise KnowledgeUpdateError(f"不支持的签名算法：{algorithm!r}")
    rules = payload["rules"]
    if not isinstance(rules, dict):
        raise KnowledgeUpdateError("知识库 rules 必须是 JSON 对象")
    if "_managed" in rules:
        raise KnowledgeUpdateError("远程 rules 不能包含内部 _managed 字段")
    signature_text = payload["signature"]
    if not isinstance(signature_text, str) or not signature_text:
        raise KnowledgeUpdateError("知识库 signature 必须是非空 base64 字符串")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise KnowledgeUpdateError("知识库 signature 不是有效 base64") from exc
    if not signature or len(signature) > 8192:
        raise KnowledgeUpdateError("知识库 signature 长度无效")
    return SignedKnowledgeEnvelope(
        sequence=sequence,
        created_at=created_at,
        rules=rules,
        signature_algorithm=algorithm,
        signature=signature,
        signature_text=signature_text,
        canonical_payload=canonical_envelope_payload(
            sequence,
            created_at,
            rules,
            signature_algorithm=algorithm,
        ),
    )


def _read_public_key(path: str | os.PathLike[str]) -> tuple[bytes, str]:
    source = normalize_path(path)
    try:
        source_stat = source.lstat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise KnowledgeUpdateError(f"无法检查知识库公钥 {source}：{exc}") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise KnowledgeUpdateError("知识库公钥必须是非符号链接普通文件")
    if source_stat.st_size <= 0 or source_stat.st_size > MAX_PUBLIC_KEY_BYTES:
        raise KnowledgeUpdateError("知识库公钥大小无效")
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise KnowledgeUpdateError(f"无法读取知识库公钥：{exc}") from exc
    return content, hashlib.sha256(content).hexdigest()


def _verify_with_openssl(
    payload: bytes,
    signature: bytes,
    public_key: bytes,
    *,
    openssl_path: str = "/usr/bin/openssl",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 5.0,
) -> None:
    run = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix="openclean-verify-") as temporary:
        directory = Path(temporary)
        payload_path = directory / "payload.json"
        signature_path = directory / "signature.bin"
        public_key_path = directory / "public-key.pem"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        public_key_path.write_bytes(public_key)
        payload_path.chmod(0o600)
        signature_path.chmod(0o600)
        public_key_path.chmod(0o600)
        command = [
            openssl_path,
            "dgst",
            "-sha256",
            "-verify",
            str(public_key_path),
            "-signature",
            str(signature_path),
            str(payload_path),
        ]
        try:
            completed = run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise KnowledgeUpdateError("未找到系统 OpenSSL 验签工具") from exc
        except subprocess.TimeoutExpired as exc:
            raise KnowledgeUpdateError("OpenSSL 验签超时") from exc
        except OSError as exc:
            raise KnowledgeUpdateError(f"无法运行 OpenSSL 验签：{exc}") from exc
    if completed.returncode != 0:
        raise KnowledgeUpdateError("知识库签名验证失败")


def _installed_metadata(destination: Path) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    try:
        raw = destination.read_bytes()
    except OSError as exc:
        raise KnowledgeUpdateError(f"无法读取现有托管知识库：{exc}") from exc
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise KnowledgeUpdateError("现有托管知识库超过大小上限")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeUpdateError("现有托管知识库格式无效，拒绝覆盖") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("_managed"), dict):
        raise KnowledgeUpdateError("现有知识库不是受管理文件，拒绝覆盖")
    metadata = payload["_managed"]
    sequence = metadata.get("sequence")
    fingerprint = metadata.get("public_key_sha256")
    if type(sequence) is not int or sequence < 1:
        raise KnowledgeUpdateError("现有托管知识库 sequence 无效")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise KnowledgeUpdateError("现有托管知识库公钥指纹无效")
    try:
        fingerprint_bytes = bytes.fromhex(fingerprint)
    except ValueError as exc:
        raise KnowledgeUpdateError("现有托管知识库公钥指纹无效") from exc
    if len(fingerprint_bytes) != 32:
        raise KnowledgeUpdateError("现有托管知识库公钥指纹无效")
    return metadata


@contextmanager
def _destination_lock(destination: Path) -> Iterator[None]:
    """对单个托管目标串行化 metadata 检查与原子替换。"""
    parent = destination.parent
    lock_path = destination.with_name(f"{destination.name}.lock")
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise KnowledgeUpdateError(f"无法准备托管知识库锁目录：{exc}") from exc

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise KnowledgeUpdateError("当前平台缺少 O_NOFOLLOW，无法安全创建知识库锁")
    flags = os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise KnowledgeUpdateError(f"无法打开托管知识库锁 {lock_path}：{exc}") from exc

    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise KnowledgeUpdateError("托管知识库锁必须是普通文件")
        if lock_stat.st_uid != os.geteuid():
            raise KnowledgeUpdateError("托管知识库锁不属于当前用户")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except KnowledgeUpdateError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise KnowledgeUpdateError(f"无法获取托管知识库锁 {lock_path}：{exc}") from exc

    try:
        yield
    finally:
        os.close(descriptor)


def _atomic_install(destination: Path, payload: dict[str, Any]) -> None:
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=parent,
        )
    except OSError as exc:
        raise KnowledgeUpdateError(f"无法准备托管知识库写入：{exc}") from exc
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_open = False
        with stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    except OSError as exc:
        raise KnowledgeUpdateError(
            f"无法写入托管知识库 {destination}：{exc}"
        ) from exc
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def update_knowledge_base(
    source_url: str,
    public_key: str | os.PathLike[str],
    *,
    destination: str | os.PathLike[str] = DEFAULT_KNOWLEDGE_PATH,
    allow_key_rotation: bool = False,
    fetcher: Callable[[str, int, float], bytes] | None = None,
    verifier: Callable[[bytes, bytes, bytes], None] | None = None,
    timeout: float = 10.0,
) -> KnowledgeUpdateResult:
    """下载、验签、防回滚并原子安装托管知识库。"""
    url = _validate_https_url(source_url)
    key_content, key_fingerprint = _read_public_key(public_key)
    fetch = fetcher or _default_fetcher
    raw = fetch(url, MAX_ENVELOPE_BYTES, timeout)
    if not isinstance(raw, bytes):
        raise KnowledgeUpdateError("知识库 fetcher 必须返回 bytes")
    envelope = parse_signed_envelope(raw)
    verify = verifier or _verify_with_openssl
    verify(envelope.canonical_payload, envelope.signature, key_content)
    try:
        KnowledgeBase.from_mapping(envelope.rules)
    except KnowledgeBaseError as exc:
        raise KnowledgeUpdateError(f"签名知识库规则无效：{exc}") from exc

    target = normalize_path(destination)
    canonical_rules = _canonical_json(envelope.rules)
    rules_sha256 = hashlib.sha256(canonical_rules).hexdigest()
    installed = dict(envelope.rules)
    installed["_managed"] = {
        "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
        "sequence": envelope.sequence,
        "created_at": envelope.created_at,
        "source_url": url,
        "signature_algorithm": envelope.signature_algorithm,
        "signature": envelope.signature_text,
        "public_key_sha256": key_fingerprint,
        "rules_sha256": rules_sha256,
    }
    with _destination_lock(target):
        existing = _installed_metadata(target)
        if existing is not None:
            previous_sequence = existing["sequence"]
            if envelope.sequence <= previous_sequence:
                raise KnowledgeUpdateError(
                    "拒绝知识库回滚或重放："
                    f"{envelope.sequence} <= {previous_sequence}"
                )
            previous_key = existing["public_key_sha256"]
            if previous_key != key_fingerprint and not allow_key_rotation:
                raise KnowledgeUpdateError(
                    "知识库公钥指纹已变化；需要显式允许 key rotation"
                )
        _atomic_install(target, installed)
    return KnowledgeUpdateResult(
        destination=target,
        sequence=envelope.sequence,
        source_url=url,
        public_key_sha256=key_fingerprint,
        rules_sha256=rules_sha256,
    )

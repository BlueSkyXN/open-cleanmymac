from __future__ import annotations

import base64
import contextlib
import fcntl
import io
import json
import multiprocessing
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.knowledge_base import KnowledgeBase
from openclean.knowledge_update import (
    MAX_ENVELOPE_BYTES,
    KnowledgeUpdateError,
    KnowledgeUpdateResult,
    canonical_envelope_payload,
    parse_signed_envelope,
    update_knowledge_base,
)


def _unsigned_envelope(sequence: int) -> bytes:
    canonical = canonical_envelope_payload(
        sequence,
        "2026-07-29T12:00:00+08:00",
        {"schema_version": 1},
    )
    envelope = json.loads(canonical)
    envelope["signature"] = base64.b64encode(b"test-signature").decode("ascii")
    return json.dumps(envelope).encode("utf-8")


def _concurrent_update_worker(
    destination: str,
    public_key: str,
    envelope: bytes,
    verified,
    outcomes,
) -> None:
    def verifier(*_) -> None:
        verified.set()

    try:
        result = update_knowledge_base(
            "https://updates.example.test/knowledge.json",
            public_key,
            destination=destination,
            fetcher=lambda *_: envelope,
            verifier=verifier,
        )
    except Exception as exc:  # noqa: BLE001 - child reports exact outcome
        outcomes.put(("error", type(exc).__name__, str(exc)))
    else:
        outcomes.put(("ok", result.sequence, ""))


class SignedEnvelopeFactory:
    def __init__(self, root: Path, name: str) -> None:
        self.private_key = root / f"{name}-private.pem"
        self.public_key = root / f"{name}-public.pem"
        subprocess.run(
            ["/usr/bin/openssl", "genrsa", "-out", str(self.private_key), "2048"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/openssl",
                "rsa",
                "-in",
                str(self.private_key),
                "-pubout",
                "-out",
                str(self.public_key),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

    def envelope(
        self,
        root: Path,
        sequence: int,
        rules: dict[str, object],
        *,
        created_at: str = "2026-07-29T12:00:00+08:00",
    ) -> bytes:
        canonical = canonical_envelope_payload(sequence, created_at, rules)
        payload_path = root / f"payload-{sequence}.json"
        signature_path = root / f"signature-{sequence}.bin"
        payload_path.write_bytes(canonical)
        subprocess.run(
            [
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(self.private_key),
                "-out",
                str(signature_path),
                str(payload_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        envelope = json.loads(canonical)
        envelope["signature"] = base64.b64encode(
            signature_path.read_bytes()
        ).decode("ascii")
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


class KnowledgeUpdateTests(unittest.TestCase):
    def test_real_signature_verification_and_private_atomic_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = SignedEnvelopeFactory(root, "primary")
            protected = root / "Protected"
            envelope = keys.envelope(
                root,
                1,
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                },
            )
            destination = root / "config" / "knowledge.json"
            calls = []

            def fetcher(url: str, maximum: int, timeout: float) -> bytes:
                calls.append((url, maximum, timeout))
                return envelope

            result = update_knowledge_base(
                "https://updates.example.test/knowledge.json",
                keys.public_key,
                destination=destination,
                fetcher=fetcher,
                timeout=3.5,
            )

            self.assertEqual(result.sequence, 1)
            self.assertEqual(result.destination, destination)
            self.assertEqual(
                calls,
                [
                    (
                        "https://updates.example.test/knowledge.json",
                        MAX_ENVELOPE_BYTES,
                        3.5,
                    )
                ],
            )
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertTrue(
                KnowledgeBase.load(destination).should_ignore_path(
                    protected / "child"
                )
            )
            installed = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(installed["_managed"]["sequence"], 1)
            self.assertEqual(
                installed["_managed"]["public_key_sha256"],
                result.public_key_sha256,
            )
            lock_path = destination.with_name(f"{destination.name}.lock")
            self.assertTrue(stat.S_ISREG(lock_path.stat().st_mode))
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(
                sorted(path.name for path in destination.parent.iterdir()),
                ["knowledge.json", "knowledge.json.lock"],
            )

    def test_tampered_signed_payload_is_rejected_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = SignedEnvelopeFactory(root, "primary")
            envelope = json.loads(
                keys.envelope(root, 1, {"schema_version": 1})
            )
            envelope["rules"]["protect"] = {
                "paths": [str(root / "Injected")]
            }
            destination = root / "knowledge.json"

            with self.assertRaisesRegex(KnowledgeUpdateError, "签名验证失败"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    keys.public_key,
                    destination=destination,
                    fetcher=lambda *_: json.dumps(envelope).encode(),
                )

            self.assertFalse(destination.exists())

    def test_sequence_prevents_rollback_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = SignedEnvelopeFactory(root, "primary")
            destination = root / "knowledge.json"
            first = keys.envelope(root, 2, {"schema_version": 1})
            update_knowledge_base(
                "https://updates.example.test/knowledge.json",
                keys.public_key,
                destination=destination,
                fetcher=lambda *_: first,
            )
            original = destination.read_bytes()
            lock_path = destination.with_name(f"{destination.name}.lock")
            lock_inode = lock_path.stat().st_ino

            for sequence in (1, 2):
                replay = keys.envelope(
                    root,
                    sequence,
                    {"schema_version": 1},
                )
                with self.assertRaisesRegex(
                    KnowledgeUpdateError,
                    "回滚或重放",
                ):
                    update_knowledge_base(
                        "https://updates.example.test/knowledge.json",
                        keys.public_key,
                        destination=destination,
                        fetcher=lambda *_, replay=replay: replay,
                    )

            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(lock_path.stat().st_ino, lock_inode)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_concurrent_updates_serialize_sequence_check_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_key = root / "public.pem"
            public_key.write_text("test public key", encoding="utf-8")
            destination = root / "knowledge.json"
            lock_path = destination.with_name(f"{destination.name}.lock")
            lock_path.touch(mode=0o600)

            lock_descriptor = os.open(lock_path, os.O_RDWR)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            context = multiprocessing.get_context("spawn")
            outcomes = context.Queue()
            verified = [context.Event(), context.Event()]
            processes = [
                context.Process(
                    target=_concurrent_update_worker,
                    args=(
                        str(destination),
                        str(public_key),
                        _unsigned_envelope(sequence),
                        verified[index],
                        outcomes,
                    ),
                )
                for index, sequence in enumerate((1, 2))
            ]
            try:
                for process in processes:
                    process.start()
                self.assertTrue(all(event.wait(5) for event in verified))
                for process in processes:
                    process.join(1)

                self.assertTrue(all(process.is_alive() for process in processes))
                self.assertFalse(destination.exists())
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
                for process in processes:
                    process.join(10)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)

            self.assertTrue(all(process.exitcode == 0 for process in processes))
            child_outcomes = [outcomes.get(timeout=2) for _ in processes]
            self.assertTrue(
                all(
                    outcome[0] == "ok" or "回滚或重放" in outcome[2]
                    for outcome in child_outcomes
                )
            )
            installed = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(installed["_managed"]["sequence"], 2)

    def test_key_rotation_requires_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = SignedEnvelopeFactory(root, "primary")
            replacement = SignedEnvelopeFactory(root, "replacement")
            destination = root / "knowledge.json"
            update_knowledge_base(
                "https://updates.example.test/knowledge.json",
                primary.public_key,
                destination=destination,
                fetcher=lambda *_: primary.envelope(
                    root, 1, {"schema_version": 1}
                ),
            )
            rotated = replacement.envelope(
                root,
                2,
                {"schema_version": 1},
            )

            with self.assertRaisesRegex(KnowledgeUpdateError, "key rotation"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    replacement.public_key,
                    destination=destination,
                    fetcher=lambda *_: rotated,
                )
            result = update_knowledge_base(
                "https://updates.example.test/knowledge.json",
                replacement.public_key,
                destination=destination,
                fetcher=lambda *_: rotated,
                allow_key_rotation=True,
            )

            self.assertEqual(result.sequence, 2)

    def test_signed_invalid_rules_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = SignedEnvelopeFactory(root, "primary")
            envelope = keys.envelope(
                root,
                1,
                {
                    "schema_version": 1,
                    "protect": {"paths": ["relative/path"]},
                },
            )
            destination = root / "knowledge.json"

            with self.assertRaisesRegex(KnowledgeUpdateError, "规则无效"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    keys.public_key,
                    destination=destination,
                    fetcher=lambda *_: envelope,
                )

            self.assertFalse(destination.exists())

    def test_https_and_public_key_file_requirements_fail_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "public.pem"
            key.write_text("key", encoding="utf-8")
            calls = []

            with self.assertRaisesRegex(KnowledgeUpdateError, "HTTPS"):
                update_knowledge_base(
                    "http://updates.example.test/knowledge.json",
                    key,
                    fetcher=lambda *_: calls.append("called") or b"{}",
                )
            link = root / "linked.pem"
            link.symlink_to(key)
            with self.assertRaisesRegex(KnowledgeUpdateError, "非符号链接"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    link,
                    fetcher=lambda *_: calls.append("called") or b"{}",
                )

            self.assertEqual(calls, [])

    def test_default_fetcher_rejects_https_downgrade_and_oversize_response(self) -> None:
        class Response:
            def __init__(self, final_url: str, length: int) -> None:
                self.final_url = final_url
                self.headers = {"Content-Length": str(length)}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self) -> str:
                return self.final_url

            def read(self, _: int) -> bytes:
                return b"{}"

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "public.pem"
            key.write_text("public key", encoding="utf-8")
            with mock.patch(
                "openclean.knowledge_update.urllib.request.urlopen",
                return_value=Response("http://updates.example.test/file", 2),
            ), self.assertRaisesRegex(KnowledgeUpdateError, "HTTPS"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    key,
                )
            with mock.patch(
                "openclean.knowledge_update.urllib.request.urlopen",
                return_value=Response(
                    "https://updates.example.test/file",
                    MAX_ENVELOPE_BYTES + 1,
                ),
            ), self.assertRaisesRegex(KnowledgeUpdateError, "大小上限"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    key,
                )

    def test_unmanaged_destination_and_atomic_replace_failure_preserve_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = SignedEnvelopeFactory(root, "primary")
            destination = root / "knowledge.json"
            destination.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            original = destination.read_bytes()

            with self.assertRaisesRegex(KnowledgeUpdateError, "不是受管理文件"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    keys.public_key,
                    destination=destination,
                    fetcher=lambda *_: keys.envelope(
                        root, 1, {"schema_version": 1}
                    ),
                )
            self.assertEqual(destination.read_bytes(), original)

            destination.unlink()
            update_knowledge_base(
                "https://updates.example.test/knowledge.json",
                keys.public_key,
                destination=destination,
                fetcher=lambda *_: keys.envelope(
                    root, 1, {"schema_version": 1}
                ),
            )
            installed = destination.read_bytes()
            with mock.patch(
                "openclean.knowledge_update.os.replace",
                side_effect=OSError("replace failed"),
            ), self.assertRaisesRegex(KnowledgeUpdateError, "无法写入"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    keys.public_key,
                    destination=destination,
                    fetcher=lambda *_: keys.envelope(
                        root, 2, {"schema_version": 1}
                    ),
                )

            self.assertEqual(destination.read_bytes(), installed)
            lock_path = destination.with_name(f"{destination.name}.lock")
            self.assertTrue(lock_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(
                sorted(path.name for path in root.iterdir() if path.name.startswith(".knowledge")),
                [],
            )

    def test_symlink_lock_file_fails_closed_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_key = root / "public.pem"
            public_key.write_text("test public key", encoding="utf-8")
            destination = root / "knowledge.json"
            lock_target = root / "unrelated.lock"
            lock_target.write_text("keep", encoding="utf-8")
            lock_path = destination.with_name(f"{destination.name}.lock")
            lock_path.symlink_to(lock_target)

            with self.assertRaisesRegex(KnowledgeUpdateError, "知识库锁"):
                update_knowledge_base(
                    "https://updates.example.test/knowledge.json",
                    public_key,
                    destination=destination,
                    fetcher=lambda *_: _unsigned_envelope(1),
                    verifier=lambda *_: None,
                )

            self.assertFalse(destination.exists())
            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(lock_target.read_text(encoding="utf-8"), "keep")


class SignedEnvelopeValidationTests(unittest.TestCase):
    def _base(self) -> dict[str, object]:
        return {
            "envelope_schema_version": 1,
            "sequence": 1,
            "created_at": "2026-07-29T12:00:00+08:00",
            "signature_algorithm": "openssl-dgst-sha256",
            "rules": {"schema_version": 1},
            "signature": base64.b64encode(b"signature").decode(),
        }

    def test_rejects_unknown_missing_and_invalid_security_fields(self) -> None:
        cases = []
        unknown = self._base()
        unknown["extra"] = True
        cases.append((unknown, "未知字段"))
        missing = self._base()
        del missing["sequence"]
        cases.append((missing, "缺少字段"))
        no_timezone = self._base()
        no_timezone["created_at"] = "2026-07-29T12:00:00"
        cases.append((no_timezone, "包含时区"))
        algorithm = self._base()
        algorithm["signature_algorithm"] = "none"
        cases.append((algorithm, "签名算法"))
        signature = self._base()
        signature["signature"] = "not-base64!"
        cases.append((signature, "base64"))
        managed = self._base()
        managed["rules"] = {"schema_version": 1, "_managed": {}}
        cases.append((managed, "不能包含"))

        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KnowledgeUpdateError,
                message,
            ):
                parse_signed_envelope(json.dumps(payload).encode())


class KnowledgeUpdateCliTests(unittest.TestCase):
    def test_explicit_update_reports_verified_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "public.pem"
            destination = root / "knowledge.json"
            key.write_text("public key", encoding="utf-8")
            update = KnowledgeUpdateResult(
                destination=destination,
                sequence=7,
                source_url="https://updates.example.test/knowledge.json",
                public_key_sha256="a" * 64,
                rules_sha256="b" * 64,
            )
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.update_knowledge_base",
                return_value=update,
            ) as updater, contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "config",
                        "--update-knowledge",
                        update.source_url,
                        "--knowledge-public-key",
                        str(key),
                        "--knowledge-path",
                        str(destination),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["sequence"], 7)
            self.assertEqual(payload["public_key_sha256"], "a" * 64)
            updater.assert_called_once_with(
                update.source_url,
                key,
                destination=destination,
                allow_key_rotation=False,
            )

    def test_update_requires_key_and_rotation_is_update_only(self) -> None:
        missing_key_stderr = io.StringIO()
        with contextlib.redirect_stderr(missing_key_stderr):
            missing_key = main(
                [
                    "config",
                    "--update-knowledge",
                    "https://updates.example.test/knowledge.json",
                ]
            )
        rotation_stderr = io.StringIO()
        with contextlib.redirect_stderr(rotation_stderr):
            rotation = main(["config", "--allow-key-rotation"])

        self.assertEqual(missing_key, 2)
        self.assertIn("--knowledge-public-key", missing_key_stderr.getvalue())
        self.assertEqual(rotation, 2)
        self.assertIn("--update-knowledge", rotation_stderr.getvalue())

    def test_update_failure_is_nonzero_and_does_not_fall_through_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "public.pem"
            key.write_text("public key", encoding="utf-8")
            stderr = io.StringIO()

            with mock.patch(
                "openclean.cli.update_knowledge_base",
                side_effect=KnowledgeUpdateError("bad signature"),
            ), mock.patch(
                "openclean.cli.ConfigStore"
            ) as config_store, contextlib.redirect_stderr(stderr):
                status = main(
                    [
                        "config",
                        "--update-knowledge",
                        "https://updates.example.test/knowledge.json",
                        "--knowledge-public-key",
                        str(key),
                    ]
                )

            self.assertEqual(status, 2)
            self.assertIn("bad signature", stderr.getvalue())
            config_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()

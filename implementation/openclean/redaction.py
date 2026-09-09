"""JSON 输出的单文档路径脱敏。"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PATH_VALUE_KEYS = frozenset({
    "path",
    "paths",
    "root",
    "project_root",
    "cleanup_root",
    "startup_program",
    "destination",
    "rules_path",
    "config_path",
    "mount_point",
    "display_path",
    "home",
    "run_store",
    "roots",
    "running_process_markers",
})
FREE_TEXT_KEYS = frozenset(
    {"message", "note", "action_block_reason", "summary", "do_not_do", "block_reasons"}
)
# run_id/finding_id 不是路径形态，不会被路径 ref 替换；为保证脱敏输出
# 不可 replay（AR-03 §7 / 决策 5），单独把这些 actionable ID 换成不可用占位。
ACTIONABLE_ID_KEYS = frozenset({"run_id", "finding_id", "finding_ids"})
REDACTION_METADATA = {
    "enabled": True,
    "scheme": "opaque-path-ref-v1",
    "scope": "single-document",
    "selection_replayable": False,
}
_ACTIONABLE_ID_PATTERN = re.compile(r"\b(run|finding):[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9/])/(?![/\s])")
_TILDE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_~])~(?:[A-Za-z0-9._-]+)?/(?!\s)"
)
_PATH_URI_PATTERN = re.compile(r"(?:file|unix):///", re.IGNORECASE)
_PATH_REF_CONTINUATION_PATTERN = re.compile(r"path:\d{4,}(?=[^\d\s])")


def _canonical_absolute_path(value: str) -> str | None:
    candidate = value
    if "=" in candidate:
        _, possible_path = candidate.split("=", 1)
        if possible_path.startswith(("/", "~")):
            candidate = possible_path
    if not candidate.startswith(("/", "~")):
        return None
    expanded = os.path.expanduser(candidate)
    if not os.path.isabs(expanded):
        return None
    return os.path.normpath(expanded)


class JsonPathRedactor:
    """把一份 JSON 文档内的绝对路径映射成稳定 opaque ref。"""

    def __init__(self, seeds: Iterable[str] = ()) -> None:
        self._aliases: dict[str, str] = {}
        self._references: dict[str, str] = {}
        for seed in (str(Path.home()), str(Path.cwd()), *seeds):
            self._remember(seed)

    def _remember(self, value: object) -> None:
        if not isinstance(value, str):
            return
        canonical = _canonical_absolute_path(value)
        if canonical is None:
            return
        self._aliases[value] = canonical
        self._aliases[canonical] = canonical

    def _collect(self, value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if child_key in PATH_VALUE_KEYS:
                    if isinstance(child_value, (list, tuple)):
                        for entry in child_value:
                            self._remember(entry)
                    else:
                        self._remember(child_value)
                self._collect(child_value, child_key)
            return
        if isinstance(value, (list, tuple)):
            for entry in value:
                self._collect(entry, key)

    def _assign_references(self) -> None:
        canonicals = sorted(set(self._aliases.values()))
        self._references = {
            canonical: f"path:{index:04d}"
            for index, canonical in enumerate(canonicals, start=1)
        }
        # Aliases stay fixed during a document; longer paths must be replaced first.
        self._replacements = sorted(
            (
                (alias, self._references[canonical])
                for alias, canonical in self._aliases.items()
                if alias != "/"
            ),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )

    def _reference(self, value: str) -> str | None:
        canonical = _canonical_absolute_path(value)
        if canonical is None:
            return None
        return self._references.get(canonical)

    def _replace_known_paths(self, value: str) -> str:
        redacted = value
        for original, reference in self._replacements:
            redacted = redacted.replace(original, reference)
        return redacted

    def _transform(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            transformed = {
                child_key: self._transform(child_value, child_key)
                for child_key, child_value in value.items()
            }
            original_path = value.get("path")
            if (
                isinstance(original_path, str)
                and isinstance(value.get("name"), str)
            ):
                reference = self._reference(original_path)
                if reference is not None:
                    transformed["name"] = reference
            return transformed
        if isinstance(value, (list, tuple)):
            return [self._transform(entry, key) for entry in value]
        if not isinstance(value, str):
            return value
        if key in ACTIONABLE_ID_KEYS:
            return f"{value.split(':', 1)[0]}:redacted"
        if key in PATH_VALUE_KEYS:
            reference = self._reference(value)
            if reference is not None:
                return reference
        redacted = _ACTIONABLE_ID_PATTERN.sub(r"\1:redacted", self._replace_known_paths(value))
        if key in FREE_TEXT_KEYS and (
            _ABSOLUTE_PATH_PATTERN.search(redacted)
            or _TILDE_PATH_PATTERN.search(redacted)
            or _PATH_URI_PATTERN.search(redacted)
            or _PATH_REF_CONTINUATION_PATTERN.search(redacted)
        ):
            return "[path details redacted]"
        return redacted

    def redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._collect(payload)
        self._assign_references()
        transformed = self._transform(payload)
        assert isinstance(transformed, dict)
        transformed["redaction"] = dict(REDACTION_METADATA)
        return transformed


def redact_json_payload(
    payload: dict[str, Any],
    *,
    path_seeds: Iterable[str] = (),
) -> dict[str, Any]:
    return JsonPathRedactor(path_seeds).redact(payload)

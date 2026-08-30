"""独立规则知识库。

规则数据采用项目自建的 JSON schema，不读取或兼容任何第三方私有规则格式。
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .models import normalize_path

DEFAULT_RULES_PATH = Path("~/.config/openclean/rules.json").expanduser()
DEFAULT_KNOWLEDGE_PATH = Path(
    "~/.config/openclean/knowledge.json"
).expanduser()
SCHEMA_VERSION = 1


class KnowledgeBaseError(ValueError):
    """知识库读取或校验失败。"""


class RulesFileNotFoundError(KnowledgeBaseError):
    pass


@dataclass(frozen=True)
class RuleMatch:
    kind: str
    matcher: str
    pattern: str


@dataclass(frozen=True)
class ApplicationRule:
    bundle_id: str
    name: str = ""
    protected: bool = False
    additional_files: tuple[Path, ...] = ()
    deep_search: bool = False


@dataclass(frozen=True)
class _PathRules:
    paths: tuple[Path, ...] = ()
    globs: tuple[str, ...] = ()
    regexes: tuple[re.Pattern[str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.paths or self.globs or self.regexes)

    def merge(self, overrides: _PathRules) -> _PathRules:
        paths = tuple(dict.fromkeys((*self.paths, *overrides.paths)))
        globs = tuple(dict.fromkeys((*self.globs, *overrides.globs)))
        regex_values = tuple(
            dict.fromkeys(
                pattern.pattern
                for pattern in (*self.regexes, *overrides.regexes)
            )
        )
        return _PathRules(
            paths=paths,
            globs=globs,
            regexes=tuple(re.compile(pattern) for pattern in regex_values),
        )

    def match(self, path: Path, kind: str) -> RuleMatch | None:
        candidate = str(path)

        for root in self.paths:
            try:
                if os.path.commonpath((candidate, str(root))) == str(root):
                    return RuleMatch(kind=kind, matcher="path", pattern=str(root))
            except ValueError:
                continue

        for pattern in self.globs:
            if fnmatch.fnmatchcase(candidate, pattern):
                return RuleMatch(kind=kind, matcher="glob", pattern=pattern)

        for pattern in self.regexes:
            if pattern.search(candidate):
                return RuleMatch(kind=kind, matcher="regex", pattern=pattern.pattern)
        return None


@dataclass(frozen=True)
class KnowledgeBase:
    """向扫描策略提供忽略、保护和应用附加文件查询。"""

    ignored: _PathRules = field(default_factory=_PathRules)
    protected: _PathRules = field(default_factory=_PathRules)
    applications: dict[str, ApplicationRule] = field(default_factory=dict)
    source: Path | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def has_path_rules(self) -> bool:
        return not (self.ignored.is_empty and self.protected.is_empty)

    @classmethod
    def empty(cls) -> KnowledgeBase:
        return cls()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> KnowledgeBase:
        source = normalize_path(path)
        try:
            raw = source.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RulesFileNotFoundError(f"规则文件不存在：{source}") from exc
        except OSError as exc:
            raise KnowledgeBaseError(f"无法读取规则文件 {source}：{exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KnowledgeBaseError(
                f"规则文件不是有效 JSON：{source}:{exc.lineno}:{exc.colno}: {exc.msg}"
            ) from exc
        return cls.from_mapping(payload, source=source)

    @classmethod
    def load_configured(
        cls, explicit_path: str | os.PathLike[str] | None = None
    ) -> KnowledgeBase:
        if explicit_path is not None:
            return cls.load(explicit_path)
        managed = (
            cls.load(DEFAULT_KNOWLEDGE_PATH)
            if DEFAULT_KNOWLEDGE_PATH.exists()
            else cls.empty()
        )
        user = (
            cls.load(DEFAULT_RULES_PATH)
            if DEFAULT_RULES_PATH.exists()
            else cls.empty()
        )
        return managed.merge(user)

    @classmethod
    def from_mapping(
        cls, payload: Any, source: Path | None = None
    ) -> KnowledgeBase:
        root = _require_mapping(payload, "根对象")
        _reject_unknown(
            root,
            {
                "schema_version",
                "ignore",
                "protect",
                "applications",
                "_managed",
            },
            "根对象",
        )
        if "_managed" in root:
            _require_mapping(root["_managed"], "_managed")

        version = root.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise KnowledgeBaseError(
                f"仅支持 schema_version={SCHEMA_VERSION}，实际为 {version!r}"
            )

        ignored = _parse_path_rules(root.get("ignore", {}), "ignore")
        protected = _parse_path_rules(root.get("protect", {}), "protect")
        applications = _parse_applications(root.get("applications", {}))
        return cls(
            ignored=ignored,
            protected=protected,
            applications=applications,
            source=source,
            schema_version=version,
        )

    def match_path(self, path: str | os.PathLike[str]) -> RuleMatch | None:
        if not self.has_path_rules:
            return None
        normalized = normalize_path(path)
        return (
            self.protected.match(normalized, "protect")
            or self.ignored.match(normalized, "ignore")
        )

    def should_ignore_path(self, path: str | os.PathLike[str]) -> bool:
        return self.match_path(path) is not None

    def should_ignore_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "file":
            return False
        return self.should_ignore_path(unquote(parsed.path))

    def is_system_item(self, path: str | os.PathLike[str]) -> bool:
        return self.protected.match(normalize_path(path), "protect") is not None

    def is_app_protected(self, bundle_id: str) -> bool:
        rule = self.applications.get(bundle_id)
        return rule.protected if rule is not None else False

    def additional_files(
        self, application_name: str, bundle_id: str
    ) -> tuple[Path, ...]:
        rule = self.applications.get(bundle_id)
        if rule is None:
            return ()
        if rule.name and application_name and rule.name != application_name:
            return ()
        return rule.additional_files

    def is_deep_search_needed(self, bundle_id: str) -> bool:
        rule = self.applications.get(bundle_id)
        return rule.deep_search if rule is not None else False

    def application_name(self, bundle_id: str) -> str | None:
        rule = self.applications.get(bundle_id)
        if rule is None:
            return None
        return rule.name or None

    def merge(self, overrides: KnowledgeBase) -> KnowledgeBase:
        applications = dict(self.applications)
        for bundle_id, override in overrides.applications.items():
            managed = applications.get(bundle_id)
            if managed is None:
                applications[bundle_id] = override
                continue
            applications[bundle_id] = ApplicationRule(
                bundle_id=bundle_id,
                name=override.name or managed.name,
                protected=managed.protected or override.protected,
                additional_files=tuple(
                    dict.fromkeys(
                        (*managed.additional_files, *override.additional_files)
                    )
                ),
                deep_search=managed.deep_search or override.deep_search,
            )
        return KnowledgeBase(
            ignored=self.ignored.merge(overrides.ignored),
            protected=self.protected.merge(overrides.protected),
            applications=applications,
            source=overrides.source or self.source,
            schema_version=SCHEMA_VERSION,
        )


class RulesStore:
    """用户忽略路径的本地 JSON 存储；写入采用同目录原子替换。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = normalize_path(path or DEFAULT_RULES_PATH)

    def list_ignored_paths(self) -> tuple[Path, ...]:
        payload = self._load_payload(missing_ok=True)
        ignore = _require_mapping(payload.get("ignore", {}), "ignore")
        values = _string_list(ignore.get("paths", []), "ignore.paths")
        paths = {
            _normalize_rule_path(value, f"ignore.paths[{index}]")
            for index, value in enumerate(values)
        }
        return tuple(sorted(paths, key=str))

    def add_ignored_path(self, path: str | os.PathLike[str]) -> bool:
        candidate = normalize_path(path)
        payload = self._load_payload(missing_ok=True)
        existing = list(self._ignored_paths_from_payload(payload))
        if any(_is_same_or_descendant(candidate, root) for root in existing):
            return False

        retained = [
            root
            for root in existing
            if not _is_same_or_descendant(root, candidate)
        ]
        retained.append(candidate)
        self._set_ignored_paths(payload, retained)
        self._write_payload(payload)
        return True

    def remove_ignored_path(self, path: str | os.PathLike[str]) -> bool:
        candidate = normalize_path(path)
        payload = self._load_payload(missing_ok=True)
        existing = list(self._ignored_paths_from_payload(payload))
        retained = [root for root in existing if root != candidate]
        if len(retained) == len(existing):
            return False
        self._set_ignored_paths(payload, retained)
        self._write_payload(payload)
        return True

    def _load_payload(self, *, missing_ok: bool) -> dict[str, Any]:
        if not self.path.exists():
            if missing_ok:
                return {"schema_version": SCHEMA_VERSION}
            raise RulesFileNotFoundError(f"规则文件不存在：{self.path}")
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KnowledgeBaseError(f"无法读取规则文件 {self.path}：{exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KnowledgeBaseError(
                f"规则文件不是有效 JSON：{self.path}:{exc.lineno}:{exc.colno}: {exc.msg}"
            ) from exc
        mapping = _require_mapping(payload, "根对象")
        KnowledgeBase.from_mapping(mapping, source=self.path)
        return mapping

    def _ignored_paths_from_payload(
        self, payload: dict[str, Any]
    ) -> tuple[Path, ...]:
        ignore = _require_mapping(payload.get("ignore", {}), "ignore")
        values = _string_list(ignore.get("paths", []), "ignore.paths")
        return tuple(
            _normalize_rule_path(value, f"ignore.paths[{index}]")
            for index, value in enumerate(values)
        )

    def _set_ignored_paths(
        self, payload: dict[str, Any], paths: list[Path]
    ) -> None:
        ignore = dict(_require_mapping(payload.get("ignore", {}), "ignore"))
        ignore["paths"] = [str(path) for path in sorted(set(paths), key=str)]
        payload["ignore"] = ignore

    def _write_payload(self, payload: dict[str, Any]) -> None:
        KnowledgeBase.from_mapping(payload, source=self.path)
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=parent
            )
        except OSError as exc:
            raise KnowledgeBaseError(f"无法准备规则文件写入：{exc}") from exc

        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor_open = False
            with stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise KnowledgeBaseError(f"无法写入规则文件 {self.path}：{exc}") from exc
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


def _parse_path_rules(value: Any, location: str) -> _PathRules:
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"paths", "globs", "regexes"}, location)

    path_values = _string_list(mapping.get("paths", []), f"{location}.paths")
    paths = tuple(
        _normalize_rule_path(item, f"{location}.paths[{index}]")
        for index, item in enumerate(path_values)
    )
    globs = tuple(
        _normalize_glob(item)
        for item in _string_list(mapping.get("globs", []), f"{location}.globs")
    )

    compiled: list[re.Pattern[str]] = []
    for index, pattern in enumerate(
        _string_list(mapping.get("regexes", []), f"{location}.regexes")
    ):
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise KnowledgeBaseError(
                f"{location}.regexes[{index}] 不是有效正则：{exc}"
            ) from exc
    return _PathRules(paths=paths, globs=globs, regexes=tuple(compiled))


def _parse_applications(value: Any) -> dict[str, ApplicationRule]:
    mapping = _require_mapping(value, "applications")
    result: dict[str, ApplicationRule] = {}
    for bundle_id, raw_rule in mapping.items():
        if not isinstance(bundle_id, str) or not bundle_id:
            raise KnowledgeBaseError("applications 的键必须是非空 bundle ID 字符串")
        rule = _require_mapping(raw_rule, f"applications.{bundle_id}")
        _reject_unknown(
            rule,
            {"name", "protected", "additional_files", "deep_search"},
            f"applications.{bundle_id}",
        )

        name = rule.get("name", "")
        protected = rule.get("protected", False)
        deep_search = rule.get("deep_search", False)
        if not isinstance(name, str):
            raise KnowledgeBaseError(f"applications.{bundle_id}.name 必须是字符串")
        if type(protected) is not bool:
            raise KnowledgeBaseError(f"applications.{bundle_id}.protected 必须是布尔值")
        if type(deep_search) is not bool:
            raise KnowledgeBaseError(f"applications.{bundle_id}.deep_search 必须是布尔值")

        additional_values = _string_list(
            rule.get("additional_files", []),
            f"applications.{bundle_id}.additional_files",
        )
        additional_files = tuple(
            _normalize_rule_path(
                item, f"applications.{bundle_id}.additional_files[{index}]"
            )
            for index, item in enumerate(additional_values)
        )
        result[bundle_id] = ApplicationRule(
            bundle_id=bundle_id,
            name=name,
            protected=protected,
            additional_files=additional_files,
            deep_search=deep_search,
        )
    return result


def _normalize_glob(pattern: str) -> str:
    expanded = os.path.expanduser(pattern)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return expanded


def _normalize_rule_path(value: str, location: str) -> Path:
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise KnowledgeBaseError(f"{location} 必须是绝对路径或以 ~ 开头")
    return normalize_path(expanded)


def _is_same_or_descendant(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KnowledgeBaseError(f"{location} 必须是 JSON 对象")
    return value


def _string_list(value: Any, location: str) -> list[str]:
    if not isinstance(value, list):
        raise KnowledgeBaseError(f"{location} 必须是字符串数组")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise KnowledgeBaseError(f"{location}[{index}] 必须是非空字符串")
        result.append(item)
    return result


def _reject_unknown(
    mapping: dict[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise KnowledgeBaseError(f"{location} 包含未知字段：{', '.join(unknown)}")

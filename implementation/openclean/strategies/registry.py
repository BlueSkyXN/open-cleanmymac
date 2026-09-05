"""策略加载、校验、注册与 pack hash（AR-02 / AR-07 阶段 A）。

设计要点：
- ``DETECTOR_WHITELIST`` / ``ACTION_WHITELIST`` 是内置白名单；pack JSON 只引用名字与参数，
  **不含可执行代码**（AR-02 §2.1）。未知名字 → fail-closed。
- 校验写法照抄 ``knowledge_base.py`` 的 ``_require_mapping``/``_string_list``/``_reject_unknown``
  模式，但不 import 其私有函数，保持本子包无跨模块私有依赖。
- pack 通过 ``importlib.resources`` 读取，wheel-safe；测试可传 ``packs_dir`` 覆盖。
- ``pack_hash`` 用 ``dataclasses.asdict`` + 规范化 JSON，保证同一逻辑 pack 的 hash 稳定，
  供 Run 固化与执行前 ``strategy_hash_matches`` 比对（AR-02 §4.1）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace as _dc_replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..core.errors import PackNotFoundError, StrategyError
from ..core.models import (
    Action,
    Conditions,
    Detector,
    Guards,
    Locator,
    Provenance,
    Recommendation,
    Strategy,
    StrategyAssessment,
    StrategyPack,
)

PACK_SCHEMA_VERSION = 1

# AR-08 §3 detector 白名单（模块级名；细分变体走 detector.params.subkind）。
DETECTOR_WHITELIST = frozenset(
    {
        "filesystem_tree",
        "retention",
        "sqlite_freelist",
        "codex_transient",
        "crashpad",
        "updater",
        "open_unlinked",
        "browser_cache",
        "docker",
        "startup_items",
    }
)

# AR-08 §4 action 白名单。P0 只接线 move_to_trash / report_only，其余名字先允许声明。
ACTION_WHITELIST = frozenset(
    {
        "move_to_trash",
        "empty_trash",
        "docker_prune",
        "specialized",
        "report_only",
    }
)

_STRATEGY_KEYS = {
    "id",
    "version",
    "pack",
    "status",
    "provenance",
    "locator",
    "detector",
    "conditions",
    "guards",
    "assessment",
    "recommendation",
    "action",
}


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyError(f"{location} 必须是 JSON 对象")
    return value


def _string_list(value: Any, location: str) -> list[str]:
    if not isinstance(value, list):
        raise StrategyError(f"{location} 必须是字符串数组")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise StrategyError(f"{location}[{index}] 必须是非空字符串")
        result.append(item)
    return result


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise StrategyError(f"{location} 包含未知字段：{', '.join(unknown)}")


def _require_str(mapping: dict[str, Any], key: str, location: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise StrategyError(f"{location}.{key} 必须是非空字符串")
    return value


def _parse_provenance(value: Any, location: str) -> tuple[Provenance, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StrategyError(f"{location} 必须是数组")
    result: list[Provenance] = []
    for index, raw in enumerate(value):
        entry = _require_mapping(raw, f"{location}[{index}]")
        _reject_unknown(entry, {"kind", "observation_id"}, f"{location}[{index}]")
        kind = _require_str(entry, "kind", f"{location}[{index}]")
        observation_id = entry.get("observation_id", "")
        if not isinstance(observation_id, str):
            raise StrategyError(f"{location}[{index}].observation_id 必须是字符串")
        result.append(Provenance(kind=kind, observation_id=observation_id))
    return tuple(result)


def _parse_detector(value: Any, location: str) -> Detector:
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"name", "params"}, location)
    name = _require_str(mapping, "name", location)
    params = mapping.get("params", {})
    if not isinstance(params, dict):
        raise StrategyError(f"{location}.params 必须是 JSON 对象")
    return Detector(name=name, params=dict(params))


def _parse_conditions(value: Any, location: str) -> Conditions:
    if value is None:
        return Conditions()
    mapping = _require_mapping(value, location)
    _reject_unknown(
        mapping,
        {"minimum_age_days", "require_structure_match", "require_no_open_handles"},
        location,
    )
    minimum_age_days = mapping.get("minimum_age_days")
    if minimum_age_days is not None and type(minimum_age_days) is not int:
        raise StrategyError(f"{location}.minimum_age_days 必须是整数")
    return Conditions(
        minimum_age_days=minimum_age_days,
        require_structure_match=bool(mapping.get("require_structure_match", False)),
        require_no_open_handles=bool(mapping.get("require_no_open_handles", False)),
    )


def _parse_guards(value: Any, location: str) -> Guards:
    if value is None:
        return Guards()
    mapping = _require_mapping(value, location)
    _reject_unknown(
        mapping, {"do_not_generalize_parent", "protect_running_processes"}, location
    )
    return Guards(
        do_not_generalize_parent=bool(
            mapping.get("do_not_generalize_parent", True)
        ),
        protect_running_processes=bool(
            mapping.get("protect_running_processes", True)
        ),
    )


def _parse_assessment(value: Any, location: str) -> StrategyAssessment:
    if value is None:
        return StrategyAssessment()
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"classification", "certainty", "action_risk"}, location)
    return StrategyAssessment(
        classification=mapping.get("classification", "report_only"),
        certainty=mapping.get("certainty", "medium"),
        action_risk=mapping.get("action_risk", "safe"),
    )


def _parse_recommendation(value: Any, location: str) -> Recommendation:
    if value is None:
        return Recommendation()
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"summary", "do_not_do"}, location)
    summary = mapping.get("summary", "")
    if not isinstance(summary, str):
        raise StrategyError(f"{location}.summary 必须是字符串")
    do_not_do = _string_list(mapping.get("do_not_do", []), f"{location}.do_not_do")
    return Recommendation(summary=summary, do_not_do=tuple(do_not_do))


def _parse_action(value: Any, location: str) -> Action:
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"name", "supported"}, location)
    name = _require_str(mapping, "name", location)
    supported = mapping.get("supported", False)
    if type(supported) is not bool:
        raise StrategyError(f"{location}.supported 必须是布尔值")
    return Action(name=name, supported=supported)


def _parse_locator(value: Any, location: str) -> Locator:
    if value is None:
        return Locator()
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, {"roots"}, location)
    return Locator(roots=tuple(_string_list(mapping.get("roots", []), f"{location}.roots")))


def _parse_strategy(value: Any, location: str) -> Strategy:
    mapping = _require_mapping(value, location)
    _reject_unknown(mapping, _STRATEGY_KEYS, location)
    version = mapping.get("version")
    if type(version) is not int:
        raise StrategyError(f"{location}.version 必须是整数")
    return Strategy(
        id=_require_str(mapping, "id", location),
        version=version,
        pack=_require_str(mapping, "pack", location),
        status=_require_str(mapping, "status", location),
        provenance=_parse_provenance(mapping.get("provenance"), f"{location}.provenance"),
        locator=_parse_locator(mapping.get("locator"), f"{location}.locator"),
        detector=_parse_detector(mapping.get("detector"), f"{location}.detector"),
        conditions=_parse_conditions(mapping.get("conditions"), f"{location}.conditions"),
        guards=_parse_guards(mapping.get("guards"), f"{location}.guards"),
        assessment=_parse_assessment(mapping.get("assessment"), f"{location}.assessment"),
        recommendation=_parse_recommendation(
            mapping.get("recommendation"), f"{location}.recommendation"
        ),
        action=_parse_action(mapping.get("action"), f"{location}.action"),
    )


def validate_pack(pack: StrategyPack) -> None:
    """校验白名单引用与跨策略约束；结构不变量已在各 dataclass ``__post_init__`` 兜底。"""
    for strategy in pack.strategies:
        if strategy.detector.name not in DETECTOR_WHITELIST:
            raise StrategyError(
                f"策略 {strategy.id} 引用未知 detector：{strategy.detector.name}"
            )
        if strategy.action.name not in ACTION_WHITELIST:
            raise StrategyError(
                f"策略 {strategy.id} 引用未知 action：{strategy.action.name}"
            )


def pack_from_mapping(payload: Any) -> StrategyPack:
    root = _require_mapping(payload, "pack 根对象")
    _reject_unknown(root, {"schema_version", "name", "strategies"}, "pack 根对象")
    version = root.get("schema_version")
    if type(version) is not int or version != PACK_SCHEMA_VERSION:
        raise StrategyError(
            f"仅支持 pack schema_version={PACK_SCHEMA_VERSION}，实际为 {version!r}"
        )
    name = _require_str(root, "name", "pack 根对象")
    raw_strategies = root.get("strategies", [])
    if not isinstance(raw_strategies, list):
        raise StrategyError("pack.strategies 必须是数组")
    strategies = tuple(
        _parse_strategy(raw, f"strategies[{index}]")
        for index, raw in enumerate(raw_strategies)
    )
    pack = StrategyPack(name=name, strategies=strategies, schema_version=version)
    validate_pack(pack)
    return pack


def _downgrade_external_pack(pack: StrategyPack) -> StrategyPack:
    """把外部 pack 中的 trusted 策略降级为 active/report_only（DEC-007）。"""
    downgraded: list[Strategy] = []
    for s in pack.strategies:
        if s.status == "trusted":
            s = _dc_replace(
                s,
                status="active",
                action=Action(name=s.action.name, supported=False),
                assessment=StrategyAssessment(
                    classification="report_only",
                    certainty=s.assessment.certainty,
                    action_risk=s.assessment.action_risk,
                ),
            )
        downgraded.append(s)
    return StrategyPack(
        name=pack.name,
        strategies=tuple(downgraded),
        schema_version=pack.schema_version,
    )


def pack_hash(pack: StrategyPack) -> str:
    canonical = json.dumps(
        asdict(pack), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _packs_root(packs_dir: Path | None) -> Any:
    if packs_dir is not None:
        return Path(packs_dir)
    return files("openclean") / "packs"


def available_pack_names(*, packs_dir: Path | None = None) -> tuple[str, ...]:
    root = _packs_root(packs_dir)
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise PackNotFoundError(f"无法枚举策略包目录：{exc}") from exc
    return tuple(sorted(e.name[: -len(".json")] for e in entries if e.name.endswith(".json")))


def load_pack(name: str, *, packs_dir: Path | None = None) -> StrategyPack:
    root = _packs_root(packs_dir)
    resource = root / f"{name}.json"
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PackNotFoundError(f"策略包不存在：{name}（{exc}）") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StrategyError(
            f"策略包 {name} 不是有效 JSON：{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    pack = pack_from_mapping(payload)
    if pack.name != name:
        raise StrategyError(f"策略包文件名 {name} 与内部 name {pack.name} 不一致")
    return pack


class StrategyRegistry:
    """加载并按 id/pack 检索策略；同 id 取最高 version（AR-02 §4.1）。

    P0 BUG-007 fix：外部 packs_dir 加载的 trusted 策略自动降级为
    active/report_only，不可获得动作权限（DEC-007）。
    """

    def __init__(
        self,
        packs: tuple[StrategyPack, ...] = (),
        *,
        external_pack_names: frozenset[str] | None = None,
    ) -> None:
        self._packs: dict[str, StrategyPack] = {pack.name: pack for pack in packs}
        self._external_packs: frozenset[str] = external_pack_names or frozenset()

    @classmethod
    def load(
        cls, names: tuple[str, ...] | None = None, *, packs_dir: Path | None = None
    ) -> StrategyRegistry:
        resolved = names if names is not None else available_pack_names(packs_dir=packs_dir)
        loaded = tuple(load_pack(name, packs_dir=packs_dir) for name in resolved)
        if packs_dir is not None:
            # P0 BUG-007：外部目录加载的 pack 强制降级 trusted → active/report_only。
            loaded = tuple(_downgrade_external_pack(p) for p in loaded)
            external = frozenset(p.name for p in loaded)
        else:
            external = frozenset()
        return cls(loaded, external_pack_names=external)

    @property
    def packs(self) -> tuple[StrategyPack, ...]:
        return tuple(self._packs.values())

    def loaded_pack_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._packs))

    def get_pack(self, name: str) -> StrategyPack:
        try:
            return self._packs[name]
        except KeyError:
            raise PackNotFoundError(f"未加载策略包：{name}") from None

    def by_pack(self, name: str) -> tuple[Strategy, ...]:
        return self.get_pack(name).strategies

    def runtime_visible(self, pack_name: str | None = None) -> tuple[Strategy, ...]:
        packs = (
            [self.get_pack(pack_name)]
            if pack_name is not None
            else list(self._packs.values())
        )
        return tuple(s for pack in packs for s in pack.runtime_visible())

    def get(self, strategy_id: str) -> Strategy:
        """按 id 返回最高 version 的策略；不存在抛 StrategyError。"""
        matches = [
            s for pack in self._packs.values() for s in pack.strategies if s.id == strategy_id
        ]
        if not matches:
            raise StrategyError(f"未知策略：{strategy_id}")
        return max(matches, key=lambda s: s.version)

    def pack_hash(self, pack_name: str) -> str:
        return pack_hash(self.get_pack(pack_name))

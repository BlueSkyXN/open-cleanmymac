"""策略加载、校验、注册与 pack hash（AR-02 / AR-07 阶段 A）。

设计要点：
- ``DETECTOR_WHITELIST`` / ``ACTION_WHITELIST`` 是内置白名单；pack JSON 只引用名字与参数，
  **不含可执行代码**（AR-02 §2.1）。未知名字 → fail-closed。
- JSON 通过严格 dataclass 解码器检查字段和类型，拒绝字符串布尔值、重复键和未知字段。
- pack 通过 ``importlib.resources`` 读取，wheel-safe；测试可传 ``packs_dir`` 覆盖。
- ``pack_hash`` 用 ``dataclasses.asdict`` + 规范化 JSON，保证同一逻辑 pack 的 hash 稳定，
  供 Run 固化与执行前 ``strategy_hash_matches`` 比对（AR-02 §4.1）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace as _dc_replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..core.errors import PackNotFoundError, StrategyError
from ..core.serialization import decode_dataclass, reject_json_constant, strict_json_pairs
from ..core.models import (
    Action,
    Strategy,
    StrategyAssessment,
    StrategyPack,
)

PACK_SCHEMA_VERSION = 1
# No production action has the required observation + promotion evidence yet.
# Changes to this set require an explicit, documented human promotion.
APPROVED_PACK_HASHES: frozenset[str] = frozenset()


def _pack_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise StrategyError("Invalid strategy pack name")
    return name


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
        "workbuddy",
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
    try:
        pack = decode_dataclass(StrategyPack, payload, "pack")
        if pack.schema_version != PACK_SCHEMA_VERSION:
            raise ValueError("Unsupported strategy schema_version")
        _pack_name(pack.name)
        validate_pack(pack)
        for strategy in pack.strategies:
            expected = {
                "codex_transient": {"subkind": str, "name_glob": str},
                "crashpad": {"subkind": str},
                "retention": {"category": str, "include_partitions": bool},
                "sqlite_freelist": {"category": str},
                "workbuddy": {},
            }.get(strategy.detector.name)
            if expected is not None:
                for key, value in strategy.detector.params.items():
                    if key not in expected or type(value) is not expected[key]:
                        raise ValueError(f"Invalid detector parameter: {strategy.id}.{key}")
            for root in strategy.locator.roots:
                if not (root == "~" or root.startswith("~/") or root.startswith("/")):
                    raise ValueError("Locator roots must be absolute or HOME-relative")
                if "\0" in root or ".." in Path(root).parts:
                    raise ValueError("Invalid locator root")
            if strategy.action.name == "report_only" and strategy.action.supported:
                raise ValueError("report_only cannot support execution")
        return pack
    except StrategyError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise StrategyError(f"Invalid strategy pack: {exc}") from exc


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
    _pack_name(name)
    root = _packs_root(packs_dir)
    resource = root / f"{name}.json"
    try:
        raw = resource.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise StrategyError(f"策略包 {name} 不是有效 UTF-8") from exc
    except (FileNotFoundError, OSError) as exc:
        raise PackNotFoundError(f"策略包不存在：{name}（{exc}）") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=strict_json_pairs,
                             parse_constant=reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise StrategyError(f"策略包 {name} 不是有效 JSON：{exc}") from exc
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
        approved_pack_hashes: frozenset[str] = frozenset(),
    ) -> None:
        if len({p.name for p in packs}) != len(packs):
            raise StrategyError("Duplicate strategy pack name")
        self._external_packs = external_pack_names or frozenset()
        self._approved_hashes = frozenset(approved_pack_hashes)
        self._packs = {
            pack.name: (pack if pack_hash(pack) in self._approved_hashes
                        and pack.name not in self._external_packs
                        else _downgrade_external_pack(pack)) for pack in packs
        }

    @classmethod
    def load(
        cls, names: tuple[str, ...] | None = None, *, packs_dir: Path | None = None
    ) -> StrategyRegistry:
        resolved = names if names is not None else available_pack_names(packs_dir=packs_dir)
        loaded = tuple(load_pack(name, packs_dir=packs_dir) for name in resolved)
        # __init__ performs the external downgrade once for every entry path.
        external = frozenset(p.name for p in loaded) if packs_dir is not None else frozenset()
        return cls(loaded, external_pack_names=external,
                   approved_pack_hashes=APPROVED_PACK_HASHES if packs_dir is None else frozenset())

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
            (self.get_pack(pack_name),)
            if pack_name is not None
            else self._packs.values()
        )
        return tuple(s for pack in packs for s in pack.runtime_visible())

    def get(self, strategy_id: str) -> Strategy:
        """按 id 返回最高 version 的策略；不存在抛 StrategyError。"""
        strategy = max(
            (s for pack in self._packs.values() for s in pack.strategies if s.id == strategy_id),
            key=lambda s: s.version, default=None,
        )
        if strategy is None:
            raise StrategyError(f"未知策略：{strategy_id}")
        return strategy

    def pack_hash(self, pack_name: str) -> str:
        return pack_hash(self.get_pack(pack_name))

    def action_approved(self, strategy: Strategy) -> bool:
        return (strategy.pack not in self._external_packs
                and strategy.status == "trusted"
                and self.pack_hash(strategy.pack) in self._approved_hashes)

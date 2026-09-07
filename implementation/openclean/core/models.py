"""Agent Runtime v1 领域模型。

定义 specs/agent-runtime/AR-01 的五个核心对象（Observation 属研究平面，见 lab）与回执：
Strategy / StrategyPack / Run / Finding / CleanupPlan / CleanupOutcomeRecord。

模型只描述事实与判断，不负责路径发现、探测或文件操作。既有 ``openclean.models.Item``
在迁移期继续作为安全内核（cleanup/detector）的执行通货；Finding 通过
``runtime.finding_projection`` 与之双向投影。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import (
    DIAGNOSTIC_KINDS,
    FILESYSTEM_RESOURCE_KINDS,
    FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS,
    RESOURCE_KINDS,
    SAFETY_LEVELS,
    FileIdentity,
)

# --- 枚举与常量 -----------------------------------------------------------

STRATEGY_STATUSES = frozenset({"draft", "active", "trusted", "deprecated"})
RUNTIME_VISIBLE_STATUSES = frozenset({"active", "trusted"})
CERTAINTY_LEVELS = frozenset({"low", "medium", "high"})
CLASSIFICATIONS = frozenset({"cleanup_candidate", "report_only", "protected"})
PROVENANCE_KINDS = frozenset(
    {
        "cleanmymac_public",
        "cleanmymac_observation",
        "personal_experience",
        "ai_research",
    }
)
# Finding 的 evidence.kind：只读诊断沿用 Item 的 DIAGNOSTIC_KINDS；可执行文件系统候选
# 使用下面这个非诊断 kind，从而绕开「诊断项必须 actionable=False」的不变量。
EVIDENCE_FILESYSTEM = "filesystem"
READONLY_EVIDENCE_KINDS = frozenset(k for k in DIAGNOSTIC_KINDS if k)

RUN_TTL_SECONDS = 24 * 60 * 60
ID_PREFIX_RUN = "run:"
ID_PREFIX_FINDING = "finding:"


# --- Strategy（策略平面）--------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """一条策略的来源记录（AR-02 §5）。来源不决定组织方式。"""

    kind: str
    observation_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PROVENANCE_KINDS:
            raise ValueError(f"未知 provenance.kind：{self.kind}")


@dataclass(frozen=True)
class Locator:
    """在哪里寻找：``~``/绝对路径根，不跟随 symlink。"""

    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class Detector:
    """用哪个内置探测器。``name`` 必须来自 registry 白名单；params 不含可执行代码。"""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Conditions:
    minimum_age_days: int | None = None
    require_structure_match: bool = False
    require_no_open_handles: bool = False

    def __post_init__(self) -> None:
        if self.minimum_age_days is not None and self.minimum_age_days < 0:
            raise ValueError("minimum_age_days 不能为负数")


@dataclass(frozen=True)
class Guards:
    do_not_generalize_parent: bool = True
    protect_running_processes: bool = True


@dataclass(frozen=True)
class StrategyAssessment:
    classification: str = "report_only"
    certainty: str = "medium"
    action_risk: str = "safe"

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(f"未知 classification：{self.classification}")
        if self.certainty not in CERTAINTY_LEVELS:
            raise ValueError(f"未知 certainty：{self.certainty}")
        if self.action_risk not in SAFETY_LEVELS:
            raise ValueError(f"未知 action_risk：{self.action_risk}")


@dataclass(frozen=True)
class Recommendation:
    summary: str = ""
    do_not_do: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    name: str
    supported: bool = False


@dataclass(frozen=True)
class Strategy:
    """策略库基本单元（AR-01 §3 / AR-02 §2）。JSON 不承载可执行代码。"""

    id: str
    version: int
    pack: str
    status: str
    detector: Detector
    action: Action
    provenance: tuple[Provenance, ...] = ()
    locator: Locator = field(default_factory=Locator)
    conditions: Conditions = field(default_factory=Conditions)
    guards: Guards = field(default_factory=Guards)
    assessment: StrategyAssessment = field(default_factory=StrategyAssessment)
    recommendation: Recommendation = field(default_factory=Recommendation)

    def __post_init__(self) -> None:
        if not self.id or "." not in self.id:
            raise ValueError("Strategy.id 必须是非空且含 '.' 的分层标识")
        if not self.pack:
            raise ValueError("Strategy.pack 不能为空")
        if not self.id.startswith(f"{self.pack}."):
            raise ValueError(f"Strategy.id 必须以 pack 前缀开头：{self.pack}")
        if type(self.version) is not int or self.version < 1:
            raise ValueError(f"Strategy.version 必须是正整数：{self.version!r}")
        if self.status not in STRATEGY_STATUSES:
            raise ValueError(f"未知 status：{self.status}")
        # AR-02 §5：provenance 为空只能是 draft。
        if not self.provenance and self.status != "draft":
            raise ValueError("无 provenance 的策略只能是 draft")
        # AR-02 §3：只有 trusted 可具备经验证动作；trusted 必须 action.supported。
        if self.status == "trusted" and not self.action.supported:
            raise ValueError("trusted 策略必须 action.supported=true")
        if self.status != "trusted" and self.action.supported:
            raise ValueError("仅 trusted 策略可 action.supported=true")

    @property
    def runtime_visible(self) -> bool:
        return self.status in RUNTIME_VISIBLE_STATUSES


@dataclass(frozen=True)
class StrategyPack:
    name: str
    strategies: tuple[Strategy, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("StrategyPack.name 不能为空")
        seen: dict[str, int] = {}
        for strategy in self.strategies:
            if strategy.pack != self.name:
                raise ValueError(
                    f"策略 {strategy.id} 的 pack 与所属包 {self.name} 不一致"
                )
            if strategy.id in seen:
                raise ValueError(f"pack 内策略 id 重复：{strategy.id}")
            seen[strategy.id] = strategy.version

    def runtime_visible(self) -> tuple[Strategy, ...]:
        return tuple(s for s in self.strategies if s.runtime_visible)


# --- Run / Finding（运行平面）---------------------------------------------


@dataclass(frozen=True)
class FindingTarget:
    kind: str
    display_path: str | None = None
    identifier: str = ""
    identity: FileIdentity | None = None

    def __post_init__(self) -> None:
        if self.kind not in RESOURCE_KINDS:
            raise ValueError(f"未知 target.kind：{self.kind}")
        if self.kind in FILESYSTEM_RESOURCE_KINDS and self.display_path is None:
            raise ValueError("文件系统 target 必须包含 display_path")
        if self.kind not in FILESYSTEM_RESOURCE_KINDS and not self.identifier:
            raise ValueError("非文件系统 target 必须包含 identifier")


@dataclass(frozen=True)
class FindingMeasurement:
    size: int = 0
    allocated_bytes: int | None = None
    logical_bytes: int | None = None
    age_days: int | None = None
    latest_mtime: float | None = None

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError("measurement.size 不能为负数")


@dataclass(frozen=True)
class FindingAssessment:
    classification: str
    certainty: str
    action_risk: str
    actionable: bool
    block_reasons: tuple[str, ...] = ()
    requires_privilege: bool = False
    is_cloud_file: bool = False
    requires_explicit_selection: bool = False

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(f"未知 classification：{self.classification}")
        if self.certainty not in CERTAINTY_LEVELS:
            raise ValueError(f"未知 certainty：{self.certainty}")
        if self.action_risk not in SAFETY_LEVELS:
            raise ValueError(f"未知 action_risk：{self.action_risk}")
        if self.actionable and self.block_reasons:
            raise ValueError("actionable 的 Finding 不能带 block_reasons")


@dataclass(frozen=True)
class FindingEvidence:
    """类型化证据（AR-01 §5）。差异放 payload，避免 Finding 顶层膨胀。"""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """某条 Strategy 在当前机器上的实际命中（AR-01 §5）。finding_id 不构成授权。"""

    finding_id: str
    run_id: str
    strategy_id: str
    strategy_version: int
    target: FindingTarget
    measurement: FindingMeasurement
    assessment: FindingAssessment
    evidence: FindingEvidence
    recommendation: Recommendation = field(default_factory=Recommendation)

    def __post_init__(self) -> None:
        if not self.finding_id.startswith(ID_PREFIX_FINDING):
            raise ValueError("finding_id 必须以 'finding:' 开头")
        if not self.run_id.startswith(ID_PREFIX_RUN):
            raise ValueError("run_id 必须以 'run:' 开头")
        if not self.strategy_id:
            raise ValueError("Finding.strategy_id 不能为空")
        # 镜像 Item.__post_init__（models.py:355）：只读诊断永远不可执行。
        if self.evidence.kind in READONLY_EVIDENCE_KINDS and self.assessment.actionable:
            raise ValueError("只读诊断 Finding 不能 actionable")
        # 镜像 Item.__post_init__（models.py:215-223）：filesystem_subset ↔ 子集诊断。
        if self.target.kind == "filesystem_subset" and (
            self.evidence.kind not in FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS
        ):
            raise ValueError("filesystem_subset 只能用于已知只读子集诊断")
        if (
            self.evidence.kind in FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS
            and self.target.kind != "filesystem_subset"
        ):
            raise ValueError("子集诊断必须使用 filesystem_subset target")


@dataclass(frozen=True)
class RunIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class Run:
    """固化一次 inspect 的环境、策略版本与 Finding 集合（AR-01 §4）。"""

    run_id: str
    created_at: float
    expires_at: float
    requested_target: str
    openclean_version: str
    macos_version: str = ""
    strategy_pack_hashes: dict[str, str] = field(default_factory=dict)
    protect_config_hash: str = ""
    complete: bool = True
    issues: tuple[RunIssue, ...] = ()
    finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id.startswith(ID_PREFIX_RUN):
            raise ValueError("run_id 必须以 'run:' 开头")

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


# --- CleanupPlan / CleanupOutcome（执行平面）------------------------------


@dataclass(frozen=True)
class ResolvedTarget:
    """CleanupPlan 中的精确动作目标；聚合根不得入内（AR-06 §4）。"""

    display_path: str
    identity: FileIdentity | None = None


@dataclass(frozen=True)
class CleanupPlanItem:
    finding_id: str
    strategy_id: str
    strategy_version: int
    action_name: str
    action_supported: bool
    can_execute: bool
    resolved_targets: tuple[ResolvedTarget, ...] = ()
    block_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupPlan:
    mode: str  # "preview" | "execute"
    run_id: str
    executed: bool
    plan_items: tuple[CleanupPlanItem, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"preview", "execute"}:
            raise ValueError(f"未知 CleanupPlan.mode：{self.mode}")
        if self.mode == "preview" and self.executed:
            raise ValueError("preview 计划的 executed 必须为 False")


@dataclass(frozen=True)
class CleanupOutcomeRecord:
    """执行回执（AR-01 §8），区分暂存与永久删除。"""

    finding_id: str
    status: str
    bytes_affected: int = 0
    destination: str | None = None
    message: str = ""

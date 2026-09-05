# AR-03 · 命令与 I/O 契约

> **0.24.0a1 当前实施约束**：P0a 只读与计划预览；7 条 Codex 策略均 active/report_only。
> 经典命令和 TUI 保留。P0b 结构匹配与正式策略审批尚未完成。本文的长期扩展不等于当前已实现。
> 本次落地语义以 [当前实现补充](ar-09-current-implementation.md) 为准。


[契约索引](_index.md) · [AR-00 架构](ar-00-architecture.md) ·
[AR-01 对象模型](ar-01-object-model.md) · [实现说明](../../implementation/README.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义 Agent 主接口与研究命令的参数、JSON 输出、退出码，以及旧命令的保留关系。
> 执行阶段的授权与 live guard 见 [AR-04](ar-04-run-store-and-execution.md)。

## 1. 命令树

```text
openclean
├── inspect   TARGET [--json]                 # 按策略包识别，产出 run_id + Finding 摘要
├── explore   PATH [--max-depth N] [--max-entries N] [--json]   # 只读研究证据
├── show      --run RUN --finding FINDING [--json]              # 单个 Finding 完整证据
├── clean     --run RUN --finding FINDING [--yes] [--json]      # 预览 / 执行
├── strategy  {list, show STRATEGY_ID, verify} [--json]         # 只读策略查询
├── config    …                                                 # 现有 + Run Store / pack 配置
└── lab       {capture, compare, draft, validate, promote, demote}   # 研究命令，非 Agent 主接口
```

当前实施为附加 Agent 接口；经典命令、TUI、ignore/config/cat 保留，optimize 继续明确拒绝。

## 2. `inspect TARGET`

按策略包运行识别，`TARGET ∈ {codex, qoder, workbuddy, claude, cursor, macos-system,
developer-tools, browsers, docker, project-artifacts, all}`（全集见
[AR-08](ar-08-strategy-pack-catalog.md)）；`all` = 已加载 pack 的并集。实现按
[AR-07](ar-07-implementation-roadmap.md) 分阶段落地，Codex 为首个切片。

```bash
openclean inspect codex --json
```

输出（示意）：

```json
{
  "schema_version": 2,
  "command": "inspect codex",
  "run_id": "run:01HXXXXCODEX",
  "expires_at": "2026-09-04T10:00:00+08:00",
  "requested_target": "codex",
  "complete": true,
  "issues": [],
  "totals": {"findings": 2, "potential_bytes": 104857600, "actionable_bytes": 0},
  "findings": [
    {
      "finding_id": "finding:01HXXXXA",
      "strategy_id": "codex.marketplace.old-staging",
      "classification": "cleanup_candidate",
      "certainty": "high",
      "action_risk": "low",
      "actionable": false,
      "potential_bytes": 104857600,
      "display_path": "~/.codex/.tmp/marketplaces/.staging/marketplace-upgrade-2024x"
    }
  ]
}
```

不变量：

- `inspect` **只返回目标 pack** 的 Finding，Agent 不再需要从全量 AI 域结果里自行猜哪些属于
  Codex（这是首切片的核心验收判据，见 [AR-06](ar-06-codex-p0-acceptance.md) §2）。
- `inspect` 永远只读，产出 `run_id` 并写入 Run Store（见 [AR-04](ar-04-run-store-and-execution.md) §2）。
- 摘要里的 `finding_id`/`run_id` 稳定、不含路径；完整证据只在 `show` 返回。

## 3. `explore PATH`

面向 Agent 研究：提供目录结构、容量、年龄、进程与句柄证据，供 AI 提出 Observation 或
Strategy draft。**`explore` 提供研究证据，不生成任意可删除目标。**

```bash
openclean explore ~/.codex --max-depth 4 --max-entries 5000 --json
```

只读约束（硬性）：

- 永远只读；输出**不能直接进入 cleanup**（不产生 `finding_id`，不写 Run Store 的可执行目标）。
- 不读取普通文件正文；不跟随 symlink。
- 有深度（`--max-depth`）、数量（`--max-entries`）、时间与容量预算；超预算时结构化截断并报告。
- 遇到跨卷、`SF_DATALESS`/疑似云占位、权限错误时结构化报告（沿用现有 `cross_device_paths`、
  dataless 阻断与 issue code 语义）。
- 复用现有 `analyzer.py` 的一级计量能力，但增加树摘要与预算控制；`analyze`（保留）与
  `explore` 的处置见 §9。

## 4. `show --run --finding`

返回单个 Finding 的完整证据、判断、阻断原因与允许的动作。

```bash
openclean show --run run:01HXXXXCODEX --finding finding:01HXXXXA --json
```

输出为 [AR-01](ar-01-object-model.md) §5 的完整 Finding 对象（含 `evidence.payload`、
`assessment.block_reasons`、`recommendation`）外加 `allowed_actions`。

不变量：

- `--finding` 必须属于 `--run`；否则退出码 `2`（`finding_not_in_run`）。
- Run 过期或不存在时拒绝，退出码 `1`（`run_expired` / `run_not_found`），**不自动重新扫描**
  并假装是同一个 Finding。
- `show` 只读，不改变 Run 状态。

## 5. `clean --run --finding`

Finding 驱动的清理。默认预览，`--yes` 才执行。

```bash
# 预览
openclean clean --run run:01HXXXXCODEX --finding finding:01HXXXXA --json
# 执行（用户对当前 Finding 明确授权后）
openclean clean --run run:01HXXXXCODEX --finding finding:01HXXXXA --yes --json
```

预览输出固定（见 [AR-01](ar-01-object-model.md) §7）：

```json
{"schema_version": 2, "command": "clean", "mode": "preview", "executed": false, "run_id": "run:01HXXXXCODEX", "plan": { "…": "CleanupPlan" }}
```

不变量：

- **不带 `--yes` 永不写**：固定 `mode="preview"`、`executed=false`。
- 目标只能从 Run Store 按 `run_id + finding_id` 解析；**不接受 AI 任意拼接路径**。当前
  并不存在 `openclean delete /arbitrary/path` 这样的入口，v1 也不新增。
- `--finding` 可重复以选择多个 Finding；批量执行 all-or-nothing（见
  [AR-04](ar-04-run-store-and-execution.md) §5）。
- `confirm`/`critical` 风险级别仍要求额外授权 flag（沿用现有 `--include-confirm`/
  `--include-critical` 语义）；**`finding_id` 不能替代风险确认**。
- 执行入口复用现有 `cleanup.py` 的 `execute_cleanup` 安全执行器（预检 + live 复核 +
  同卷 Trash），只是选择来源从「本次扫描的 path/identifier」改为「Run Store 的 Finding」。
- `active`（非 `trusted`）策略的 Finding，预览返回 `can_execute=false`、
  `block_reasons=["strategy_not_trusted"]`；`--yes` 也拒绝执行。

## 6. `strategy` 与 `lab`

普通运行时只需要只读策略查询：

```bash
openclean strategy list --json
openclean strategy show codex.marketplace.old-staging --json
openclean strategy verify --json          # 校验已加载 pack 的结构与白名单引用
```

研究与写入放到 `lab`（占位契约，P0 不要求自动化实现，见
[AR-06](ar-06-codex-p0-acceptance.md) §5）：

```bash
openclean lab capture  --source cleanmymac --scenario codex-ai-junk
openclean lab compare  --scenario codex-ai-junk --reference cleanmymac --candidate openclean
openclean lab draft    --from-observation obs:personal:qoder:001
openclean lab validate --strategy codex.marketplace.old-staging
openclean lab promote  --strategy codex.marketplace.old-staging --to active
openclean lab demote   --strategy codex.marketplace.old-staging --to deprecated
```

不变量：

- **`promote`/`demote` 必须是显式人工动作。** AI 可以生成 draft 与 validate 报告，**不能**
  自行把策略晋级到 `trusted`（见 [AR-00](ar-00-architecture.md) §3、
  [AR-02](ar-02-strategy-and-lifecycle.md) §3）。
- `lab` 写入只影响本地研究材料，不改变已加载 pack，除非显式 `promote` 后重新加载。

## 7. `--redact-paths` 不可 replay（决策 5）

沿用现有 `redaction.py` 脱敏机制（单文档 opaque path ref）：

- 所有新 JSON 命令支持 `--redact-paths`，在最终序列化阶段把同一文档内路径映射成稳定
  opaque ref（如 `path:0001`）。
- **脱敏输出同时替换 actionable ID**：`run_id`/`finding_id` 在脱敏文档中不可用于后续
  `show`/`clean`。输出声明 `selection_replayable=false`。
- 脱敏结果只能用于向用户报告，不能作为 selector 回放；这与「`finding_id` 不编码路径、
  不可跨会话重放」一致（见 [AR-04](ar-04-run-store-and-execution.md) §3）。

## 8. 退出码

沿用现有语义（[implementation/README.md](../../implementation/README.md) 退出码节）：

| code | 含义 | 新命令典型场景 |
|---:|---|---|
| `0` | 命令按契约完成 | `inspect`/`show`/`clean` 预览成功 |
| `1` | 有 blocking issue、outcome 失败或能力 unavailable | Run 过期/不存在、执行被 live guard 阻断、`active` 策略请求执行 |
| `2` | 参数、规则、路径、选择或配置错误 | `--finding` 不属于 `--run`、未知 `TARGET`、未知 `strategy_id` |
| `130` | 用户中断 | TTY 下中断 |

## 9. 命令面与旧能力处置（决策 1）

当前命令面并存，不删除经典入口：

| 现有入口 | 当前处置 | Agent 关系 |
|---|---|---|
| scan / clean category / analyze / purge | 全部保留 | 新增 inspect/show/clean --run，不替换旧调用 |
| optimize ram/purgeable | 保持不可执行 | 未实现公开执行器 |
| ignore/config/cat | 保留 | Agent 应用同一保护配置 |
| curses TUI | 保留 | 人类界面，不是 Agent 输出替代品 |

当前 CLI envelope 固定为 schema v2；对象和持久化容器独立版本。长期扩展须单独更新契约。

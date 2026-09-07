# open-cleanmymac · AI 开发交接入口

> 给接手开发的 AI：读完这份和它指向的规格，就能开工。
> 目标：用 Python 独立实现 CleanMyMac CLI 的开源平替（`openclean`），尽量对齐核心使用
> 流程、具体功能和终端体验，并增加自有识别规则、存储诊断与清理特色。
> 方便脚本和 AI Agent 使用是接口增强，不是将产品改造成 Agent 策略平台。

当前用户可见行为以 [README.md](README.md) 和实际实现为依据；后续研发定位以本页为准。
开发先读相关 `specs/` 和 `implementation/`，补充依据可来自公开资料及独立验证的行为；
不要读 `analysis/`。缺口见 [implementation/TODO.md](implementation/TODO.md)。

## 产品主线与研发取舍

- **平替优先、特色增强、接口友好**：CleanMyMac CLI 是核心流程与具体功能的对标对象，
  不只是可有可无的经验来源。OpenClean 独立运行，不要求安装或调用官方 CLI。
- **按用户任务组织产品**：以 Clean、Purge、Analyze、Optimize、Config 为核心流程，围绕
  扫描、审阅、确认、执行和结果反馈完善体验。保留 `scan`、`ignore` 等自有实用入口；
  不为机械复制菜单而删除既有功能，也不把尚不可用的 Optimize 宣称为已完成。
- **平替以结果和可用性衡量**：对照官方 CLI 的实际功能，区分已对齐、部分对齐、未实现和
  有意不做；不因命令名存在或能够安全拒绝，就把相应执行能力算成等效实现。Desktop 能力
  不自动进入 CLI 对齐范围，缺少公开 API、签名或环境验收时如实记录限制。
- **技术原理可借鉴，代码与规则独立维护**：依据公开接口、通用算法和独立实验实现等效
  机制；不直接复制参考软件源码、反编译伪代码、私有规则库或专有数据。不把来源未确认的
  研究材料包装成公开事实；参考产品的命中也不直接构成删除依据。
- **特色承接已有成果**：个人经验用于补充工具、环境及潜在垃圾对象的识别。复用现有
  扫描点、日志/runtime 保留期、updater、SQLite、浏览器和临时结构等诊断；缺少 pack 或
  Observation 文件不等于能力未实现，也不要求全部重新研究。仅对具体证据缺口、行为变化
  或从只读升级为清理的对象补充相应验证。
- **Agent 是调用方，不是产品中心**：TUI、非交互参数、JSON、退出码和结果引用共同服务
  人、脚本与 Agent。Codex、Qoder、WorkBuddy 等是被识别的软件，不是待接入的模型服务；
  通用清理工具的范围不能因此收窄为 AI 软件目录。
- **用户指挥、Agent 实际使用**：用户给出目标、范围和必要的动作授权，Agent 负责选择命令、
  获取证据、精确预览、在既有授权范围内执行并核对结果；不要求用户手工搬运 JSON 或逐条敲命令。
  已有明确授权不必每个工具调用重复索要，但不确定范围、不可执行项和新风险必须停止扩面。
  Agent 调用契约见 `docs/AI_USAGE.md`；自有经验和历史来源见 `docs/EXPERIENCE.md`。
- **内部模型服务功能**：Strategy、Run、Finding 和 Run Store 按实际收益使用。保留已有
  接口与安全修复，不因本次定位调整回滚代码；不强制所有经典能力先迁入 pack/Finding 才能
  使用，不以 pack 数量、P0a/P0b 阶段或框架模块数量代替整个产品的完成度。
- **研发先查已有实现**：每项任务先说明对应的用户功能、现有能力及真实差距，再判断是
  补功能、补识别对象、改善体验还是完善接口。优先复用，避免重复建设；动作启用仍需其
  对应证据和明确授权，不因“平替”目标绕过既有保护条件。

旧规格或私人交接若将“面向 AI Agent 的存储策略引擎”作为产品中心，或要求退役经典
命令、从头重做已有能力，不作为当前研发方向。相关实现细节可以复用；遇到冲突应明确
标注，并按本页定位制定任务，不静默执行旧路线。

## 30 秒上手

```bash
make preview          # TemporaryDirectory 隔离演示，不碰真实 HOME
make check            # 本地轻量语法和 CLI 启动检查，无需开发依赖
cat implementation/TODO.md
```

源码直接跑：

```bash
cd implementation
PYTHONPATH=. python3 -m openclean.cli scan --json           # 经典五域扫描（只读）
PYTHONPATH=. python3 -m openclean.cli inspect codex --json  # Agent Runtime（附加命令面）
```

扫描/预览不修改候选。Agent `inspect` 会写入本机 Run Store；`--yes`、`ignore add/remove`、
`config --analytics` 和知识库更新也涉及写入。

## 仓库地图

| 路径 | 关系 | 说明 |
|---|---|---|
| **`AGENTS.md`** | 你在这里 | 开工顺序与硬性约束 |
| **`specs/`** | 必读 | 净室规格；实现状态见 `_index.md` |
| **`implementation/`** | 战场 | Python 代码 + `TODO.md` |
| `README.md` | 用户门面 | 公开行为变化必须同步 |
| `implementation/README.md` | CLI 契约 | 选择语义、JSON、规则格式 |
| `HANDOFF.md` | 旧入口 | 已并入本页的短跳转 |
| `docs/` | 用户/开发者文档 | 预览、能力地图、架构 |
| `CONTRIBUTING.md` / `SECURITY.md` | 协作与安全 | 净室边界、检查门、漏洞报告 |
| `analysis/` | ❌ 不要看 | 原始分析产物，已隔离 |
| `local/` | 私人交接入口（若存在） | 先读 `local/README.md`；不进公开发行包或 Git |

## 开发前读规格

按 [specs/_index.md](specs/_index.md) 的顺序：

1. `_index.md` — 规格索引与实现状态
2. `00-architecture.md` — 命令树、模块职责
3. `02-scan-points.md` — 扫哪里（路径、模式、安全等级）
4. `01-scan-engine.md` — 任务图、加权进度、三态控制
5. `07-predicate-engine.md` — 忽略/保护谓词
6. `05-algorithms.md` — 大小统计、硬链接、云占位
7. `03-knowledge-base.md` — 规则存储；本项目用明文 JSON
8. `06-system-flow.md` — 端到端流程
9. `04-ipc-protocol.md` — 仅当做特权帮助器时读

OpenClean **自有**的 Agent 接口扩展规格（Agent Runtime v1：对象模型、命令与 I/O、Run Store、
执行不变量、策略包、研究治理、实施路线）在
[specs/agent-runtime/](specs/agent-runtime/_index.md)；它是自有设计，与 00-07 的参考事实分开。

规格记录参考对象的功能事实。本项目实际交付范围以 `_index.md`、根 README 和
`implementation/TODO.md` 为准；高风险能力可以只读或不实现。

## 代码入口

`implementation/openclean/`：

| 领域 | 文件 |
|---|---|
| CLI / JSON | `cli.py`、`redaction.py` |
| 扫描 / 项目 | `engine.py`、`scanpoints.py`、`filesystem.py`、`application_ownership.py` |
| 模型 / 进度 | `models.py`、`task_graph.py`、`progress.py` |
| 规则 | `predicates.py`、`knowledge_base.py`、`knowledge_update.py` |
| 执行 | `cleanup.py`、`macos.py`、`processes.py` |
| 专项扫描 / detector | `docker.py`、`updater.py`、`storage_diagnostics.py`、`workbuddy.py`、`startup_items.py`、`application_languages.py`、`analyzer.py` |
| Agent Runtime | `core/`、`strategies/`、`runtime/`、`actions/`、`packs/`（`inspect`/`show`/`clean --run`/`strategy`） |
| TUI | `tui.py`、`space_tui.py`、`navigator.py` |
| 预览 / 发行 | `scripts/preview_all.py`、`scripts/capture_tui_assets.py`、`scripts/check_release_artifacts.py` |

当前用户态扫描、预览、选择、同卷 Trash、JSON schema v2 和受限 Docker prune 已落地。
另新增 **Agent Runtime v1**（P0 Codex 切片）作为附加命令面：`inspect`/`show`/
`clean --run --finding`/`strategy`、Finding/Run/CleanupPlan、本机私有 Run Store、`codex` pack
（7 条 active/report_only）及 `workbuddy` pack（已观察结构只读，未启用生产动作）；它与经典五域命令**并存**，底层探测器/计量/保护闸/同卷 Trash
执行器复用未重写。
`optimize ram|purgeable` 明确拒绝。特权帮助器、正式知识库服务端、真实 Docker daemon
验收未做。逐项状态见 [docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

## 硬性约束

1. 写操作默认必须有 `--yes`；`critical` 级需双重确认。
2. 不读、不引用、不复制 `analysis/` 到 `implementation/`。
3. `specs/` 是参考事实来源；与实现冲突时改实现或更新规格（并注明）。公开行为以
   README 为准。
4. 运行时保持零第三方依赖；新增依赖必须在 TODO 说明理由。
5. 只考虑 macOS 路径和 API。
6. 不把缺少公开 API、签名或真实环境验收的能力伪装为完成。
7. 写测试只用 `TemporaryDirectory`；不在真实 `HOME` 或真实 Docker daemon 上跑 `--yes`。
8. 不提交 token、私钥、真实用户路径或机器扫描结果。
9. 不创建 tag、Release、PyPI/Homebrew 发布，除非单独授权并完成发行审阅。

涉及 Agent 接口时，先读 [Agent Runtime 当前状态](docs/AGENT_RUNTIME_STATUS.md)。
P0a/P0b 仅描述该扩展的阶段，不代表整个清理产品的阶段；旧规格中的未来模型替换不是
删除经典命令的授权。当前 Agent 生产策略关闭，也不等于经典清理动作全部关闭。

## 认领任务

以用户点名的功能目标为先，结合 [implementation/TODO.md](implementation/TODO.md) 和
[能力地图](docs/CAPABILITIES.md) 核对差距。以下是现有外部前提与能力缺口，不是要求先完成
特权或 Agent 框架、再开发普通功能的固定顺序：

1. `optimize ram|purgeable`：先确认可靠公开接口，不能用 `/usr/sbin/purge` 或制造内存压力冒充
2. 特权帮助器：用户态稳定后再单独建设 SMAppService/XPC 签名链
3. 知识库发布源：自建 HTTPS channel 和公钥；禁止引入原厂私有规则
4. 真实 Docker daemon 验收
5. 跟踪后续公开 CLI 文档，避免把桌面版能力误列为缺口

## 验证

```bash
make check
make test-focused TEST_PATTERN=test_agent_identifiers.py  # 换成受影响测试文件
```

本地默认只跑轻量检查和受影响测试；文档修改只需差异检查。不自动安装或升级开发工具，
不要求 Python 3.13 或重复全量验证。Python 3.11 是当前基线。
GitHub Actions 负责 `make ci-check`、构建、归档审计和 wheel 独立安装验证；
本地仅在排查相关 CI 失败或用户明确要求时按需复现。Ruff 是可选本地工具，精确版本仅供 CI 复现。
Ruff 门禁以 `implementation/pyproject.toml` 的显式基础错误规则为准；不因工具升级自动扩大风格要求。
减轻本地验证不改变执行保护条件；云端未运行时报告待验证，不宣称已通过。

修改公开 CLI、JSON schema 或安全级时，同步测试、README、相关 `docs/` 页和 CHANGELOG。
检查结果以当前 checkout 的命令输出和 exact-head CI 为准，不要把历史测试计数写进文档。

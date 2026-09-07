# open-cleanmymac · AI 开发交接入口

> 给接手开发的 AI：读完这份和它指向的规格，就能开工。
> 目标：用 Python 独立实现 macOS 清理 CLI（`openclean`），保守对齐 CleanMyMac 5 CLI
> 的公开命令面；缺少安全公开接口或签名环境的能力保持 fail-closed。

用户可见行为以 [README.md](README.md) 为准。实现时只依据 `specs/` 和
`implementation/`，不要读 `analysis/`。缺口认领 [implementation/TODO.md](implementation/TODO.md)。

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

OpenClean **自有**的下一代架构契约（Agent Runtime v1：对象模型、命令与 I/O、Run Store、
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
| 专项扫描 / detector | `docker.py`、`updater.py`、`storage_diagnostics.py`、`startup_items.py`、`application_languages.py`、`analyzer.py` |
| Agent Runtime | `core/`、`strategies/`、`runtime/`、`actions/`、`packs/`（`inspect`/`show`/`clean --run`/`strategy`） |
| TUI | `tui.py`、`space_tui.py`、`navigator.py` |
| 预览 / 发行 | `scripts/preview_all.py`、`scripts/capture_tui_assets.py`、`scripts/check_release_artifacts.py` |

当前用户态扫描、预览、选择、同卷 Trash、JSON schema v2 和受限 Docker prune 已落地。
另新增 **Agent Runtime v1**（P0 Codex 切片）作为附加命令面：`inspect`/`show`/
`clean --run --finding`/`strategy`、Finding/Run/CleanupPlan、本机私有 Run Store、`codex` pack
（7 条 active/report_only，未启用生产动作）；它与经典五域命令**并存**，底层探测器/计量/保护闸/同卷 Trash
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

当前实现与长期目标有区别，先读 [Agent Runtime 当前状态](docs/AGENT_RUNTIME_STATUS.md)。
旧规格中的未来模型替换不是本次删除经典命令的授权。

## 认领任务

按 [implementation/TODO.md](implementation/TODO.md) 的优先级：

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

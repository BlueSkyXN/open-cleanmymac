# open-cleanmymac 交接快照

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[安全](SECURITY.md) · [规格索引](specs/_index.md) ·
[实现说明](implementation/README.md)

> 版本：`0.23.0` · 日期：2026-07-31 · 状态：private GitHub Alpha 基线

## 立即上手

```bash
make check
make package
make release-check
```

全部功能的安全演示入口是 `make preview`。它在 `TemporaryDirectory` 中覆盖 19 个场景，
不会扫描或修改真实 `HOME`。用户文档从 [README.md](README.md) 开始，架构见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，逐项状态见
[docs/CAPABILITIES.md](docs/CAPABILITIES.md)，当前缺口见
[implementation/TODO.md](implementation/TODO.md)。

## 当前完成状态

已实现并有自动化覆盖：

- system/developer/ai/trash/project 五域扫描；
- 物理/逻辑大小、硬链接去重、Darwin `SF_DATALESS`/zero-block 占位保护和重叠归属；
- Predicate 组合、JSON KnowledgeBase、ignore、签名更新客户端；
- clean/purge/analyze 的文本、JSON、curses 审阅和用户态执行；精确 `--select` 不继承
  默认或同等级批量选择；
- 同卷 Trash、Trash 永久清空、Docker 三类固定 prune；Trash 创建使用可信父目录 fd，
  rename/replace 后的清理错误不会覆盖已发生副作用；
- 项目分组和 7 天预选、Time Machine 只读提示；
- System Junk 精细候选、Darwin cache、broken startup items；
- ApplicationLanguages 保守只读审计；
- 任务 DAG、加权进度、运行中进程保护；
- JSON schema v2、机器错误 envelope、显式 `--redact-paths` 单文档脱敏、标准 Python 包装
  和 release archive audit；
- Docker scan-time CLI realpath、context/host、endpoint 和 Engine ID binding；CLI
  realpath 变化或执行前即时复核发现不一致会 fail-closed，多 CLI 进程间仍有已记录的
  TOCTOU 边界。

最近完整本地验证：`ruff` 通过，告警升级的 `py_compile` 通过，318 个单元测试通过，
19/19 隔离预览通过且 `real_user_data_modified=false`。这是本地快照；远端状态必须读取
GitHub exact commit 的 Actions 结果后另行确认。

## 有意未实现或受阻

| 能力 | 状态 | 原因 |
|---|---|---|
| SMAppService/XPC helper | 未实现 | 需要 native app、签名、entitlements、notarization、真实安装验收 |
| optimize executor | guarded unavailable | 没有已验证、安全、公开的等价 API |
| 正式知识库 channel | 客户端完成，服务端未配置 | 项目还没有正式 URL/signing key/public key |
| ApplicationLanguages 删除 | 永久锁定于当前版本 | 可能破坏签名 app；通用 executor 不适用 |
| universal binary thinning | 未实现 | 签名、恢复和兼容性风险 |
| Docker 真实 daemon | binding 客户端完成，待单独验收 | prune 不可恢复，preview 不连接真实 daemon；多 CLI 进程不是原子 API 事务 |

不能把这些状态改写成“已完成”。正确结果是显式拒绝、结构化 guard 或 external prerequisite。

## 安全红线

- 不读取、不提交 `analysis/`；不碰 `local/`。
- 不在真实用户数据上运行 `--yes` 测试；写测试只用 `TemporaryDirectory`。
- 不提交 token、私钥、真实用户路径、机器扫描结果或第三方私有规则。
- 不解除 `requires_privilege`、ApplicationLanguages、Docker Volumes 等 fail-closed 标记。
- 不用 `/usr/sbin/purge`、`memory_pressure` 或任意 shell 命令冒充 optimize。
- 不创建 tag、Release、PyPI/Homebrew 发布或 public repo，除非得到单独授权并完成许可证审阅。

## 关键实现入口

| 领域 | 文件 |
|---|---|
| CLI/JSON | `implementation/openclean/cli.py` |
| 扫描/项目 | `engine.py`、`scanpoints.py` |
| 模型/指标 | `models.py` |
| 规则/更新 | `predicates.py`、`knowledge_base.py`、`knowledge_update.py` |
| 执行安全 | `cleanup.py`、`macos.py`、`processes.py` |
| TUI | `tui.py`、`space_tui.py`、`navigator.py` |
| DAG/进度 | `task_graph.py`、`progress.py` |
| 专项扫描 | `docker.py`、`startup_items.py`、`application_languages.py` |
| 全功能预览 | `implementation/scripts/preview_all.py` |
| TUI 文档资产 | `implementation/scripts/capture_tui_assets.py`、`docs/assets/` |
| 归档检查 | `implementation/scripts/check_release_artifacts.py` |

## 包装策略

- wheel：只包含运行时 `openclean` 包和 console entry point；
- sdist：有意包含 tests、preview、release checker、`openclean_cli.py`、README/TODO；
- 两者都禁止包含 `analysis/`、`local/`、缓存、`.DS_Store`、凭据和私钥；
- CI 构建 artifact，但不发布 release/channel。

## 交接后的第一步

1. 读取 `git status`、HEAD、remote 和 exact-head CI，不依赖本快照猜当前状态。
2. 运行 `make check`；只有代码/文档变更后才重跑 package/release-check。
3. 按 `implementation/TODO.md` 认领一个边界明确的切片。
4. 修改行为时同步测试、README/PREVIEW、CHANGELOG 和 capability matrix。

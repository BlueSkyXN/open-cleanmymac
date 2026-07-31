# open-cleanmymac

`open-cleanmymac` 是一个面向 macOS 的 Python 清理 CLI，安装后的命令名为
`openclean`。项目采用净室方法，依据仓库中的功能规格独立实现，不复用参考软件的代码、
私有规则库或商业数据。

当前版本：**0.23.0 Alpha**。用户态扫描、预览、选择、同卷 Trash 执行、空间分析、
TUI、JSON 输出、项目产物清理和受限 Docker prune 已实现。特权帮助器和
`optimize ram|purgeable` 的实际执行器尚不可用；CLI 会明确拒绝，不伪报成功。

> 这是会操作文件的系统工具。默认命令只读，但带 `--yes` 的清理命令、清空 Trash 和
> Docker prune 可能修改或永久删除数据。先运行隔离预览，再阅读
> [安全边界](#安全边界)。

## 30 秒安全预览

要求：macOS、Python 3.11 或更高版本；当前 CI 只验证 Python 3.11。开发检查还需要
`ruff`。

```bash
git clone https://github.com/BlueSkyXN/open-cleanmymac.git
cd open-cleanmymac
make preview
```

`make preview` 不扫描真实 `HOME`，而是在 `TemporaryDirectory` 中构造夹具，预览并
执行 19 个隔离场景，最后报告 `real_user_data_modified=false`。它覆盖五域扫描、四类
`clean`、`purge`、`analyze`、ignore/config 生命周期、临时清理执行，以及两个
`optimize` 安全拒绝。完整说明见 [docs/PREVIEW.md](docs/PREVIEW.md)。

安装 CLI：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./implementation
.venv/bin/openclean --version
```

只读入门命令：

```bash
.venv/bin/openclean scan --json
.venv/bin/openclean clean dev --no-interactive
.venv/bin/openclean purge ~/Projects --no-interactive
.venv/bin/openclean analyze ~ --no-interactive
.venv/bin/openclean ignore list
.venv/bin/openclean config
```

上面的命令不会执行清理。不要在尚未审阅候选时给清理命令添加 `--yes`。

## 能力矩阵

| 能力 | 扫描/预览 | 执行 | 当前边界 |
|---|---:|---:|---|
| system / developer / ai / trash / project 五域 | ✅ | — | `scan` 始终只读 |
| `clean junk\|dev\|ai` | ✅ | ✅ 用户态 | 默认预览；显式 `--yes` 才执行当前选择 |
| `clean trash` | ✅ | ✅ | `confirm`；执行会永久清空内容 |
| `purge [path]` | ✅ | ✅ 用户态 | 旧产物默认预选；普通项移到同卷 Trash |
| `analyze [path]` | ✅ | ✅ 精确选择 | TUI/JSON/行式导航；不删除 Time Machine 快照 |
| Docker daemon 容量 | ✅ | ✅ 受限 | 三类官方 prune 均需 identifier 精确选择；Volumes 拒绝 |
| ApplicationLanguages | ✅ | ❌ | 只读审计；修改签名 app 风险过高 |
| Broken startup items | ✅ | ✅ 用户项 | 系统项需要尚未实现的特权帮助器 |
| Time Machine 本地快照 | ✅ | ❌ | 只显示数量/名称；公开列表不提供精确大小 |
| 签名托管知识库 | ✅ | ✅ 显式更新 | 需用户提供 HTTPS URL 和钉住的公钥；项目不内置服务端 |
| `optimize ram\|purgeable` | ✅ 命令面 | ❌ | `status=unavailable`，退出码 1 |
| SMAppService/XPC 特权清理 | — | ❌ | 需要 native host/helper、签名、entitlements 和真实安装验收 |
| universal binary thinning | — | ❌ | 未实现；不修改签名应用包 |

扫描域的直观含义：

| 域 | 典型内容 |
|---|---|
| system | 用户缓存/日志、诊断报告、Xcode、失效启动项、应用语言只读审计 |
| developer | pip、uv、npm、Cargo、Homebrew、Docker daemon 报告 |
| ai | Claude、Codex、Gemini、OpenCode、Cursor 缓存 |
| project | `node_modules`、`.venv`、`target`、DerivedData 等可重建产物 |
| trash | 当前用户与挂载卷的 Trash 根目录 |

逐项能力来源、实现状态、验证证据和有意排除项见
[docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

这里的“全部功能可预览”指：所有非交互命令族、临时写路径和 guard 状态都能通过隔离
脚本演示；curses TUI 同时有状态机测试和由生产绘制函数生成的确定性 SVG，但不冒充
macOS Terminal 的像素级截图。需要外部签名、真实 Docker daemon、正式知识库服务或
不存在安全公开 API 的能力，会以结构化方式展示为 guarded/unavailable，而不是伪造
执行结果。

## TUI 视觉预览

下图直接调用当前 Clean TUI 的生产绘制函数，以固定合成候选生成；路径、容量和资源均为
演示数据，不读取真实 `HOME`、Docker daemon 或云文件。

![Clean TUI 候选审阅，使用固定合成数据](docs/assets/tui-clean-review.svg)

只读汇总与 Analyze 空间浏览画面见
[全功能隔离预览](docs/PREVIEW.md#合成-tui-视觉预览)。

## CLI 概览

```text
openclean scan [--domain DOMAIN] [--json]
openclean clean [junk|dev|ai|trash] [selection options] [--yes]
openclean purge [PATH] [selection options] [--yes]
openclean analyze [PATH] [--top N] [--select PATH] [--yes]
openclean optimize {ram,purgeable} [--json]
openclean ignore {list,add,remove}
openclean config [--analytics on|off]
openclean cat [--json]
```

连接 TTY 时，`clean`、`purge` 和 `analyze` 默认进入 curses 全屏界面；JSON、管道、
`--no-interactive` 或任何参数化选择 flag 使用非交互流程。`clean`/`purge --select`
进入独立精确模式，不继承默认预选，也不会因 `--include-confirm` 或
`--include-critical` 扩大到同等级其他候选。完整参数以
`openclean <command> --help` 为准。

JSON schema 当前为版本 `2`。扫描结果区分：

- `potential_bytes`：发现的物理占用；
- `reclaimable_bytes`：当前实现可执行候选的物理占用；
- `requires_privilege_bytes`：需要尚未实现的特权能力；
- `unsupported_bytes`：当前明确不支持执行的候选。

JSON 会包含绝对路径、项目名和本机目录结构。把输出附到 issue、日志或工单前应先脱敏。
参数、规则或路径错误在 `--json` 模式下也返回稳定的 JSON error envelope。

## 安全边界

| 操作 | 是否写入 | 可恢复性 |
|---|---:|---|
| `scan`、不带 `--yes` 的 `clean`/`purge`/`analyze` | 否 | 不适用 |
| 普通 `clean`/`purge`/`analyze --yes` | 是 | 通常移动到同卷 Trash，可手工恢复 |
| `clean trash --select EXACT_ROOT --include-confirm --yes` | 是 | 永久删除所选 Trash 内容，不能恢复 |
| Docker prune | 是 | 永久操作；不经过 Trash，必须精确选择资源 identifier |
| `ignore add/remove`、`config --analytics` | 是 | 修改本地 `0600` JSON 配置 |
| `config --update-knowledge` | 网络 + 写入 | 验签、防回滚后原子安装规则 |

执行链会在扫描时、批量预检时和最终移动前重复检查保护规则、inode、owner、挂载点、
云占位、运行中进程和 symlink 边界。macOS 上通过 Darwin `SF_DATALESS`，并辅以
`st_blocks == 0 && st_size > 0` 的保守启发式，在目录枚举前阻止 dataless/疑似占位项；
这不等于识别所有已经 materialized 的 cloud-synced 文件。Poetry/uv 环境变量路径只
允许落在受信缓存根下，
并强制降级为需要精确选择的 `confirm`。Docker 三类 prune 同样不参与默认或批量选择。
包含 symlink ancestor 的路径会被拒绝；普通移动使用逐组件 `O_NOFOLLOW` 的目录 fd 和
Darwin `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)`，原子拒绝覆盖 Trash 中并发出现
的目标。Trash 永久清空只处理最终审计快照，审计后新增项会保留。

`safe`、`confirm`、`critical` 是候选风险级别，不是数据价值保证。用户规则中的
`ignore`/`protect` 是额外保护层，不能代替备份和人工审阅。安全政策见
[SECURITY.md](SECURITY.md)。

## 架构

这是一个模块化单体 CLI：

```text
CLI / TUI / JSON
      │
      ▼
任务 DAG + 加权进度 + 五域扫描编排
      │
      ▼
扫描点 / 动态发现 / 文件计量 / 进程保护
      │
      ▼
Predicate + KnowledgeBase 最外层保护闸
      │
      ▼
Item / ScanIssue / ScanResult
      │
      ├── 只读报告
      └── 显式选择 → 执行前复核 → Trash / Docker 白名单
```

模块职责、数据流、信任边界和未来 XPC 分层见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。净室规格入口是
[specs/_index.md](specs/_index.md)。

## 开发与验证

```bash
python3 -m pip install ruff build
make lint
make test
make preview
make check
make package
make release-check
```

当前本地基线通过 286 个 `unittest` 和 19/19 隔离预览场景；最终事实以 CI 和当前
checkout 实际运行结果为准。CI 在 macOS/Python 3.11 上执行 lint、测试、预览、构建、
归档审计和隔离 wheel 安装，不会发布 PyPI、Homebrew 或 GitHub Release。

贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本记录见
[CHANGELOG.md](CHANGELOG.md)，当前开发缺口见
[implementation/TODO.md](implementation/TODO.md)。

## 仓库边界与合规

| 路径 | 作用 |
|---|---|
| `implementation/` | Python 包、测试、隔离预览与发行检查 |
| `specs/` | 净室功能规格和实现状态 |
| `docs/` | 架构与功能预览文档 |
| `analysis/` | 受隔离的原始分析材料；被 `.gitignore` 排除，禁止提交/读取 |
| `local/` | 本机过程材料；被 `.gitignore` 排除 |

本项目与 MacPaw 或 CleanMyMac 没有关联，也不受其背书。产品名仅用于描述兼容目标和
研究背景。项目不包含参考软件代码、私有 `.cmmkb` 数据、密钥或用户机器扫描结果。

仓库当前**没有选择公共开源许可证**，也没有授权公开复制、修改、分发或发布包。当前
交付目标是 private GitHub 仓库；转为 public、发布 PyPI/Homebrew 或接受外部分发前，
必须单独完成许可证、商标和净室合规决策。

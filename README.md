<div align="center">

# open-cleanmymac

macOS 磁盘清理 CLI · 安装后的命令名为 **`openclean`**

[![CI](https://github.com/BlueSkyXN/open-cleanmymac/actions/workflows/ci.yml/badge.svg)](https://github.com/BlueSkyXN/open-cleanmymac/actions/workflows/ci.yml)
[![Version 0.24.0a2 Alpha](https://img.shields.io/badge/version-0.24.0a2_Alpha-orange)](CHANGELOG.md)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/platform-macOS-111111?logo=apple&logoColor=white)](docs/PREVIEW.md)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

[功能预览](docs/PREVIEW.md)
· [能力地图](docs/CAPABILITIES.md)
· [架构](docs/ARCHITECTURE.md)
· [安全政策](SECURITY.md)
· [Agent 调用](docs/AI_USAGE.md)
· [贡献指南](CONTRIBUTING.md)
· [规格索引](specs/_index.md)

</div>

独立实现 CleanMyMac CLI 的开源平替：对齐核心流程与具体功能，增加自有识别和存储诊断特色，
并方便脚本和 Agent 调用。依据仓库内的功能规格做净室实现，不复用参考软件的代码、私有规则库或商业数据。
用户可以只给出目标与授权范围，由 Agent 完成 JSON 扫描、解释、精确预览、授权内执行和结果核对；
已有个人经验与不能泛化的条件见 [自有经验](docs/EXPERIENCE.md)。
默认只扫描和预览；显式 `--yes` 才会移动或删除文件。缺少安全公开接口的能力保持
fail-closed，不会伪报成功。

> **会操作文件。** 扫描与清理预览不修改候选；`inspect` 写入本机 Run Store，显式配置命令也可能写入。
> 清理、清空 Trash 和 Docker prune 可能永久删除数据。请先跑隔离预览，再阅读 [安全](#安全)。

用户态扫描、预览、选择、同卷 Trash、空间分析和 TUI
已实现；并新增面向 AI Agent 的 **Agent Runtime** 命令面（`inspect`/`show`/
`clean --run --finding`/`strategy`，首个策略包 `codex`），与既有命令**并存**。Docker prune 仅有受限代码路径与隔离验证，真实 daemon 尚未验收。特权帮助器和
`optimize ram / purgeable` 执行器不可用。[GitHub Releases](https://github.com/BlueSkyXN/open-cleanmymac/releases)
是唯一发行渠道，已发布版本和附件以发行页为准；不通过 PyPI、Homebrew 或其他包管理器分发。

<p align="center">
  <img src="docs/assets/tui-clean-review.svg" alt="Clean TUI 候选审阅，使用固定合成数据" width="920">
</p>

上图由当前 Clean TUI 的生产绘制函数生成，使用固定合成候选。更多画面见
[docs/PREVIEW.md](docs/PREVIEW.md)。

> 当前版本 `0.24.0a2`：经典功能保留；Codex/WorkBuddy 专项 inspect 只读与计划预览可用，生产策略动作未启用。
> 完整范围与 JSON/Run 版本见 [Agent Runtime 当前状态](docs/AGENT_RUNTIME_STATUS.md)。

## 快速开始

要求：macOS、Python 3.11+。CI 配置为 macOS 15 / 26 双版本，Python 保持 3.11；
实际通过情况以对应提交的 GitHub Actions 为准，不代表其它系统或 Python 版本已验证。

### 安装 GitHub 预发行包

`0.24.0a2` 为 Alpha，不是全功能稳定版。通过 GitHub CLI 下载 wheel 与校验文件：

```bash
gh release download v0.24.0a2 --repo BlueSkyXN/open-cleanmymac --pattern '*.whl' --pattern '*.tar.gz' --pattern SHA256SUMS
shasum -a 256 -c SHA256SUMS
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps ./open_cleanmymac-0.24.0a2-py3-none-any.whl
.venv/bin/openclean --version
.venv/bin/openclean strategy list --json
```

也可从发行页下载同名附件。运行时零第三方依赖；包文件包含 Python CLI，不包含需要签名的特权 helper。

### 从源码运行与隔离预览

```bash
git clone https://github.com/BlueSkyXN/open-cleanmymac.git
cd open-cleanmymac
make preview
```

`make preview` 不扫描真实 `HOME`。它在临时目录里演示全部非交互命令，并报告
`real_user_data_modified=false`。说明见 [docs/PREVIEW.md](docs/PREVIEW.md)。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./implementation
.venv/bin/openclean --version
.venv/bin/openclean scan --json
.venv/bin/openclean clean dev --no-interactive
# Agent Runtime（附加命令面，与上面命令并存）：
.venv/bin/openclean inspect codex --json
.venv/bin/openclean inspect workbuddy --json
.venv/bin/openclean strategy list
```

这些命令不会执行清理。不要在尚未审阅候选时加 `--yes`。

## 功能

| 能力 | 预览 | 执行 | 当前边界 |
|---|---|---|---|
| 无参数主菜单 | 是 | 否 | Clean/Purge/Analyze/Optimize/Config；`M` More，`Q` 退出；初始化失败退回行式菜单 |
| 五域扫描（system / developer / ai / trash / project） | 是 | 只读 | `scan` 始终只读 |
| `clean junk / dev / ai` | 是 | 用户态 | 默认预览；`--yes` 才执行当前选择 |
| `clean trash` | 是 | 永久删除 | 清空内容，保留 Trash 根 |
| `purge [path]` | 是 | 用户态 | 旧产物默认预选；普通项移到同卷 Trash |
| `analyze [path]` | 是 | critical 精确选择 | 占用不等于垃圾；不跨候选所在卷 |
| Docker daemon 容量 | 是 | 受限 | 三类 prune 需精确选择；Volumes 拒绝；真实 daemon 待验收 |
| 日志 / 缓存 / updater / WorkBuddy 经验结构等诊断 | 是 | 否 | 只读报告；不提供通用删除器 |
| `optimize ram / purgeable` | 命令面 | 否 | `status=unavailable`，退出码 1 |
| 特权系统清理 | — | 否 | 需要尚未实现的签名 helper |
| Agent Runtime（`inspect`/`show`/`clean --run`/`strategy`） | 是 | 当前不启用生产动作 | Codex/WorkBuddy 包均只读；见 [当前状态](docs/AGENT_RUNTIME_STATUS.md) |

扫描域：system（用户缓存、日志、updater、Xcode）、developer（语言与包管理器缓存、
Docker 报告）、ai（AI 工具缓存）、project（可重建产物）、trash（当前用户与挂载卷
Trash）。逐项状态、来源和有意排除项见
[docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

当前不在范围内：Desktop GUI、菜单栏、后台 agent、应用卸载、恶意软件扫描。

## 命令

直接运行 `openclean`，TTY 下使用方向键和 Enter 进入主流程，`M` 打开 More/Cat。
子任务结束后按 Enter 返回菜单。主菜单只进入审阅/预览，不自动附加 `--yes`；
Optimize 显示不可用原因，不执行维护。非 TTY 无参数启动仍只输出帮助。

Clean/Purge 的逐项列表按 `I` 查看只读详情，方向键滚动、Esc 返回，选择保持不变。
详情显示已有年龄、句柄、保留期、SQLite、updater 等证据，不额外扫描。
只读诊断不是可清理对象；内部空闲页、年龄桶或逻辑上限也不是已经释放的空间。

```text
openclean scan [--domain DOMAIN] [--json [--redact-paths]]
openclean clean [junk|dev|ai|trash] [selection options] [--yes]
openclean purge [PATH] [selection options] [--yes]
openclean analyze [PATH] [--top N] [--select PATH] [--yes]
openclean optimize {ram,purgeable} [--json]
openclean ignore {list,add,remove}
openclean config [--analytics on|off]
openclean cat [--json]
```

连接 TTY 时，`clean`、`purge` 和 `analyze` 默认进入 curses 界面。JSON、管道、
`--no-interactive` 或任何参数化选择 flag 走非交互流程。完整参数、选择语义和 JSON
schema v2 以 `openclean <command> --help` 和
[implementation/README.md](implementation/README.md) 为准。

## 安全

| 操作 | 是否写入 | 可恢复性 |
|---|---|---|
| `scan`、不带 `--yes` 的 `clean` / `purge` / `analyze` | 否 | 不适用 |
| 普通 `clean` / `purge` / `analyze --yes` | 是 | 通常移到同卷 Trash |
| `clean trash --select EXACT_ROOT --include-confirm --yes` | 是 | 永久删除所选 Trash 内容 |
| Docker prune | 是 | 永久操作，不经过 Trash |
| `ignore add/remove`、`config --analytics` | 是 | 修改本地 `0600` JSON 配置 |
| `config --update-knowledge` | 网络 + 写入 | 验签后原子安装规则 |

`safe`、`confirm`、`critical` 是候选风险级别，不是数据价值保证。路径竞态、Trash
身份、Docker binding 和知识库安装细节见 [SECURITY.md](SECURITY.md)。

已发布 `0.24.0a1` 的 Agent `clean --redact-paths` 存在嵌套计划脱敏遗漏，不应直接公开其输出。
`0.24.0a2` 修复此问题，见 [CHANGELOG](CHANGELOG.md)；旧安装包不会自动更新。升级后重新 inspect，不沿用旧版本 Run 执行。

## 开发

```bash
make check
make test-focused TEST_PATTERN=test_agent_identifiers.py  # 按修改选择测试文件
```

本地默认使用 Python 3.11，无需安装开发依赖或其他 Python 版本。`make check` 仅检查语法和
CLI 启动；全量测试、Ruff、构建、归档和独立安装验证由 GitHub Actions 执行，结果以 exact-head CI 为准。
需要复现云端失败时才安装 `requirements-dev.txt` 并运行相应目标。文档分层见
[CONTRIBUTING.md](CONTRIBUTING.md)；缺口见
[implementation/TODO.md](implementation/TODO.md)。

## 仓库边界

| 路径 | 作用 |
|---|---|
| `implementation/` | Python 包、测试、隔离预览与发行检查 |
| `specs/` | OpenClean 行为与验收规格；参考依据、条件设计和未批准提案分别标注 |
| `docs/` | 架构、能力地图与功能预览 |
| `analysis/` | 受隔离的原始分析材料；禁止提交/读取 |
| `local/` | 本机过程材料；被 `.gitignore` 排除 |

本项目与 MacPaw 或 CleanMyMac **没有关联**，也不受其背书。产品名仅用于描述兼容目标
和研究背景。

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)。当前公开的是源码与 CI 基线，
不是已签名的产品发布。

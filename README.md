<div align="center">

# open-cleanmymac

macOS 磁盘清理 CLI · 安装后的命令名为 **`openclean`**

[![CI](https://github.com/BlueSkyXN/open-cleanmymac/actions/workflows/ci.yml/badge.svg)](https://github.com/BlueSkyXN/open-cleanmymac/actions/workflows/ci.yml)
[![Version 0.23.0 Alpha](https://img.shields.io/badge/version-0.23.0_Alpha-orange)](CHANGELOG.md)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/platform-macOS-111111?logo=apple&logoColor=white)](docs/PREVIEW.md)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

[功能预览](docs/PREVIEW.md)
· [能力地图](docs/CAPABILITIES.md)
· [架构](docs/ARCHITECTURE.md)
· [安全政策](SECURITY.md)
· [AI 只读调用](docs/AI_USAGE.md)
· [贡献指南](CONTRIBUTING.md)
· [规格索引](specs/_index.md)

</div>

依据仓库内的功能规格做净室实现：不复用参考软件的代码、私有规则库或商业数据。
默认只扫描和预览；显式 `--yes` 才会移动或删除文件。缺少安全公开接口的能力保持
fail-closed，不会伪报成功。

> **会操作文件。** 不带 `--yes` 的命令只读；清理、清空 Trash 和 Docker prune 可能
> 永久删除数据。请先跑隔离预览，再阅读 [安全](#安全)。

当前基线是 **0.23.0 Alpha**。用户态扫描、预览、选择、同卷 Trash、空间分析和 TUI
已实现。Docker prune 仅有受限代码路径与隔离验证，真实 daemon 尚未验收。特权帮助器和
`optimize ram / purgeable` 执行器不可用。GitHub Release 是唯一计划的正式发布渠道；当前尚未创建
Release，也不计划通过 PyPI、Homebrew 或其他包管理器分发。

<p align="center">
  <img src="docs/assets/tui-clean-review.svg" alt="Clean TUI 候选审阅，使用固定合成数据" width="920">
</p>

上图由当前 Clean TUI 的生产绘制函数生成，使用固定合成候选。更多画面见
[docs/PREVIEW.md](docs/PREVIEW.md)。

## 快速开始

要求：macOS、Python 3.11+。当前 CI 只验证 Python 3.11。

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
```

这些命令不会执行清理。不要在尚未审阅候选时加 `--yes`。

## 功能

| 能力 | 预览 | 执行 | 当前边界 |
|---|---|---|---|
| 五域扫描（system / developer / ai / trash / project） | 是 | 只读 | `scan` 始终只读 |
| `clean junk / dev / ai` | 是 | 用户态 | 默认预览；`--yes` 才执行当前选择 |
| `clean trash` | 是 | 永久删除 | 清空内容，保留 Trash 根 |
| `purge [path]` | 是 | 用户态 | 旧产物默认预选；普通项移到同卷 Trash |
| `analyze [path]` | 是 | critical 精确选择 | 占用不等于垃圾；不跨候选所在卷 |
| Docker daemon 容量 | 是 | 受限 | 三类 prune 需精确选择；Volumes 拒绝；真实 daemon 待验收 |
| 日志 / 缓存 / updater 等诊断 | 是 | 否 | 只读报告；不提供通用删除器 |
| `optimize ram / purgeable` | 命令面 | 否 | `status=unavailable`，退出码 1 |
| 特权系统清理 | — | 否 | 需要尚未实现的签名 helper |

扫描域：system（用户缓存、日志、updater、Xcode）、developer（语言与包管理器缓存、
Docker 报告）、ai（AI 工具缓存）、project（可重建产物）、trash（当前用户与挂载卷
Trash）。逐项状态、来源和有意排除项见
[docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

当前不在范围内：Desktop GUI、菜单栏、后台 agent、应用卸载、恶意软件扫描。

## 命令

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

## 开发

```bash
python3 -m pip install -r requirements-dev.txt
make check
make package
make release-check
```

检查门以当前 checkout 的 `make check` 为准。文档分层见
[CONTRIBUTING.md](CONTRIBUTING.md)；缺口见
[implementation/TODO.md](implementation/TODO.md)。

## 仓库边界

| 路径 | 作用 |
|---|---|
| `implementation/` | Python 包、测试、隔离预览与发行检查 |
| `specs/` | 净室功能规格 |
| `docs/` | 架构、能力地图与功能预览 |
| `analysis/` | 受隔离的原始分析材料；禁止提交/读取 |
| `local/` | 本机过程材料；被 `.gitignore` 排除 |

本项目与 MacPaw 或 CleanMyMac **没有关联**，也不受其背书。产品名仅用于描述兼容目标
和研究背景。

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)。当前公开的是源码与 CI 基线，
不是已签名的产品发布。

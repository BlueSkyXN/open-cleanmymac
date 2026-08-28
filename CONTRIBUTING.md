# Contributing

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[安全](SECURITY.md) · [规格索引](specs/_index.md)

本仓库以 [GNU GPL v3](LICENSE) 公开。提交变更即按该许可证贡献代码与文档。请先阅读净室
边界和 [SECURITY.md](SECURITY.md)；不要把 `analysis/` 或真实用户数据带进仓库。

## 净室边界

- 只依据 `specs/`、公开文档和可独立验证的通用 macOS 行为实现。
- 不读取、提交、引用或复制 `analysis/`；不把 `local/` 过程材料带入代码或文档。
- 不提交参考软件代码、反编译表达、私有规则库、商业扫描指纹或原厂数据。
- 新扫描点必须说明公开来源或通用命名依据，并采用保守安全级。
- 不在 issue、测试、截图、日志或 commit 中加入真实用户路径、凭据或机器扫描结果。

## 开发环境

要求 macOS 和 Python 3.11+；当前 CI 只验证 Python 3.11：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ruff build
make check
```

运行时包保持零第三方依赖。新增运行时依赖前必须说明标准库方案为何不足、供应链影响、
许可证和移除方案。

## 变更原则

1. 保持最小充分改动，不顺手重写无关模块。
2. 修改公开 CLI、JSON schema 或安全级时同步更新测试、README、PREVIEW 和 CHANGELOG。
3. 任何新写操作默认只预览，并设计显式授权、可恢复性、执行前 live 复核和结果报告。
4. 不把缺少公开 API、签名或真实环境验收的能力伪装为完成；使用 fail-closed 状态。
5. 对扫描范围变宽、环境路径、symlink、挂载点、Docker 和特权边界增加负向测试。
6. 测试写操作只能使用 `TemporaryDirectory`；不得用真实 `HOME` 或真实 Docker daemon。

## 检查门

```bash
make lint
make test
make preview
make package
make release-check
git diff --check
```

wheel 只含运行时包；sdist 有意包含 tests、preview、release checker 和 checkout wrapper。
归档不得包含 `analysis/`、`local/`、缓存、`.DS_Store`、凭据或私钥。

## 提交说明

提交信息使用简洁的祈使句，说明用户可见结果，例如：

```text
Harden environment-derived cleanup paths
Document guarded feature preview
```

Pull request 应列出行为变化、安全影响、验证命令与结果、未验证的外部边界。不要宣称 CI、
真实 Docker、特权安装或发布成功，除非提供 exact commit 的读取证据。

## 文档分层

| 文档 | 读者 | 写什么 |
|---|---|---|
| [README.md](README.md) | 用户、审阅者 | 安装、命令、能力边界、许可证 |
| [docs/PREVIEW.md](docs/PREVIEW.md) | 想先看效果的人 | 隔离预览、合成 TUI、退出码 |
| [docs/CAPABILITIES.md](docs/CAPABILITIES.md) | 要核对范围的人 | 能力状态、来源、验证证据 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 开发者 | 模块、数据流、信任边界 |
| [SECURITY.md](SECURITY.md) | 安全审阅 | 威胁模型、报告渠道 |
| [specs/_index.md](specs/_index.md) | 实现者 | 净室规格与实现缺口 |
| [AGENTS.md](AGENTS.md) | 接手的 AI | 开工顺序与硬性约束 |

修改公开 CLI、JSON schema 或安全级时，至少同步 README、相关 `docs/` 页和 CHANGELOG。

## 许可证

贡献按 [GNU GPL v3](LICENSE) 授权。不要在提交中夹带不兼容许可证的代码、私有规则或未授权素材。

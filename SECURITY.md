# Security Policy

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[规格索引](specs/_index.md) · [实现说明](implementation/README.md)

`openclean` 会枚举、移动，并在特定命令下永久删除文件。安全缺陷可能导致数据丢失。
请不要在公开 issue 中提交可利用细节、真实目录树、规则文件、用户名、token 或其他隐私数据。

当前公开源码基线是 `0.23.0` Alpha，许可证为 [GPL-3.0](LICENSE)。GitHub Release 是唯一计划的
正式发布渠道，当前尚未创建 Release；项目不通过 PyPI、Homebrew 或其他包管理器分发。在 Release
创建前，只有仓库 exact commit 和 CI artifact 能作为当前构建来源。

路径竞态、Trash 身份、Docker binding、知识库安装和只读诊断的实现细节见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。CLI 选择语义与 JSON 字段见
[implementation/README.md](implementation/README.md)。

## 报告漏洞

优先使用 GitHub 仓库的 **Private vulnerability reporting / Security advisory**。在 private
report 中提供：受影响版本和 macOS 版本、最小复现步骤、预期/实际结果、是否需要
`--yes`、是否涉及 symlink/挂载点/并发替换，以及只含合成路径的日志。

如果 private reporting 尚未启用，请只创建一个不含技术细节的普通 issue，说明需要维护者
建立私密沟通渠道。仓库当前没有公布安全邮箱，不要把敏感材料发往猜测的地址。

## 当前支持范围

| 版本 | 状态 |
|---|---|
| `0.23.x` | 当前 Alpha 基线 |
| `<0.23` | 不维护；请先复现于当前版本 |

支持范围覆盖当前 Git 基线，不覆盖自行修改后的 fork 或未审阅的第三方构建。

## 威胁模型

默认信任当前用户明确授权的操作，不信任扫描与执行之间的文件系统变化、环境变量、
Docker context 元数据、未签名规则和未实现的特权路径。

| 威胁 | 默认对策 |
|---|---|
| 误删用户数据 | 扫描/预览只读；写操作必须 `--yes`；`confirm`/`critical` 需额外授权 |
| 扩大选择范围 | `--select` 从空集开始；不可执行项不能被参数解锁 |
| 路径替换 / symlink | 拒绝目标与 ancestor symlink；执行前复核 device/inode/owner/mount |
| 清空 Trash / Docker prune | 永久操作，不经过可恢复 Trash；prune 需精确 identifier |
| 云占位误删 | dataless/疑似占位不计入可回收量且不可执行 |
| 规则投毒 | 知识库只接受显式 HTTPS、钉住公钥、递增 sequence 和有效签名 |
| 信息泄漏 | 默认 JSON 含绝对路径；分享时用 `--redact-paths`；错误 envelope 不带 traceback |
| 特权越权 | 无 helper 时特权项 fail-closed；不用 sudo wrapper 冒充 XPC |

## 安全默认

- 默认扫描和预览只读；ignore/config 写入只由对应的显式配置子命令触发。
- 用户态普通文件通常移动到同卷 Trash；清空 Trash 和 Docker prune 是永久操作。
- KnowledgeBase 保护闸在扫描和执行前重复运行。
- 特权路径、Docker volumes、ApplicationLanguages 修改和 universal binary thinning
  保持 fail-closed。
- Full Disk Access、admin helper 和 SIP 是不同能力，不互相替代。

## 已知边界

- 当前没有 SMAppService/XPC helper，不能清理需要 admin helper 的系统项。
- Python 用户态进程无法对同 UID 恶意进程提供绝对竞态隔离。
- `SF_DATALESS` 和 zero-block 启发式不能识别所有已经 materialized 的 cloud-synced 文件。
- Docker CLI 的 identity probe 与 prune 是不同进程；即时复核缩小窗口，但不提供同一 API
  connection 的原子绑定。realpath 复核与 `exec` 也不是原子操作。
- `complete=true` 只表示没有 blocking issue；调用方还应检查所有 `issues`。
- Docker prune、Trash 永久清空和未来任何特权操作都需要独立风险评估。

## 安全测试要求

修复文件操作或规则边界时，至少覆盖：

- 根路径、home、home 祖先、挂载点和跨卷路径；
- 目标 symlink、ancestor symlink、扫描后替换和 inode 变化；
- 保护路径与保护后代、云占位和运行中进程；
- 未带 `--yes`、批量选择扩大、critical 二次确认；
- Trash 与 Docker 的可恢复性差异；
- JSON error envelope 不泄漏 traceback 或密钥内容。

不得使用真实用户数据作为公开 fixture 或测试快照。专项诊断、updater 和知识库安装的测试
面见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与仓库测试。

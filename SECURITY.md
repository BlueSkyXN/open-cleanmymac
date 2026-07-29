# Security Policy

`openclean` 会枚举、移动和在特定命令下永久删除文件，因此安全缺陷可能导致数据丢失。
请不要在公开 issue 中提交可利用细节、真实目录树、规则文件、用户名、token 或其他隐私数据。

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

项目尚未发布 PyPI/Homebrew/GitHub Release。只有仓库 exact commit 和 CI artifact 能作为
当前构建来源。

## 安全模型

- 默认扫描和预览只读；清理候选文件必须显式 `--yes`。ignore/config 写入只由对应的
  显式配置子命令触发。
- `confirm`/`critical` 需要额外选择授权；不可执行项不能被参数强制解锁。
- 用户态文件通常移动到同卷 Trash；清空 Trash 和 Docker prune 是永久操作。
- KnowledgeBase 保护闸在扫描和执行前重复运行。
- 扫描和执行拒绝 symlink 目标与 symlink ancestor，并复核 device/inode/owner/mount。
- fd-relative `O_NOFOLLOW`/`renameat` 用于普通 Trash 移动；批量预检失败时不开始整批执行。
- 环境变量缓存根受可信目录约束，并要求精确选择。
- 特权路径、Docker volumes、ApplicationLanguages 修改和 universal binary thinning保持
  fail-closed。
- 托管知识库只接受显式 HTTPS、钉住公钥、递增 sequence 和有效签名。

## 已知边界

- 当前没有 SMAppService/XPC helper，不能清理需要 admin helper 的系统项。
- Full Disk Access、admin helper 和 SIP 是不同能力；本项目不把其中一个当成另一个。
- Python 用户态进程无法对同 UID 恶意进程提供绝对竞态隔离。
- `complete=true` 只表示没有 blocking issue；调用方还应检查所有 `issues`。
- JSON 输出含完整绝对路径，可能暴露用户名、项目名和目录结构。
- Docker prune、Trash 永久清空和未来任何特权操作都需要独立风险评估。

## 安全测试要求

修复文件操作或规则边界时，至少覆盖：

- 根路径、home、home 祖先、挂载点和跨卷路径；
- 目标 symlink、ancestor symlink、扫描后替换和 inode 变化；
- 保护路径与保护后代、云占位和运行中进程；
- 环境变量指向 Documents、`/`、受信缓存根和受保护路径；
- 未带 `--yes`、批量选择扩大、critical 二次确认；
- Trash 与 Docker 的可恢复性差异；
- JSON error envelope 不泄漏 traceback 或密钥内容。

不得使用真实用户数据作为公开 fixture 或测试快照。

# Security Policy

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[规格索引](specs/_index.md) · [实现说明](implementation/README.md)

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
- `clean`/`purge --select` 从空选择集开始，不继承默认项；tier flag 在精确模式中只解锁
  指定目标，不批量扩大选择范围。`--select` 与 `--all` 被拒绝组合。
- 用户态文件通常移动到同卷 Trash；清空 Trash 和 Docker prune 是永久操作。
- Docker Build Cache、Images、Containers 均要求 identifier 精确选择，不参与默认或批量
  选择。actionable 候选还必须携带内部 scan-time binding；容量读取和 prune 使用明确
  context 或 effective host，执行前重新复核 endpoint、`SkipTLSVerify` 和 Engine ID。
  binding 缺失、畸形，或执行前即时复核发现不一致时均 fail-closed。
- `--redact-paths` 只在最终 JSON 序列化边界替换绝对路径和相关自由文本；默认精确路径
  合同保持不变，脱敏输出明确标记为不可用于 selector replay。
- KnowledgeBase 保护闸在扫描和执行前重复运行。
- 扫描和执行拒绝 symlink 目标与 symlink ancestor，并复核 device/inode/owner/mount。
- fd-relative `O_NOFOLLOW` 和 Darwin `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)`
  用于普通 Trash 移动；目标竞态时拒绝覆盖，批量预检失败时不开始整批执行。
- 普通 Trash 目标目录必须属于当前用户并使用私有权限；新建目录在可信父目录 fd 下相对
  创建，no-follow 打开并绑定 path/fd identity 后才执行 `fchmod`。最终目录 fd 会再次复核
  device/inode/owner/mode，拒绝 symlink、外置卷共享 `.Trashes` 下预先放置或并发替换的
  用户目录。
- Trash rename 成功后的 fd 清理错误返回 `partial` 并保留已移动事实；两个目录 fd 独立
  尝试关闭，不会让第一个 close 错误阻止第二个。
- Trash 永久清空只处理最终审计得到的 inode 快照；审计后新增项保留，部分删除以
  `partial` 明确报告不可逆副作用。
- 后代目录审计使用 no-follow fd + `fstat` + `scandir(fd)`，拒绝扫描后替换的 symlink、
  类型、owner、device 或 dataless 变化。
- 环境变量缓存根受可信目录约束，并要求精确选择。
- macOS `SF_DATALESS` 在目录枚举和最终移动前优先检查；zero-block 规则作为保守兜底，
  dataless/疑似云占位对象不计入可回收空间且不可执行。
- 特权路径、Docker volumes、ApplicationLanguages 修改和 universal binary thinning保持
  fail-closed。
- 托管知识库只接受显式 HTTPS、钉住公钥、递增 sequence 和有效签名；sequence/key 检查
  与安装由稳定 `0600` 目标锁跨进程序列化。`os.replace` 是安装提交边界，后置目录同步或
  fd 清理只做 best effort，不会把已安装规则伪报为未安装失败。
- Docker prune 一旦启动，timeout 或非零退出按副作用未知的 `partial` 报告，不会假定
  daemon 尚未删除任何资源。

## 已知边界

- 当前没有 SMAppService/XPC helper，不能清理需要 admin helper 的系统项。
- Full Disk Access、admin helper 和 SIP 是不同能力；本项目不把其中一个当成另一个。
- Docker 标准 CLI 的 identity probe 与 prune 是不同进程；显式 target 和即时复核会缩小
  context metadata TOCTOU 窗口，但不宣称具有同一 API connection 的原子绑定保证。
- Python 用户态进程无法对同 UID 恶意进程提供绝对竞态隔离。
- `SF_DATALESS` 和 zero-block 启发式不能识别所有已经 materialized 的 cloud-synced 文件；
  当前只承诺 dataless/疑似占位保护，不承诺完整云同步来源识别。
- `complete=true` 只表示没有 blocking issue；调用方还应检查所有 `issues`。
- 默认 JSON 输出含完整绝对路径，可能暴露用户名、项目名和目录结构；分享时应显式使用
  `--redact-paths`，并注意 URL、snapshot name 和 process marker 不属于该 profile。
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

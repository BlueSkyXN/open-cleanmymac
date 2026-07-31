# 开发任务与能力缺口

[README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md) ·
[实现说明](README.md)

当前基线：`openclean 0.23.0`，对齐 CleanMyMac CLI v1.0.0 Public Beta 的公开命令面。
本清单只保留尚未完成或需要外部验收的工作；已完成功能见根 README、CHANGELOG 和测试。
CleanMyMac Desktop 的应用卸载、恶意软件扫描等不是本项目 CLI 对齐目标。

## P0：发布前必须保持的阻断边界

### 1. SMAppService/XPC 特权帮助器

**状态：外部前提未满足，当前 fail-closed。**

需要完整 Xcode、native host app/helper、Apple Developer 签名身份、Team ID、entitlements、
notarization 和真实安装/升级/回滚验收。Python wheel 本身不能安全承载该能力。

实现前必须先完成：

- 基于 audit token 的 designated requirement 双向校验，而不是只比 Team ID；
- 有版本和大小限制的协议，超时、取消、幂等和错误分类；
- 领域操作白名单，禁止通用 `{delete, absolute_path}`；
- helper 端重新发现和验证允许的 root/相对目标，不信任 CLI 的安全标记；
- fd-based no-follow、device/inode/owner/mount 检查和执行前业务重判定；
- FDA、admin helper、SIP unsupported 三类能力分别建模；
- 安装、升级、卸载、失效签名和回滚测试。

依据：[specs/04-ipc-protocol.md](../specs/04-ipc-protocol.md)。在这些门槛完成前，
`requires_privilege=true` 项必须保持 `actionable=false`。

### 2. `optimize ram|purgeable` executor

**状态：命令树和 JSON refusal 已完成；实际执行器不可用。**

- `/usr/sbin/purge` 需要权限、语义是磁盘缓存，不等价于释放匿名内存；不能套用。
- `memory_pressure` 或主动制造内存压力不是安全产品实现；不能伪装成功。
- 尚未找到可验证、安全、公开的 generic purgeable-space 释放 API。
- 任何未来 executor 都必须遵守默认只读/显式授权，并有可测量 before/after 证据。

在接口事实变化前，保持 `status=unavailable` 和退出码 1 是正确行为。

## P1：需要项目资源或真实环境

### 3. 正式知识库发布 channel

客户端 HTTPS、签名、大小限制、跨进程防回滚、公钥钉扎、key rotation 和原子安装已完成。
仍需项目自己拥有并审计：

- HTTPS 发布服务；
- 离线保管的 signing private key；
- 可提交的正式 public PEM；
- sequence/撤回/rotation/灾备流程；
- 完全由公开信息和本项目维护的规则数据。

禁止引入原厂 `.cmmkb`、私有规则或未知公钥。没有正式材料前不设置默认 URL。

### 4. Docker 真实 daemon 联调

隔离测试已覆盖解析、分级、命令白名单和报告，但 preview 不连接真实 daemon。需要在明确
授权的测试 daemon 上分别验证：

- `docker system df --format json` 不同版本输出；
- Build Cache、Images、Containers 三条 prune 的 before/after 和错误报告；
- 当前客户端已把扫描与执行绑定到明确 context/effective host、endpoint TLS mode 和
  Engine ID，并在 prune 前复核；仍需用真实 context、`DOCKER_HOST`、TLS 和 Engine reset
  验证行为。标准 CLI 多进程无法提供同一 API connection 的原子 precondition；
- 运行中 container、并发 daemon 变化和超时；
- Local Volumes 永远不可执行。

真实 prune 不可通过 Trash 恢复，不能在普通开发 daemon 或用户数据上自动跑。

### 5. macOS/Python 兼容矩阵

当前 CI 基线是 `macos-14 + Python 3.11`。在扩大支持声明前，增加 Python 3.12/3.13 和
目标 macOS 版本的真实验证，重点检查 curses、`st_blocks`、`getconf`、`tmutil`、mount、
Trash 权限和 OpenSSL CLI 差异。

另需在专用测试账号/隔离 File Provider 数据上验证 `SF_DATALESS`：扫描前后占位状态不变、
dataless 目录不触发枚举或下载，并覆盖 iCloud Drive 与至少一个第三方 provider。不要在
真实用户文件上通过 evict/Remove Download 制造测试夹具。

## P2：保守增强

### 6. 权限能力模型

当前 `requires_privilege` 已能 fail-closed，但未来应把执行能力细分为：

- `user`
- `exact_user_confirmation`
- `full_disk_access`
- `admin_helper`
- `sip_unsupported`
- `signed_bundle_mutation_unsupported`

这项重构必须保持现有 JSON 兼容或通过 schema 版本升级明确变更。

### 7. ApplicationLanguages / universal binary

ApplicationLanguages 只读审计已经完成，并故意固定 `critical + actionable=false`。不要把
通用 cleanup executor 直接用于 `.app`，否则可能移动整个应用或破坏 code signature。

universal binary thinning 未实现。若未来仅做审计，需要结构化记录 Mach-O slices、父 app、
签名状态和兼容性；任何写入必须另有原子输出、恢复、签名和 Rosetta/plugin 验收方案。

### 8. Countable 进度与任务控制聚合

当前已实现固定权重百分比、不可变快照、任务成功/失败/取消终态和共享三态协作控制；
`processed_items` 仍是启发式进度输入，不是已知总量的 Countable 完成数/总数。后续若 UI
需要完整对齐规格 01，还需为可计数任务建模 total，并提供每任务 Control、引擎级聚合和
Control 状态 observer；不能把进度 callback 当作 Control observer。

## 当前已完成的交付门

- 五域扫描、物理/逻辑大小、硬链接去重、Darwin `SF_DATALESS`/zero-block 占位保护、
  重叠归属；
- Predicate/KnowledgeBase、ignore lifecycle、签名托管规则客户端；
- clean/purge/analyze 预览、TUI、精确选择、用户态执行和报告；`--select` 不继承默认
  预选，tier flag 只作为精确目标的风险授权；
- 同卷 Trash、Trash 清空、Docker 固定 prune、运行中进程保护；
- 任务 DAG、加权进度、动态扫描并发；
- System Junk、Darwin cache、broken startup items、Time Machine、ApplicationLanguages；
- JSON schema v2 和统一机器错误 envelope；重复 domain/root 去重，显式 project root
  做存在性、目录类型和 symlink 参数校验；
- 显式 `--redact-paths` 单文档 opaque refs，覆盖成功/失败 JSON 和解析前错误；
- Docker scan-time target binding 与 prune 前 context/host、endpoint、Engine ID 复核；
- 19 场景隔离预览、macOS CI、wheel/sdist 和归档审计。

## 提交前验证

```bash
make check
make package
make release-check
git diff --check
```

任何真实 `--yes`、Docker prune、网络知识库更新、特权安装或公共发布都需要单独授权和
对应环境证据。

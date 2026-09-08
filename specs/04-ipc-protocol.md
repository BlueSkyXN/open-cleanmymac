# 04 · 特权清理边界与 helper 条件设计

> 文档 ID：OC-04 · 修订：2 · 更新：2026-09-08
> 状态：当前拒绝契约 baseline；未来 native host/helper 协议 change-pending。
> 来源：SRC-GOAL、SRC-CONTRACT、[TODO](../implementation/TODO.md)；不是参考产品的 IPC 协议副本。

## 1. 当前产品行为

REQ-PRIV-001：没有已验证的 native host/helper、签名与安装链时，
需要特权的候选只能报告，保持 `requires_privilege=true`、`actionable=false`。
`--yes`、critical 确认、Agent Strategy 或 root 身份都不能使未实现能力变成已交付能力。
VAL-PRIV-001：扫描可解释原因；经典/Agent 执行均拒绝，没有 sudo shell 替代路径。

当前 Python wheel 没有特权服务。普通用户态清理不依赖此服务，
也不需要先开发 helper 才能改进其他产品功能。

## 2. 权限不能混为一谈

- 当前用户的可访问性、Full Disk Access、管理员权限、SIP 与签名 bundle 变更是不同边界。
- 目录以 `/Library` 或 `/private/var` 开头不能单独确定是否可执行；按具体资源判定。
- root 不等于 File Provider/FDA/SIP/签名限制全部消失。
- 不要求用户为一个尚无 executor 的能力修改系统设置。

这些是设计约束，不新增公共权限枚举或改变现有 JSON。

## 3. 未来专项的输入与非目标

只有用户批准具体操作、目标范围和签名/测试环境后，才制定可执行 native 规格。
需核对当时 Apple 官方 API、最低 macOS、注册方式和 entitlements；
不把未经验证的 Swift API 拼写或参考软件内部类型写成可直接使用的接口。

待交付物应包括：host/helper 身份、领域操作与参数 schema、授权模型、
安装/升级/卸载/回滚流程、威胁模型、正反样本和真实安装证据。
本篇不预定 Mach service 名、Team ID、消息 JSON 格式或后台服务产品形态。

## 4. 启用时不得降低的要求

以下仅在该专项获批时成为实现验收，不代表已存在 executor：

| 需求 | 条件设计 | 对应验收 |
|---|---|---|
| REQ-PRIV-002 身份 | 基于 audit token 与预期 designated requirement 双向校验；不能只比 Team ID | VAL-PRIV-002：错误签名/伪造 peer/失效身份拒绝 |
| REQ-PRIV-003 服务端授权 | helper 自行验证固定 root、相对目标与领域条件，不信任 CLI 的 safety/路径判断 | VAL-PRIV-003：任意路径、未知动作、参数注入和越界请求拒绝 |
| REQ-PRIV-004 路径复核 | no-follow、device/inode/owner/mount 与业务状态在操作前重判 | VAL-PRIV-004：替换目标、祖先 symlink、跨卷、恢复有效的启动项拒绝 |
| REQ-PRIV-005 协议失败 | 定义版本/大小上限、超时、取消、幂等与稳定错误；不假设断线等于没执行 | VAL-PRIV-005：重复请求/断线/超限/版本不匹配结果可解释 |
| REQ-PRIV-006 生命周期 | 安装、升级、签名变化、卸载和回滚都有真实 macOS 验证 | VAL-PRIV-006：失败不遗留无约束特权入口；host/helper 不兼容拒绝 |

禁止通用文件删除、任意写入、任意 shell 或跨用户批量处理接口。
不使用 sudo 包装器绕过上述设计；不将对方使用 XPC 当成必须复刻其内部消息格式的理由。

## 5. 验证边界

当前拒绝行为入口：[cleanup.py](../implementation/openclean/cleanup.py)、
[test_cleanup.py](../implementation/tests/test_cleanup.py)、
[test_agent_review_execution.py](../implementation/tests/test_agent_review_execution.py)。
其余验收尚无对应 native 实现，不能标为通过。发布门槛与外部前提统一见 TODO。

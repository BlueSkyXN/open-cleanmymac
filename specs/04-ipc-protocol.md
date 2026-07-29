# 04 · IPC / 特权操作协议规格

> 来源：`analysis/raw/IPCProtocol.txt`（Swift/ObjC 类型事实）。
> 描述 CLI 与特权帮助器/主程序通信的"协议形状"，供独立实现参考。不含原代码。

## 1. 为什么需要 IPC

清理涉及两类操作：
- **用户态即可完成**：删 `~/Library/Caches` 下自有文件。
- **需提权**：删 `/Library`、`/private/var`、他人文件、受 SIP 保护边界外的系统区。

CLI 把"需提权的操作"委托给一个**特权帮助器（Privileged Helper）**，
两者通过 **XPC**（macOS 原生安全 IPC）通信。CLI 也可能与**主程序**交换能力/许可状态。

## 2. 协议分层（事实）

从 `IPCProtocol` 模块还原出的组件（功能事实）：

```
XPCListener / XPCConnection           连接与监听（XPC 通道）
XPCClientEngine / XPCServerEngine     客户端/服务端引擎（收发调度）
XPCMessageMapper                      消息 <-> 领域对象 映射
XPCMessageJSONEncoder                 消息体以 JSON 编码
XPCMessageValidator                   入站消息校验（防伪造/越权）
XPCRemoteProxyType                    远端代理抽象（调用对端如本地对象）
RequestProxy（XPCConnection 内）       请求-响应配对（异步回调）
```

## 3. 协议契约（行为描述）

| 项 | 约定 |
|---|---|
| 传输 | XPC（命名 Mach 服务；帮助器经 `launchd`/`SMAppService` 注册） |
| 编码 | 消息体 **JSON**（字段化，便于版本演进） |
| 模式 | 请求-响应 + 事件推送（进度/完成回调） |
| 安全 | 入站**消息校验**；对端代码签名/teamID 校验；最小权限（仅暴露必要操作） |
| 生命周期 | 惰性建立连接 → 发送操作 → 收进度事件 → 完成/错误 → 空闲断开 |

## 4. 典型特权操作（推导）

- 删除受保护路径（系统缓存/日志、他人文件、外卷 `.Trashes`）。
- 清空所有用户废纸篓、管理系统迁移残留、dyld 缓存重建触发。
- 卸载应用时移除其 LaunchDaemons/PrivilegedHelperTools。

## 5. 独立实现提示

- 帮助器用 `SMJobBless`（旧）或 `SMAppService.privilegedHelper`（新）安装。
- 校验对端：从 XPC audit token 校验 designated requirement；Team ID 只能作为要求的一部分，
  不能单独作为身份凭证。host 与 helper 必须双向限制预期签名。
- 消息校验白名单化：只接受枚举内的**领域操作**和严格参数，禁止通用
  `{op: delete, path: absolutePath}`。
- 我们的实现可用同样的 XPC+JSON 形态（macOS 标准做法，非其专有）。

## 6. 本项目启用前的强制安全契约

当前 Python wheel 不包含 app bundle、native helper、entitlements 或签名链，因此特权项
固定 `actionable=false`。不得用 sudo shell wrapper 代替 XPC 设计。未来实现至少满足：

1. **服务端重新授权**：helper 根据固定 root、相对组件和领域状态重新发现目标，不信任
   CLI 提交的路径、safety、`requires_privilege` 或扫描时判定。
2. **路径与身份**：逐组件 no-follow，核对 device/inode、owner、mount 和目标类型；操作
   前重新执行业务判定，例如 broken startup item 必须重解析 plist。
3. **权限分类**：区分 Full Disk Access、admin helper、SIP unsupported 和签名 bundle
   mutation unsupported；helper/root 不等于自动拥有或绕过其他能力。
4. **协议治理**：版本号、request ID、消息大小上限、超时、取消、幂等语义、稳定错误码和
   不含敏感路径的审计事件。
5. **生命周期**：安装、升级、密钥/签名变化、失效 helper、卸载和回滚都有真实 macOS
   验收；host/helper 版本不匹配时 fail-closed。
6. **范围限制**：只提供有限操作，例如“某个已验证系统缓存根下的一级子项”，不提供
   任意文件写入、任意命令执行或跨用户通用删除。

完成协议、威胁模型和 native 签名环境之前，本规格是未来设计门槛，不是已实现能力。

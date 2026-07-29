# 03 · 知识库规格

> 来源：`analysis/raw/KnowledgeBaseService.txt` + SystemJunk 的知识库协议事实。
> ⚠️ 净室红线：知识库的**数据**（MacPaw 多年积累的应用指纹/规则明细）是商业秘密。
> 本规格只描述**结构与行为思想**，供独立实现**自建数据**，不得提取/复用其数据。

## 1. 知识库的角色

扫描引擎的"硬编码扫描点"（见 02）只覆盖通用路径；
**应用级、动态的精细规则**由知识库提供，且知识库**可在线更新**（存在"知识库更新服务"）。
即：扫描点字典 ≈ 硬编码骨架 + 知识库数据填充。

## 2. 数据类别（事实：知识库覆盖的域）

知识库按域分桶，包含以下键（从接口事实提取）：

```
systemCaches  userCaches  systemLogs  userLogs   (系统/用户 缓存与日志规则)
xcodeCaches   iTunesCaches  photosCaches           (专项应用)
sandboxContainers  groupContainers                 (容器路径映射)
守护进程日志:  cmmXMASLogs  healthMonitorXLogs  menuXLogs
```

## 3. 能力接口（行为契约）

知识库对引擎暴露如下**查询能力**（从 demangled 方法签名还原的功能事实）：

| 能力 | 语义 |
|---|---|
| 路径是否被忽略 `isPathIgnored(path)` | 命中忽略规则 → 该项不可删 |
| 是否系统关键项 `isSystemItem(atPath:)` | 系统关键 → 保护，不删 |
| 应用是否受保护 `isAppProtected(bundleId:)` | 受保护应用 → 跳过 |
| 应用附加文件 `additionalFiles(forApplicationName:bundleId:)` | 给出某应用"该连带清理"的额外路径 |
| 是否需要深搜 `isDeepSearchNeeded(for:)` | 某应用是否需递归深挖 |
| 应用名解析 `applicationName(for:)` | 由 bundle 反解应用名 |
| 自定义忽略项 `addCustomIgnoreItem(...)` | 用户级忽略列表（增/删/快照） |
| 规则读/写 `readScanners / writeScanners` | 扫描器规则的持久化 |

## 4. 存储与格式

- 知识库以**自定义二进制容器**持久化（`.cmmkb`），内容为**加密或压缩的 blob**（非明文 plist/SQLite）。
- 运行时由知识库服务**解码/解析**后供查询；用户自定义部分（如忽略项）单独存放于
  `~/Library/Application Support/KnowledgeBase/userInfo.cmmkb`。
- 规则匹配采用**正则/模式**（存在 RegexCache 以加速）。

## 5. 独立实现的数据策略（净室合规）

我们不复用 MacPaw 数据，改为**自建等价规则**：

1. **忽略/保护规则**：用公开常识自建（如 `/System`、`/usr`、正在运行应用的容器默认保护）。
2. **应用附加文件**：按"应用支持目录/容器/缓存/日志/偏好"的通用命名约定推导，而非照抄其明细。
3. **可更新机制**：本项目规则规范为**明文 JSON**（无需加密），支持本地文件和显式
   签名 HTTPS 更新；不支持 YAML，避免多格式语义漂移。
4. **正则缓存**：规则多时同理加缓存加速（通用工程实践，非其专有）。

> 合规声明：本节描述的"能力接口"是*功能事实*（任何清理工具都需要这些判定）；
> 具体规则数据由本项目独立采集，与 MacPaw 无关。

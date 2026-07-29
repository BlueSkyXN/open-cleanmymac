# 05 · 核心算法规格（算法级还原）

> 净室规格：本文把 `analysis/` 二进制分析还原的算法**事实**提炼为"行为契约"，
> 供独立实现参考。只描述**做什么/怎么算的逻辑**，不复制 MacPaw 的代码表达。
> 分析侧细节见 `analysis/ALGORITHMS.md` 与 `analysis/decompiled/`。

---

## 1. 扫描引擎算法（ScanningCore）

### 1.1 进度聚合 = 加权平均
- 每个子任务是一个 **WeightedProgressable**（携带权重）。
- 引擎维护每项的实时进度 `Double(0..1)`。
- **整体进度 = Σ(子进度 × 权重) / Σ权重**；权重通常取该任务的预计项数（`itemsCount`）。
- 任意时刻可读取不可变**快照**（`CompoundProgressSnapshot`），供 UI 渲染。

### 1.2 依赖解析 = 责任链 + 惰性缓存
- 多个 `DependenciesResolverType` 组成**责任链**，依次为任务求依赖。
- 依赖结果存入惰性缓存（首次访问时求值并记忆），避免重复解析。
- 依赖未满足 → 任务不进入就绪，报"应用依赖未满足"。

### 1.3 控制 = 响应式三态流
- 每个运行任务暴露一个控制通道（异步事件流）。
- 控制动作枚举：**pause / resume / cancel**。
- 任务在异步执行中**消费控制事件**：pause→挂起等待，resume→继续，cancel→在安全点退出。
- 取消是**协作式**：任务自检取消标志，而非强杀。

### 1.4 编排流水线
- 一次扫描 = 构建扫描器 → 任务入队 → 依赖排序 → 并发执行。
- 监控器（watcher）+ 日志器（logger）旁路观察；失败任务收集到 `problematicTasks`，**单任务失败不中断整体**。

---

## 2. 扫描任务构建算法（SystemJunk，统一模式）

**所有扫描任务遵循同一构建范式**（自 927 个已还原方法归纳）：

```
ScanTask =
  ① identifier（类别标识，如 "UserCaches"）
+ ② knowledgeBase（忽略/保护规则来源）
+ ③ scanningPathsProvider（产出待扫路径集合）
+ ④ predicate = CompoundPredicate([
                  FileIgnorePredicate(knowledgeBase),   // KB 忽略过滤
                  <类别专属谓词>                          // 如时效/类型过滤
                ])
+ ⑤ FileManagerProvider（遍历）+ FileSizingService（统计大小）
+ ⑥ [可选] PrivilegedOperationsPerformer（需提权时）
```

**判定一项是否可清的通用流程**：
1. PathsProvider 产出候选路径。
2. `CompoundPredicate` 逐个过滤：先过 KB 忽略（`isPathIgnored`/`shouldIgnoreURL`），
   再过类别规则（如诊断报告按扩展名 `.ips/.crash`，缓存按"应用未在运行"）。
3. 幸存者经 FileSizing 计物理大小，标记安全等级后入结果。

---

## 3. 文件大小统计算法（FileManagerService）

### 3.1 遍历 = BSD fts(3)
- 用 `fts_open/fts_read` 遍历（高效、低内存）。
- 配置项：**跳过符号链接**（防外链重复计数）、递归、是否含根目录、是否后序回调。
- 每个节点包装为 `{isDirectory, isHidden, isSymLink, name, path, size}`。

### 3.2 大小 = 物理 + 逻辑
- **物理大小**：APFS 实际占用块（克隆/稀疏文件的真实磁盘量）。
- **逻辑大小**：文件表观长度。
- 统计全程支持**取消标志**（与引擎三态对接）。
- 快照/备份卷大小单独缓存并记录**测量时间**（时效失效重测）。

### 3.3 显示格式化
- 支持 **base2（1024）/ base10（1000）** 与本地化单位。

---

## 4. universal 二进制瘦身算法（CMLipo）

1. **探测兼容架构**：读 fat 二进制各切片的 `cputype/cpusubtype`，映射为架构枚举，
   选出"与本机兼容"的目标切片。
2. **找冗余切片**：遍历 fat 所有切片，**保留兼容切片，其余标记为 unnecessary**。
3. **剔除**：对标记切片执行 lipo 瘦身，回收空间。
4. 任一步失败 → 构造错误（如"无兼容架构"），不破坏原文件。

---

## 5. 专项判定算法要点

| 域 | 判定逻辑（行为契约） |
|---|---|
| **应用语言包** | 枚举应用 `.lproj`；按用户语言白名单保留；`hasNonStringsFilesAt` 排除含非 strings 资源的语言包。本项目只报告潜在项，固定不可执行。 |
| **诊断日志** | 收集 `DiagnosticReports` 下 `.ips/.crash/.panic/.diag/.hang`，过 KB 忽略后判可删。 |
| **缓存** | 缓存目录归属某应用时，**该应用正在运行则跳过**（防删在写缓存）。 |
| **失效启动项** | LaunchAgents/Daemons plist 指向的可执行路径**不存在** → 判为 broken。 |
| **失效偏好** | plist 对应应用已不存在 → 判为残留偏好。 |
| **Xcode** | DerivedData/DeviceSupport/Archives/ModuleCaches/Simulator runtimes 等，按 KB 忽略 + 是否当前用 SDK 判定。 |
| **AI 工具** | 按工具固定相对路径（cache/debug/telemetry/tmp）定位，过 AIJunkIgnoreRules 判可删。 |

---

## 6. 知识库加载算法（格式思想层）

- 持久化为自定义二进制容器（`.cmmkb`）。
- 结构 ≈ **序列化对象图（NSKeyedArchiver/PropertyList）+ deflate 压缩 + 外层编码**。
- 运行时解码→反序列化为内存对象图，供 `readScanners`/`isPathIgnored` 等查询。
- 用户自定义忽略项单独存放（`userInfo.cmmkb`），支持增删与快照。
- **净室红线**：具体规则数据不取；实现侧用明文 JSON 自建等价规则。

> universal binary/lipo 的步骤是参考算法事实，不代表当前写入需求。没有签名、原子替换、
> 恢复、Rosetta/plugin 兼容和真实应用验收前，本项目不实现二进制修改。

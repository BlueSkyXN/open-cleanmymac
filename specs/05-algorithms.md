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

### 3.4 本项目的保守遍历边界
- Python 实现使用显式栈和 no-follow `lstat` / `scandir`；目录在实际进入前重新验证，
  普通文件只做一次 `lstat`，避免跟随扫描期间被替换的目录 symlink。
- `lstat`、打开目录和读取下一目录项遇到 `EINTR` 时重试；权限、缺失和其它文件系统错误
  继续转成结构化 issue，不把部分结果伪装成完整可执行候选。
- `analyze` 为每个一级候选同时固定 `st_dev` 与 `statvfs().f_fsid`。任一身份变化时停止
  该分支，记录 `cross_device_skipped` / `cross_device_paths`，不计入容量并阻止候选执行；
  双重身份用于识别 macOS APFS root/Data 可能呈现相同 `st_dev` 的挂载边界。

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
| **缓存** | 缓存目录归属某应用时，应用正在运行则候选**可见但不可执行**；进程状态无法读取时同样 fail-closed。通用 `~/Library/Caches` 一级候选不能绕过已知应用归属规则。 |
| **失效启动项** | LaunchAgents/Daemons plist 指向的可执行路径**不存在** → 判为 broken。 |
| **失效偏好** | plist 对应应用已不存在 → 判为残留偏好。 |
| **Xcode** | DerivedData/DeviceSupport/Archives/ModuleCaches/Simulator runtimes 等，按 KB 忽略 + 是否当前用 SDK 判定。 |
| **AI 工具** | 按工具固定相对路径（cache/debug/telemetry/tmp）定位，过 AIJunkIgnoreRules 判可删；Codex 只保留精确 app/tool catalog cache 子项，临时结构走 §7.3 只读诊断；Chrome DevTools MCP 同时兼容根布局与 `Default/` profile。 |

---

## 6. updater 状态机（本项目扩展）

1. 从公开维护的 updater 根读取已暂存 app 的 `Info.plist`，或只解压 ZIP 内受限大小的顶层
   app `Info.plist`；不执行下载包或暂存 bundle。
2. 按 bundle ID 在 `/Applications`、`~/Applications` 和挂载卷的 `Applications` 中定位
   已安装 app。
3. 仅比较纯数字点分版本：
   - staged > installed：`pending_update`，不可执行；
   - staged == installed：`same_version_residue`，critical 精确选择；
   - staged < installed：`older_version_residue`，critical 精确选择；
   - app 缺失、metadata 损坏、多版本冲突或不可比较：fail-closed。
4. 执行前重新读取版本；状态、installed 或 staged 版本任一变化即取消整批。
5. 已安装 app 位于 `/Volumes` 时只报告外置安装提示，不假定 updater 一定能自动替换。
6. `DARWIN_USER_TEMP_DIR` 中的 Qoder ShipIt 动态根通过 `getconf` 发现并复用同一版本模型，
   但完整 app 临时副本固定不可执行。

---

## 7. 结构化空间诊断（本项目扩展）

### 7.1 日志/trace 保留期

1. 对明确的日志根做 no-follow、同 device 遍历，硬链接按 `(device, inode)` 去重；
2. 只累计普通文件的 `st_blocks * 512`、逻辑大小、mtime 和数量，不读取正文；
3. 分别计算早于 7/14/30 天的物理容量，并独立报告进程与 `lsof` 打开句柄；
4. 保护规则、跨卷、云占位和访问错误仍会跳过或形成 issue；
5. 结果固定 `critical + actionable=false`，不把 retention 阈值变成通用删除事实。
6. 固定根只采用公开路径约定；Darwin 动态根由 `getconf` 发现，并只允许维护的直接子项
   glob。真实机器的用户名、容量、mtime、UUID、会话名和私有项目名不得固化成规则。
7. 日志之外的 runtime、历史安装包、toolhost snapshot、构建 temp 和 code-sign clone
   仍只读取 metadata；retention 诊断不等价于安全删除结论。

### 7.2 SQLite freelist

1. 仅针对公开维护的精确数据库路径，拒绝 symlink/非普通文件；
2. 使用 `file:...?mode=ro&immutable=1` 读取 `page_size`、`page_count`、`freelist_count`；
3. 查询前后复核 device/inode/size/mtime，变化则丢弃结果；
4. `potential_bytes` 最多为内部空闲页和当前物理分配量的较小值，但
   `reclaimable_bytes=0`；同时报告数据库总大小、空闲比例、WAL/SHM/journal 和句柄；
5. 不创建/修改 sidecar，不 checkpoint，不运行 `VACUUM`，不删除数据库。

### 7.3 Codex 临时结构与 Crashpad 配对

1. 不把 `~/.codex/tmp` 或 `~/.codex/.tmp` 整根判为垃圾；installed marketplace、bundled
   marketplace、plugin source、session、配置和恢复状态不进入该扫描器；
2. marketplace 只匹配精确 `.staging` 根的直接子项 `marketplace-upgrade-*`，按物理块、
   mtime、进程和句柄汇总，不据名称或年龄断言升级已经失败；
3. Git 空壳只在 Codex 精确临时根中匹配 `git-*`，并要求结构仅含指向
   `refs/heads/main` 的小型 `HEAD`、空 `objects`、空 `refs` 和可选 `.DS_Store`；存在
   config、object、ref、worktree 或其它成员即不匹配；
4. Crashpad 只通过同一目录内的公开文件名关系配对 `<id>.dmp` 与
   `<id>_sidecar.json`；只汇总没有同名 dump 的普通 sidecar，同时独立报告 paired、
   最近更新和打开句柄数量，不读取崩溃正文；
5. 所有结果固定 `critical + actionable=false`。未来执行器必须保存扫描时 identity 清单，
   并在逐项操作前重新确认进程、句柄和配对关系；不能复用通用目录删除器。
6. 三类结果使用 `resource_kind=filesystem_subset`：path 只是聚合锚点，物理容量只覆盖
   精确命中子集；Crashpad 配对/近期计数分别使用 `paired_artifact_count` 和
   `recent_artifact_count`，不复用 Docker 语义的 `active_count`。

---

## 8. 知识库加载算法（格式思想层）

- 持久化为自定义二进制容器（`.cmmkb`）。
- 结构 ≈ **序列化对象图（NSKeyedArchiver/PropertyList）+ deflate 压缩 + 外层编码**。
- 运行时解码→反序列化为内存对象图，供 `readScanners`/`isPathIgnored` 等查询。
- 用户自定义忽略项单独存放（`userInfo.cmmkb`），支持增删与快照。
- **净室红线**：具体规则数据不取；实现侧用明文 JSON 自建等价规则。

> universal binary/lipo 的步骤是参考算法事实，不代表当前写入需求。没有签名、原子替换、
> 恢复、Rosetta/plugin 兼容和真实应用验收前，本项目不实现二进制修改。

# 00 · 总体架构规格

> 净室规格：描述 CleanMyMac 5 CLI 的架构"做什么"，供独立实现参考。
> 来源：参考对象的公开命令面、符号与字符串事实。不含原代码表达。

## 1. 进程模型

CLI 是一个**单一可执行入口**（`cleanmymac` / `cmm`），内部以 **模块化动态库** 组织职责，
并可通过 **XPC** 与已安装的 CleanMyMac 主程序/特权帮助器协作完成需要提升权限的操作。

```
┌──────────────────────────────────────────────────────────┐
│  CLI 主壳（命令解析 + 编排 + 输出渲染 + 进度）             │
│   - 基于 ArgumentParser 的子命令体系（scan / clean / …）   │
├──────────────────────────────────────────────────────────┤
│  扫描引擎层（任务图 + 进度 + 控制）                          │
├──────────────┬───────────────┬───────────────┬───────────┤
│ 系统垃圾域    │ 开发垃圾域      │ 项目产物域      │ 空间透镜域  │
│ (ObjC 任务集) │ (Swift 扫描器)  │ (Swift 扫描器)  │ (Swift)    │
├──────────────┴───────────────┴───────────────┴───────────┤
│  服务层：文件遍历/大小 · 应用枚举 · 知识库 · 权限 · 网络      │
├──────────────────────────────────────────────────────────┤
│  IPC 层：XPC 客户端/服务端（JSON 消息）→ 主程序/特权操作     │
└──────────────────────────────────────────────────────────┘
```

## 2. 命令面（CLI 接口契约，已从主壳元数据还原）

CLI 采用"子命令 + 选项"结构（ArgumentParser）。主壳（CMMCLI）还原出的命令树：

```
CleanMyMacCLI (根命令)
├─ AnalyzeCommand   磁盘/空间分析(SpaceLens)
├─ CleanCommand     清理
│   ├─ Trash        废纸篓
│   ├─ AIJunk       AI 工具缓存
│   └─ ...(SystemJunk / DeveloperJunk / ProjectArtifacts)
└─ CatCommand       元数据可确认存在；公开语义不足，不能推断为导出契约
```

> 参考元数据只能确认 `CatCommand` 名称，无法从公开文档可靠确定其行为。
> 独立实现若提供同名命令，不得把它计入兼容性声明。本项目差异见 [_index.md](_index.md)。

**编排核心 `AggregateCleanFlowScanner`**：聚合四大域扫描器
`{ systemJunkScanner, developerJunkScanner, trashScanner, aiJunkScanner }`，
一次 clean 流程 = 并行驱动多域扫描器 → 汇总。

**扫描器接入模式**：每域一个 `*Scanner`（如 `SystemJunkScanner` 带 `TaskDescriptor`），
通过 **Adapter**（`AnalyzeScannerAdapter`/`ProjectArtifactsScannerAdapter`）把域扫描器
适配进统一引擎接口。

**扫描执行**：Swift 域的任务是 `*ScanTask.execute() -> Findings { totalSize, items }`
（如 `AIJunkScanTask.execute()`）；工厂 `*ScanFactory.makeTasks(...)` 按目标生成任务集
（如 `AIJunkScanFactory.makeTasks(tools:)` 按检测到的 AI 工具生成）。

**附加能力**：
- 优化命令：`PurgeableOptimizeTaskRunner`（清可 purge 空间）、`RamOptimizeTaskRunner`（释放内存）。
- SpaceLens：`DeletionPlanner.units(for:shouldSpare:)`（删除规划器，逐项决策删/留），
  `ScanTasksFactory` + `CloudScannerStrategy`（云文件策略）+ FullDiskAccessPermissionProvider。

## 3. 模块职责（独立实现时的等价划分）

| 模块 | 职责 | 实现要点 |
|---|---|---|
| 命令层 | 解析子命令/选项、调度对应扫描器、渲染输出 | 任意 CLI 框架 |
| 扫描引擎 | 任务依赖图、并发、进度聚合、暂停/恢复/取消 | 见 `01-scan-engine.md` |
| 各域扫描器 | 按扫描点字典产出"可清理项"列表 | 见 `02-scan-points.md` |
| 知识库 | 忽略规则、受保护项、应用→附加文件映射 | 见 `03-knowledge-base.md` |
| 文件服务 | 遍历、大小统计（含硬链接去重/符号链接处理） | 抽象接口 |
| 应用服务 | 枚举已装应用、bundleID、容器、插件、LaunchAgents | 抽象接口 |
| 权限服务 | 全盘访问/辅助功能授权检测与引导 | 抽象接口 |
| IPC | 与特权帮助器/主程序的安全消息通道 | 见 `04-ipc-protocol.md` |

## 4. 关键设计事实

- **域与扫描器解耦**：每个域实现统一的"扫描器协议"（输入配置 → 输出可清理项流 + 进度）。
- **知识库驱动**：扫描点并非全部硬编码——应用级的"该清哪些文件"由**可更新的知识库**提供，引擎运行时查询。
- **安全默认**：存在"受保护/忽略"机制；删除前判定项是否系统关键、是否被用户加入忽略列表。
- **权限分层**：普通项用户态即可；系统区/受保护项需特权帮助器（XPC）。

# 02 · 识别对象、范围与自有特色

> 文档 ID：OC-02 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-GOAL、SRC-CODE、SRC-EXPERIENCE。不是参考产品的“扫描点全集”。

## 1. 规则如何进入产品

REQ-DETECT-001：新增识别对象必须说明定位根、结构/类型条件、排除条件、计量方式、
运行状态要求和动作边界。参考分类或个人怀疑是线索，不能单独成为删除依据。
VAL-DETECT-001：每个新增对象至少有对应正例、近似但不应命中的负例以及保护场景。

精确路径、模式、风险级、默认选择在 [scanpoints.py](../implementation/openclean/scanpoints.py)
与各专项 scanner 中维护，避免手抄另一份会漂移的删除字典。
本文保存用户功能和不能泛化的边界，不删除已有实现，也不因为旧规格列过路径就新增扫描点。

## 2. 经典五域

| 域与入口 | 当前识别主题与实现锚点 | 不能据此推出 |
|---|---|---|
| system / `clean junk` | 用户/系统缓存日志、Darwin 动态根、诊断报告、updater、启动项、语言资源；`SYSTEM_JUNK` 及动态发现 | 所有系统路径可删；root/FDA 可以替代 helper、SIP 或签名约束 |
| developer / `clean dev` | 包管理器/开发工具缓存、Docker 容量；`DEVELOPER_JUNK`、`docker.py` | 缓存名称证明无用户数据；Docker 所有资源都可 prune |
| project / `purge` | 项目标记发现与明确产物名，如 node_modules、.venv、构建缓存；`PROJECT_ARTIFACT_NAMES` 等 | 项目标记、源码、.git 或项目内任意 .Trash 都是产物 |
| ai / `clean ai` | Claude/Codex/Gemini/OpenCode 等既有工具路径、Codex 与 WorkBuddy 专项；`AI_TOOL_JUNK` | 整个工具 HOME、session、runtime 都是垃圾；只能服务 AI 软件 |
| trash / `clean trash` | 当前用户 Trash 和支持的挂载卷 Trash 根；`macos.py` | 清空所有用户或未选中的 Trash；可恢复的普通移动等于永久清空 |

REQ-DETECT-002：默认选择、风险门、精确选择与只读标记是不同维度。
AI 域默认不选；不可执行对象不因 `--all`、tier flag 或 `--yes` 获得动作能力。
VAL-DETECT-002：新增扫描点必须通过精确选择和默认选择回归，不附带选中同等级其它对象。

`analyze` 是独立占用分析入口，不是第六套垃圾规则；大目录仍需明确选择和 critical 确认。

## 3. 承接个人经验与已有诊断

REQ-DETECT-003：复用下表能力和 [EXPERIENCE](../docs/EXPERIENCE.md) 的来源映射；
缺少某个 pack 不等于该能力不存在。只读识别有产品价值，但不是动作完成。

| 主题 | 应当解释的证据 | 识别/处理边界 |
|---|---|---|
| 多工具 retention | 文件数、已分配/逻辑容量、7/14/30 天桶、句柄与测量限制 | 桶可重叠；年龄不直接授予整根删除 |
| Codex staging | 精确 marketplace 子目标和聚合摘要、目标数/测量数、完整性 | .tmp/.staging 父目录不可泛化；结构动作门仍未实现 |
| Codex Git 空壳 | 临时根下已验证空骨架结构 | 普通仓库、有内容/工作状态的近似结构排除 |
| Codex SQLite | page/freelist、内部空闲、WAL/SHM/journal | 不读业务行、不在线 VACUUM、不删除 DB/sidecar |
| Crashpad | sidecar 与同名 dump 配对、近期项、数量 | 不删除配对 .dmp；聚合证据不等于动作目标 |
| Qoder/updater | staged/installed 版本与应用存在性、Darwin 临时副本 | 当前/待升级/未知版本不能作旧版本残留；临时副本诊断保持只读 |
| 浏览器 CacheStorage | 已支持浏览器的 Default/Profile 范围内占用与年龄 | 不读取 origin 数据或删除 Cookies、Login Data、IndexedDB、整个 profile |
| deleted-open | device/inode 去重、进程/句柄、逻辑大小上限 | 已解除链接，无可直接删除路径；potential/reclaimable 为 0 |
| Darwin 临时副本 | 动态根、已知结构、年龄及归属 | 不把整个临时根视为可删 |
| WorkBuddy | expired 后缀、numeric Worker 整组年龄、精确 Electron 缓存 | 以下专项要求，全部只读 |

VAL-DETECT-003：专项 JSON/文本/详情解释使用本次 Item 证据，不额外读取正文，不把诊断字节计入可回收。

## 4. WorkBuddy 的具体经验约束

REQ-DETECT-004：expired 日志只匹配有效日期的
`logs/YYYY-MM-DD.expired-<13位毫秒时间>-<8位小写hex>/`，不是 `.expired-*` 前缀。
正常日期日志、sandbox/update、symlink 和近似名称不因相似而命中该结构。

REQ-DETECT-005：numeric Worker 按整组文件及目录最新 mtime 判断年龄；
跳过保护/云占位/跨卷对象或发生错误时，整组年龄为未知，不拿剩余旧文件推断完整组。

REQ-DETECT-006：Electron 只识别 `app/session` 和 `Partitions/*` 下的
`Cache`、`Code Cache`、`GPUCache`、`DawnGraphiteCache`、`DawnWebGPUCache`。
保留 Cookies、IndexedDB、Local Storage、binaries、plugins、skills、项目、会话和历史。
已有两个 BundleMigration ID 的归属保护继续有效，不泛化名称相似目录。

VAL-DETECT-004/005/006：[test_workbuddy.py](../implementation/tests/test_workbuddy.py)
覆盖正反名称、整组年龄、跳过与预算、session/Partitions 范围、经典/inspect 共用和拒绝执行。
新增清理动作需要新的对象与前后证据，不能把此只读规格改成执行授权。

## 5. 实现与测试

- 经典发现：[test_scan_point_expansion.py](../implementation/tests/test_scan_point_expansion.py)、[test_system_junk_discovery.py](../implementation/tests/test_system_junk_discovery.py)、[test_project_purge.py](../implementation/tests/test_project_purge.py)。
- 专项诊断：[storage_diagnostics.py](../implementation/openclean/storage_diagnostics.py)、[workbuddy.py](../implementation/openclean/workbuddy.py)、[test_storage_diagnostics.py](../implementation/tests/test_storage_diagnostics.py)。
- updater/进程：[test_updater.py](../implementation/tests/test_updater.py)、[test_process_protection.py](../implementation/tests/test_process_protection.py)。
- 动作边界：[06](06-system-flow.md)、[07](07-predicate-engine.md)。

不在本文设“每个软件都建包”的目标，也不保留未经证实的厂商私有路径字典。
具体覆盖与未实现项仍由能力地图和 TODO 维护。

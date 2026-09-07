# 自有经验与实现依据

本页保存可公开的经验摘要和代码映射，不包含真实机器容量、用户路径、日志正文或凭据。
历史观察不是当前机器状态，也不自动构成清理授权。Agent 用当前 JSON 结果作判断。

## 已有成果从哪里来

| 经验主题 | 可追溯历史与当前实现 | 处理边界 |
|---|---|---|
| Docker 目标绑定 | `d526744`；`docker.py`、`cleanup.py` | 扫描和执行绑定 CLI/context/daemon；固定 prune，Volumes 不开放 |
| 多工具日志、runtime、浏览器缓存保留期 | `43ba38a`、`d040d48`、`a1b4b58`；`storage_diagnostics.py` | 年龄桶是占用诊断，不是整根删除规则 |
| Codex SQLite、临时结构和 Crashpad | 同上及后续保护修正；`storage_diagnostics.py` | 不删除数据库、整个 .tmp、会话或配对 dump |
| Qoder 更新与临时 runtime | 同上；Darwin 动态根和 updater 版本判断 | 当前/待升级/未知版本不能当成旧缓存 |
| WorkBuddy 精确结构 | `obs:personal:workbuddy:20260901`；`workbuddy.py` | 本轮新增确定性只读识别，经典 AI 域和 `inspect workbuddy` 共用 |

这些提交证明功能已经进入项目历史，不代表每条规则的最初来源都已独立查明；
无法从历史证据确认的条目，不标为“CleanMyMac 官方规则”或伪造 Observation。

## WorkBuddy 个人经验摘要

观察 ID：`obs:personal:workbuddy:20260901`。来源为项目工作区保存的个人复核记录，
记录版本为 WorkBuddy 5.4.5；本轮仅回查了当前顶层目录名称形态，没有读取日志正文。
以下是可复用结构事实，不引用当时的容量数字或“无条件安全”结论。

- expired 日志形态为 `logs/YYYY-MM-DD.expired-<13位毫秒时间>-<8位小写hex>/`，不是 `.expired-*` 前缀。
  检测还要求有效日期及非符号链接目录。正常日期日志、sandbox/update 不因容量大而标记过期。
- traces 的 numeric Worker 是完整工作单元。整组年龄使用目录及文件最新 mtime，
  不能因为其中存在旧文件就拆删活跃 Worker；文件年龄桶与整组年龄是不同指标。
  存在保护路径、云占位、跨卷或测量错误时，整组年龄标为未知，不用剩余文件推断完整 Worker。
- Electron 只识别 `app/session` 及 `app/session/Partitions/*` 下的精确
  `Cache`、`Code Cache`、`GPUCache`、`DawnGraphiteCache`、`DawnWebGPUCache`。
  不遍历 Cookies、IndexedDB、Local Storage 或把整个 session 当缓存。更深未知布局不猜测匹配。
- binaries、plugins、connectors-marketplace、skills、projects 和 file-history 不纳入这条诊断规则。
  只有一个版本的运行时可能就是当前唯一副本；大小本身不能证明可删除。
- 禁用 session-history 清理的环境变量不能证明它控制日志回收，不能据此改配置。
- `Library/Caches/com.workbuddy.workbuddy.BundleMigration` 与
  `Library/Caches/com.tencent.workbuddy.mac.BundleMigration` 都归属 WorkBuddy；本轮补齐后一别名的运行应用保护，
  不泛化到名称相似的 sibling，也不把添加归属保护当成开启清理。
- 所有发现保持 `actionable=false`；发现和测量有条目上限，超限会报告不完整，不开放写入。

## Agent 的使用方式

全机功能仍按经典五域命令发现；专项可调用 `inspect workbuddy --json`，随后使用真实
Run/Finding ID 调用 `show` 和清理预览。该包当前只有只读策略，`--yes` 仍被阻断。
已有经典可执行候选则按用户授权范围走精确选择、预览、执行和结果核对，不必等全部能力迁入 Pack。
完整调用约定见 [AI_USAGE.md](AI_USAGE.md)。

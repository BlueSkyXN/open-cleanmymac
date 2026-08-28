# open-cleanmymac · AI 开发交接入口

> 这份文档是给**接手开发的 AI**的总入口。读完这份 + 它指向的规格，就能直接开工。
> 目标：用 Python 独立实现一个 macOS 清理工具 CLI（`openclean`），保守对齐
> CleanMyMac 5 CLI 的公开命令面；缺少安全公开接口或签名环境的能力保持 fail-closed。

---

## 0. TL;DR —— 30 秒上手

```bash
# 1. 当前实现已能跑（默认只读；写操作必须显式 --yes）
cd implementation
PYTHONPATH=. python3 -m openclean.cli scan                # 扫全部域
PYTHONPATH=. python3 -m openclean.cli scan --json         # 机器可读输出

# 2. 开发从这份任务清单开始认领
cat implementation/TODO.md

# 3. 设计依据全在 specs/（按 _index.md 的顺序读）
ls specs/
```

**当前状态**（用户门面以 [README.md](README.md) 为准，缺口见 `implementation/TODO.md`）：

- **已完成**：五域扫描、谓词 / JSON 知识库、用户态 `clean` / `purge` / `analyze`、
  TUI / JSON、同卷 Trash、Docker 白名单 prune、云占位保护、隔离预览、标准 Python 包装
- **明确拒绝**：`optimize ram|purgeable`（没有已验证的安全公开执行器）
- **未做 / 外部前提**：特权帮助器、正式知识库服务端、真实 Docker daemon 验收
- **写入边界**：扫描和预览默认只读；`--yes`、`ignore add/remove`、
  `config --analytics` 和知识库更新才会写入

---

## 1. 这个仓库是什么

一个**净室（clean-room）实现**项目：

- **参考对象**：CleanMyMac 5 CLI（macOS 清理工具，Swift/ObjC 编写）
- **做法**：对它的二进制做结构分析 → 提炼成**设计规格**（`specs/`）→ **独立用 Python 实现**（`implementation/`）
- **法律红线**：`implementation/` 的代码与 CleanMyMac **零代码血缘**，只依据规格中描述的事实和算法思想。

**对你（接手的 AI）最重要的含义**：
> 实现时只依据 `specs/` 和 `implementation/`。用户可见行为以 `README.md` 为准。
> 不要去看 `analysis/`（受版权保护的原始分析产物，已隔离）。

---

## 2. 仓库地图（哪些和你有关）

| 路径 | 和你的关系 | 说明 |
|---|---|---|
| **`AGENTS.md`** | ⭐ 你在这里 | AI 开工入口（本文件） |
| **`specs/`** | ⭐ **必读** | 8 份设计规格，按 `_index.md` 的顺序读 |
| **`implementation/`** | ⭐ **你的战场** | Python 实现代码 + `TODO.md` 任务清单 |
| `README.md` | 用户门面 | 公开行为变化必须同步；许可证为 GPL-3.0 |
| `docs/` | 用户/开发者文档 | 预览、能力地图、架构 |
| `CONTRIBUTING.md` / `SECURITY.md` | 协作与安全 | 净室边界、检查门、漏洞报告 |
| `analysis/` | ❌ **不要看** | 原始分析产物，受版权保护，`.gitignore` 已隔离 |
| `local/` | ❌ 不用管 | 过程笔记 + 当时的分析脚手架（`local/tools/`），`.gitignore` 已隔离 |

> 如果你发现自己在读 `analysis/` 或想从中抄代码——**停下来**，那违反了净室红线。所有你需要的信息都已被提炼进 `specs/`。

---

## 3. 开发前必读的规格（按顺序）

在 `specs/` 下，**建议阅读顺序**：

1. **`_index.md`** —— 规格索引 + 每份的"实现状态"（先读这个）
2. **`00-architecture.md`** —— 总体架构、命令树、模块职责划分
3. **`02-scan-points.md`** —— ⭐ 核心：所有"该扫哪里"的字典（路径、模式、安全等级）
4. **`01-scan-engine.md`** —— 任务图、加权进度、暂停/恢复/取消
5. **`07-predicate-engine.md`** —— 判定一个文件"要不要忽略"的谓词系统
6. **`05-algorithms.md`** —— 关键算法（目录大小统计、硬链接去重、fat 二进制瘦身等）
7. **`03-knowledge-base.md`** —— 忽略/保护规则的存储格式（本项目使用明文 JSON）
8. **`06-system-flow.md`** —— 数据流/控制流图（把上面串起来）
9. **`04-ipc-protocol.md`** —— XPC 特权操作（仅当需要 sudo 类操作时读）

---

## 4. 当前实现状态

**代码位置**：`implementation/openclean/`

| 模块 | 文件 | 状态 |
|---|---|---|
| 扫描点数据表 | `scanpoints.py` | ✅ 完成（system/developer/ai/trash/project 五域） |
| 引擎 | `engine.py` | ✅ 基础完成（目录大小、硬链接去重、并发、三态控制） |
| CLI | `cli.py` | ✅ scan/clean/purge/analyze/ignore/config/optimize guard/cat |
| 谓词判定层 | `predicates.py` | ✅ 基础完成（组合谓词 + KB 最外层安全闸） |
| 知识库 | `knowledge_base.py` | ✅ 本地 JSON 规则 + ignore 管理完成 |
| `clean` / `purge` 执行 | `cleanup.py` / `tui.py` | 🟡 全屏复选、同卷 Trash、安全复核、Docker 白名单和报告完成；特权待做 |
| 云文件感知 | `models.py` / `engine.py` | ✅ dataless/疑似占位不遍历、不计可回收量且不可执行；不承诺识别全部 materialized 云同步文件 |
| Docker 资源扫描/执行 | `docker.py` | ✅ Build Cache/Images/Containers 白名单 prune；Local Volumes 硬拒绝 |
| 特权操作 | — | ❌ **未做**（XPC，specs/04，优先级低） |

**验证方式**：以 `make check` 的 lint、完整单测和 19 场景隔离预览为准；不要把历史
真实机器扫描结果写入仓库。

---

## 5. 下一步做什么（按优先级认领）

详见 **`implementation/TODO.md`**，这里是 Top 5：

1. **`optimize ram|purgeable`**：先确认可靠公开 macOS 接口，不能把需特权的
   `/usr/sbin/purge` 或制造内存压力伪装成等价实现
2. **特权帮助器**：用户态能力稳定后再单独建设 SMAppService/XPC 签名链
3. **知识库发布源**：在拥有自建规则服务和公钥后配置正式 HTTPS channel；禁止引入
   原厂私有规则数据
4. **真实联调**：在安装 Docker 的机器上复核 daemon 报告和三个 prune 白名单流程
5. **版本跟踪**：持续核对 CleanMyMac CLI 后续公开 release 和命令文档，避免把桌面版
   能力误列为 CLI 缺口

---

## 6. 硬性约束（开发时必须遵守）

1. **只读安全优先**：任何 `clean` 类写操作，默认**必须**有 `--yes` 才执行，且 `critical` 级需双重确认
2. **不碰 `analysis/`**：不要读、不要引用、不要从中复制任何东西到 `implementation/`
3. **规格为准**：`specs/` 是唯一事实来源；若发现实现与规格冲突，改实现或更新规格（并注明）
4. **纯标准库**：目前实现零依赖，尽量保持（如需引入依赖，在 TODO 里说明理由）
5. **跨平台只需 macOS**：所有路径/ API 只需考虑 macOS（`~/Library` 等）

---

## 7. 快速验证你的改动

```bash
make check
make package
make release-check
```

---

*交接包整理：2026-07-30 · 基于 CleanMyMac 5 CLI v1.0.0 的净室规格 · 仓库许可证 [GPL-3.0](LICENSE)*

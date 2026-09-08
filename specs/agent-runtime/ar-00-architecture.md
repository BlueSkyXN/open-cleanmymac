# AR-00 · Agent 附加层与经典内核的边界

> 文档 ID：AR-00 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-GOAL、SRC-CODE、SRC-CONTRACT；见 [总索引](../_index.md)。

## 1. 为什么保留附加层

跨命令 inspect→show→clean 需要保存一次观察、稳定引用结果并复核其来源。
Run Store/Strategy/Finding 服务这个功能；它们不是开发所有清理能力的前置条件。
经典 path/identifier 选择同样来自确定性扫描，并非任意路径删除器。

REQ-AR-BOUND-001：经典命令继续独立使用 Item/ScanResult，无 Run Store 也可发现、预览和执行支持动作。
VAL-AR-BOUND-001：经典 CLI 与选择回归不依赖 Agent 存储；两套接口均不能绕过 cleanup 保护。

## 2. 现有协作关系

```text
经典 scan/clean/purge/analyze → Item/ScanResult → 选择/预检 → cleanup
                                      ↑                  ↑
inspect → 已加载 Strategy → 共用 detector → Finding/Run   │
                                      ↓                  │
                                  Run Store → show/plan → 实时重探测 Item
```

| 已有模块 | 职责 | 不负责 |
|---|---|---|
| `strategies/registry.py` | 包读取、严格校验、来源/审批状态与 hash | 运行参考 CLI 或自动合并个人猜测 |
| `runtime/inspect_service.py` | 调用已接线 detector、投影、汇总和保存 | 探索任意未知布局后直接执行 |
| `runtime/finding_projection.py` | Item/Finding 字段转换 | 强制退役 Item |
| `runtime/run_store.py` | 私有 bundle、身份/格式/期限校验 | 将缓存当长期授权凭证 |
| `actions/planner.py` | 计划、再解析、实时检测并复用 cleanup | 通用删除器或新特权层 |
| `cli.py` | 参数、JSON、退出码 | 模型自行推断文件事实 |

REQ-AR-BOUND-002：复杂识别逻辑留在现有 Python detector，包 JSON 只声明内置名字、参数和说明。
VAL-AR-BOUND-002：未知名字/非法参数不能执行；未接线名字返回 scanner_unavailable，而非完整空结果。

## 3. 人与 Agent 的职责

用户决定目标、范围、是否承担某项动作风险；Agent 选择可用命令、取得证据、解释并执行已授权动作。
既有授权不必每次调用重复确认；新增风险/范围不自动继承。
研究阶段可辅助提线索、核查结构和编写测试，不能把模型推测或 pack 声明当作生产动作审批。

REQ-AR-BOUND-003：调用方不能将只读证据改成 shell 删除、开通 trusted 或扩大父目录。
VAL-AR-BOUND-003：当前生产包请求写入仍阻断；经典精确执行仅影响授权对象，见 [AR-06](ar-06-codex-p0-acceptance.md)。

## 4. 演进限制

不因上述分层搬迁源码目录、拆分所有 detectors/probes 或建立并行规则引擎。
不要求所有个人经验写成 Observation 文件后才允许维护既有 scanner。
融合指开发时审阅多种来源、形成一条独立规则，不是运行时调用两套清理器取并集。

架构与真实安全边界见 [ARCHITECTURE](../../docs/ARCHITECTURE.md)；
对象/执行细节见 [AR-01](ar-01-object-model.md)、[AR-04](ar-04-run-store-and-execution.md)。
验收锚点：[test_agent_projection.py](../../implementation/tests/test_agent_projection.py)、
[test_agent_review_scan.py](../../implementation/tests/test_agent_review_scan.py)、
[test_agent_contract.py](../../implementation/tests/test_agent_contract.py)、
[test_cleanup_cli.py](../../implementation/tests/test_cleanup_cli.py)。

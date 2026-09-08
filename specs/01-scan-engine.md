# 01 · 扫描、完整性与进度

> 文档 ID：OC-01 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT；产品入口见 [00](00-architecture.md)。

## 1. 输入与输出

一次扫描按调用方选择的域、项目根、规则和现有预算装配任务。
`scan` 默认五域；`clean` 默认系统/开发/AI/Trash，项目发现走 `purge` 或 project 域。
Agent `inspect all` 仅是已加载 pack 的集合，不能替代五域扫描。

扫描输出是 Item/ScanIssue/ScanResult 或其 Finding 投影，不是“全部可删清单”。
权限拒绝、跨界跳过、运行状态未知与预算截断必须有对应证据；不得只留下成功项后声称完整。

## 2. 行为需求

| 需求 | 契约 | 验收 |
|---|---|---|
| REQ-SCAN-001 范围 | 不因菜单或 Agent 调用改变默认域；应用任务按现有静态/动态 scanner 装配 | VAL-SCAN-001：显式域和默认域与 CLI 契约一致，结果不串域 |
| REQ-SCAN-002 调度 | DAG 拒绝重复 ID、未知依赖、自依赖和环；依赖失败阻断下游而非伪造成功 | VAL-SCAN-002：非法图失败，独立任务仍能完成，输出按既有顺序汇总 |
| REQ-SCAN-003 遍历 | 保护闸先于细节读取；不跟随候选/祖先 symlink；云占位和文件系统边界按 05/07 处理 | VAL-SCAN-003：保护对象不被递归测量；跳过和错误不转成可执行结果 |
| REQ-SCAN-004 完整性 | 取消或 blocking issue 导致不完整；保留可解释的部分结果和 issues | VAL-SCAN-004：部分发现不被写成完整空扫描，CLI 返回对应非零状态 |
| REQ-SCAN-005 进度 | 固定权重百分比与不可变快照保持单调；成功、失败、取消终态分开 | VAL-SCAN-005：失败/取消不显示为成功完成，不将 entry 启发值包装成精确总量 |
| REQ-SCAN-006 控制 | 使用现有共享 pause/resume/cancel 协作控制，在检查点响应，不强杀文件操作 | VAL-SCAN-006：暂停不继续推进，恢复可继续，取消可收尾且不触发清理 |

## 3. 当前进度模型与未批准增强

`task_graph.py` 和 `progress.py` 是现有内部执行器，不是用户必须理解的产品概念。
`processed_items` 是进度输入，不是所有 scanner 已知总量的证明。
Countable total、每任务控制聚合、observer 等不因为参考对象曾有同名概念就成为强制缺口。

只有具体 UI/自动化任务需要更精确反馈时，才提出：
哪些任务有可信 total、未知总量如何表示、取消如何传播、性能收益和兼容性如何验证。
本篇不要求新增进度 API、并发框架或 detector/entry 重构。

## 4. 验证与实现锚点

- [engine.py](../implementation/openclean/engine.py)、[task_graph.py](../implementation/openclean/task_graph.py)、[progress.py](../implementation/openclean/progress.py)。
- VAL-SCAN-001/003/004：[test_cli_contract.py](../implementation/tests/test_cli_contract.py)、[test_scan_rules_integration.py](../implementation/tests/test_scan_rules_integration.py)、[test_system_junk_discovery.py](../implementation/tests/test_system_junk_discovery.py)。
- VAL-SCAN-002：[test_task_graph.py](../implementation/tests/test_task_graph.py)。
- VAL-SCAN-005/006：[test_progress.py](../implementation/tests/test_progress.py)、[test_task_graph.py](../implementation/tests/test_task_graph.py)。

这些是验收入口，不是已通过声明。只修改本文时检查文档即可；引擎改动才运行相应测试。

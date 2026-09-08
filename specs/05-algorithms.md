# 05 · 容量计量与诊断解释

> 文档 ID：OC-05 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT、SRC-EXPERIENCE；不规定参考软件的内部算法或私有格式。

## 1. 指标定义

| 指标 | 含义 | 不表示 |
|---|---|---|
| logical bytes | 文件表观长度 | 该文件独占物理空间或删除收益 |
| allocated bytes | 以 `st_blocks * 512` 为基础的已分配块计量 | APFS 克隆/快照共享块的独占量 |
| potential bytes | 按具体结果口径发现的占用 | 全部可删或已释放 |
| reclaimable bytes | 清理域中当前 actionable 候选计量；Analyze/只读诊断为 0 | 保证执行成功后的精确磁盘增量 |
| requires_privilege / unsupported bytes | 当前不能由支持动作处理的占用分类 | 已完成特权执行 |
| moved_to_trash bytes | 已移入同卷 Trash 的操作回执 | 已释放磁盘空间 |
| permanently_deleted bytes | 永久删除动作回执中的计量 | APFS/快照/打开句柄下的实时可用空间净增量 |

若用户要求实际释放量，应另读取动作前后卷容量并说明并发写入、快照、句柄等干扰，
不能用 moved/potential 代替。没有做前后测量就明确未测。

## 2. 计量需求

| 需求 | 契约 | 验收 |
|---|---|---|
| REQ-SIZE-001 文件遍历 | 复用 lstat/scandir 的无跟随读取与 EINTR 处理；先检查保护/云占位 | VAL-SIZE-001：symlink 不走到外部，dataless 目录不枚举，异常形成 issue |
| REQ-SIZE-002 重复计量 | 硬链接按 device/inode 去重；跨任务和父子重叠按现有归属合并 | VAL-SIZE-002：同文件不重复累加；filesystem_subset 锚点不能吞掉父目录其它内容 |
| REQ-SIZE-003 文件系统 | Analyze 每个一级候选固定设备与文件系统边界；跨界内容跳过并标记 | VAL-SIZE-003：st_dev 或 f_fsid 变化都不跨入计量/执行 |
| REQ-SIZE-004 未知状态 | stat/权限/预算失败不当成完整零字节；已测部分可显示但声明不完整 | VAL-SIZE-004：部分计量与 total/measured/complete 一致 |
| REQ-SIZE-005 报告一致 | JSON、文本、详情读取同次证据；单位格式化不改变原始字节；不重复累计诊断桶 | VAL-SIZE-005：诊断不可回收、Analyze 为 0、Trash 移动不称已释放 |

云保护采用当前 Darwin `SF_DATALESS` 与 zero-block 启发式。
这不保证识别所有已下载的同步文件，不能声称已完成所有 File Provider 的真实兼容验收。

## 3. 专项数值解释

### retention

7/14/30 天值是达到对应年龄的累计桶，可互相包含，不能相加。
单文件年龄与 Worker 整组最新 mtime 不同；整组计量有跳过/错误时年龄未知。
容量大、年龄老或名称包含 log/runtime 都不能单独决定删除。

### SQLite

内部空闲量基于 page_size × freelist_count；它是数据库内部空间，不是磁盘空闲。
只读 immutable 探测不写 DB 或 sidecar，不读取业务行。WAL 单独报告；
immutable 视图不是活跃 WAL 数据库的最新事务一致快照，不能据此在线压缩或删 sidecar。

### Codex / Crashpad

staging 的 measured_count/measurement_complete 必须与有界发现相符。
Crashpad 的配对数与近期项是排除证据，不把所有 sidecar 或父目录统一当垃圾。
`filesystem_subset` 的路径只是锚点；计量覆盖匹配子集，不是父目录总量。

### deleted-open

同一 device/inode 多个 FD/进程仅计一次。逻辑上限不保证当前真实物理占用或可回收量；
该诊断的 potential/reclaimable 为 0。路径只作内部保护过滤，输出不公开已删除文件路径，
不通过删目录、杀进程或重启应用冒充 cleanup。

### updater 与签名资源

installed/staged 版本状态决定说明与保护；未知、待升级或缺失安装不能当旧缓存。
语言包审计不授权改签名应用。universal binary thinning 未实现，本篇不保留可照抄的写入步骤。

## 4. 实现与验收锚点

- 基础：[filesystem.py](../implementation/openclean/filesystem.py)、[models.py](../implementation/openclean/models.py)、[test_file_sizing.py](../implementation/tests/test_file_sizing.py)。
- 重叠/分析：[engine.py](../implementation/openclean/engine.py)、[test_analyzer.py](../implementation/tests/test_analyzer.py)、[test_slim_regressions.py](../implementation/tests/test_slim_regressions.py)。
- 专项：[test_storage_diagnostics.py](../implementation/tests/test_storage_diagnostics.py)、[test_workbuddy.py](../implementation/tests/test_workbuddy.py)、[test_updater.py](../implementation/tests/test_updater.py)。
- 呈现：[test_tui.py](../implementation/tests/test_tui.py)、[test_cleanup_cli.py](../implementation/tests/test_cleanup_cli.py)。

不为此次文档整理新增计量算法。对“物理收益”等更强承诺，必须有相应实验而不是措辞升级。

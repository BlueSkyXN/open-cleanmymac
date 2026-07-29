# 06 · 系统流程规格（数据流 / 控制流）

> 净室规格：描述 CleanMyMac CLI 端到端的运行时流程，供独立实现对齐。
> 来源：`analysis/` 二进制结构分析 + 元数据。只描述流程契约，不含代码表达。

---

## 1. 顶层命令流（用户 → 结果）

```
用户命令 (scan/clean <domain> [options])
   │
   ▼
命令解析（子命令 + 选项 + 目标域 + 根路径 + 忽略项 + 输出格式）
   │
   ▼
装配：选定域 → 构建扫描任务集（每域一个或多个 ScanTask）
   │
   ▼
扫描引擎：依赖解析 → 并发执行 → 进度聚合 → 结果汇总
   │
   ├─ 数据流：各任务产出"可清理项"(路径/大小/类别/安全级) → 去重合并
   └─ 控制流：pause/resume/cancel 事件 → 广播到每个在跑任务
   │
   ▼
结果：分类汇总 + potential/actionable/privileged/unsupported 空间 → 文本表 / JSON 输出
   │
   ▼ (仅 clean)
删除阶段：按安全级过滤 → 用户确认 → 普通项用户态删 / 受保护项走特权帮助器(XPC)
```

## 2. 单任务数据流（扫描器内部）

```
ScanningPathsProvider ──产出候选路径──▶ CompoundPredicate 过滤 ──▶ FileSizing 计大小
   (硬编码骨架 + 知识库补充)            (KB忽略 ∩ 类别规则)          (fts 遍历, 物理/逻辑)
                                                  │                        │
                                                  ▼                        ▼
                                          被忽略/受保护项            可清理项 + 大小
                                                  │                        │
                                                  ▼                        ▼
                                            标记不可删                标记安全级 → 结果流
```

## 3. 引擎控制流（三态传播）

```
            ┌─────────────────────────────────────────────┐
   UI/CLI ──▶  AggregatedControl (聚合所有任务控制句柄)      │
            └───────┬───────────────┬───────────────┬─────┘
                pause           resume            cancel
                  │               │               │
        ┌─────────▼────┐  ┌───────▼──────┐  ┌─────▼────────┐
        │ Task A 控制流 │  │ Task B 控制流 │  │ Task C 控制流 │   (AsyncStream<ControlAction>)
        └─────────┬────┘  └───────┬──────┘  └─────┬────────┘
                  ▼               ▼               ▼
            任务在安全点    任务从暂停处      任务尽快停在下个
            挂起等待        继续执行          安全点, 标记已取消
```

## 4. 进度聚合流

```
Task A 进度 ─┐
Task B 进度 ─┼─▶ CompoundProgress (按 WeightedProgressable 权重加权) ─▶ Snapshot ─▶ UI 刷新
Task C 进度 ─┘        Σ(进度×权重)/Σ权重                     itemsCount 不可变快照
```

## 5. 删除（clean）控制流

```
可清理项清单
   │ 按 safety 分级: safe / confirm / critical
   ▼
safe 项 ──用户确认(--yes 或交互)──▶ 删除
confirm 项 ──需显式开启(--include-confirm)──▶ 删除
critical 项 ──默认不动
   │
   ├─ 用户态普通项 → 同卷 Trash（清空 Trash 除外）
   └─ 受保护/系统项 → XPC 请求特权帮助器
                         │  消息(JSON): {version, operationKind, rootKind,
                         │               relativeComponents, expectedIdentity}
                         ▼
                     帮助器校验(签名/白名单) → 执行 → 回结果/进度事件
```

## 6. 知识库数据流

```
.cmmkb (编码+压缩+序列化对象图)
   │ 启动时: 解码 → 解压 → 反序列化
   ▼
内存知识库对象图
   │ 运行时查询: isPathIgnored / isSystemItem / additionalFiles / isDeepSearchNeeded ...
   ▼
各扫描任务的 Predicate（判定依据）
   │
   ▼ (可选) 在线更新服务 → 下载新 .cmmkb → 热替换
```

上图是参考对象的功能事实。本项目实现使用严格 schema 的 JSON：托管
`knowledge.json` 与用户 `rules.json` 分层，远程更新只由显式 HTTPS 命令触发，经签名、
sequence 防回滚和公钥钉扎后原子安装，不解析或复用 `.cmmkb`。

## 7. 权限分层流

```
启动 → 检测全盘访问(FDA)/辅助功能权限
   ├─ 已授权 → 全量扫描（含受保护区）
   └─ 未授权 → 仅扫用户态可达区；受保护项标记"需授权"
                       │
                       ▼ 用户触发 clean 且含受保护项
                 引导授权 / 调用特权帮助器(XPC)
```

---

## 8. 独立实现的对齐检查单

- [x] 引擎：并发任务、加权进度、不可变快照 + 三态协作取消
- [x] 引擎：任意任务的通用依赖图、拓扑校验、失败传播与并发调度
- [x] 统计：fts 式遍历 + 符号链接跳过 + 取消标志（已实现）
- [x] 判定：KB 优先保护闸 + 可组合谓词；兼容子串 `--ignore`
- [x] 知识库：自建明文 JSON（忽略/保护/应用附加文件）+ 用户 ignore 管理
- [x] 知识库：显式 HTTPS 签名更新、sequence 防回滚、公钥钉扎与原子安装
- [x] 删除：安全分级、显式确认、同卷 Trash、Docker 白名单和执行前复核
- [ ] 删除：特权帮助器/XPC
- [x] 输出：文本/JSON、TTY 加权进度和 curses 审阅

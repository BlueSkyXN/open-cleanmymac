# AR-04 · Run Store 与执行

[契约索引](_index.md) · [AR-01 对象模型](ar-01-object-model.md) ·
[AR-03 命令与 I/O](ar-03-cli-and-io-contract.md) · [架构](../../docs/ARCHITECTURE.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义 Run Store 的存储与标识契约（决策 2、3）、Finding → Action 状态机、`can_execute`
> 合取条件，以及对现有 `cleanup.py` live guard 的复用（决策 4 的运行时侧）。

## 1. 为什么需要 Run Store

当前 JSON item 没有 `run_id`/`finding_id`/`strategy_id`（见 `_item_payload`，
`implementation/openclean/cli.py`），选择只在**同一次命令内**按 path/identifier 有效。
要支持「先 `inspect`，稍后 `show`/`clean`」的跨命令 Agent 工作流，必须把一次识别的环境、
策略版本与 Finding 集合固化到本机私有存储，并绑定稳定标识。

改成 Finding ID 的价值不是修复一个「任意路径删除器」（当前并不存在），而是：

- 支持跨命令 Agent 工作流；
- 提供稳定审计标识；
- 绑定 Strategy 版本与扫描证据；
- 避免 Agent 重放或重新拼接路径；
- 让清理计划可被单独预览和审阅。

## 2. Run Store 存储契约（决策 2）

| 维度 | 契约 |
|---|---|
| 位置 | 本机**私有状态目录**，默认 `~/.local/state/openclean/runs/`（`$XDG_STATE_HOME` 优先），与现有 `~/.config/openclean/` 同属 per-user 私有配置族 |
| 权限 | 目录 `0700`，文件 `0600`；启动时校验，权限过宽则拒绝读写并 fail-closed |
| TTL | 默认 **24 小时**；每个 Run 记录 `created_at`/`expires_at` |
| 容量 | 有总容量与条目上限；超限按最旧优先淘汰（LRU），淘汰即失效 |
| 写入 | **原子写**：临时文件 + `fsync` + `os.replace`，与 `RulesStore._write_payload` 同一模式（`implementation/openclean/knowledge_base.py`） |
| 过期 | 过期后**拒绝执行**（退出码 `1`，`run_expired`），不自动重新扫描并假装是同一个 Finding |
| 内容 | Run manifest + 其 Finding 集合；不缓存可执行授权，只缓存识别证据 |

Run manifest 字段见 [AR-01](ar-01-object-model.md) §4。TTL 24 小时**不是安全有效期**：
即使 Run 未过期，执行前仍必须重新校验全部 live guard（§4–§5）。

## 3. 标识稳定性（决策 3）

- `run_id`、`finding_id` 稳定、唯一、跨命令可读；格式如 `run:<token>`、`finding:<token>`。
- **ID 不编码路径**：从 ID 本身不能反推目标路径；目标只在 Run Store 内按 ID 解析。
- ID 不可跨会话重放：脱敏输出替换 actionable ID 且 `selection_replayable=false`
  （见 [AR-03](ar-03-cli-and-io-contract.md) §7）。
- `finding_id` 绑定 `run_id`、`strategy_id`、`strategy_version`、目标 identity、`observed_at`
  与 `expires_at`；`show`/`clean` 校验 `--finding` 属于 `--run`。
- **`finding_id` 本身不构成授权**（见 §4）。

## 4. `can_execute` 合取条件

Finding ID 不等于动作授权。某个 Finding 可执行，当且仅当以下**全部**成立：

```text
can_execute =
    user_confirmed                    # 用户对当前 Finding 明确授权（--yes + 风险 flag）
    AND strategy.status == trusted    # 只有 trusted 策略具备动作能力（AR-02 §3）
    AND action.supported              # 动作已验证可执行
    AND run_not_expired               # Run 未过期且存在
    AND strategy_hash_matches         # 当前 pack hash 与 Run 记录一致（AR-02 §4.1）
    AND target_identity_matches       # 目标 device/inode/owner 与识别时一致
    AND current_protect_rules_allow   # 当前 KB protect/ignore 未命中
    AND live_guards_pass              # 执行前实时复核全部通过（§5）
```

任一条件不成立 → `can_execute=false`，在 `block_reasons` 给出结构化原因，退出码 `1`
（执行请求）或预览标注（预览请求）。即使 Strategy 是 `trusted`，只要应用运行中、句柄状态
未知、identity 改变或命中 protect，当前 Finding 仍是 `actionable=false`。

## 5. Finding → Action 状态机

对齐现有写操作状态机（[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §5），并前置
Run/Finding 解析：

```text
[*] --> Inspected: inspect → Run + Finding（写 Run Store）
Inspected --> Shown: show --run --finding（只读）
Shown --> Resolved: clean 预览 → CleanupPlan（mode=preview, executed=false）
Resolved --> Rejected: can_execute 任一条件不成立
Resolved --> Confirmed: --yes + confirm/critical 风险授权
Confirmed --> Audited: 批量预检 all-or-nothing（_audit_item）
Audited --> Rejected: live inode/owner/symlink/dataless/进程/updater/protect 变化
Audited --> Trashed: move_to_trash（普通文件系统项）
Trashed --> Reported: CleanupOutcome 回执
Rejected --> [*]
Reported --> [*]
```

批量语义（复用 `execute_cleanup`，`implementation/openclean/cleanup.py`）：

- **all-or-nothing 预检**：任一选中 Finding 在执行前预检失败，整批不开始，全部标
  `blocked`/`not_run`。
- **逐项 live 复核**：每项操作前再次复核 identity 与 guard。
- **identity 变化整批 fail-closed**：识别与执行之间目标 device/inode/owner 改变即拒绝。

## 6. 复用现有 live guard（不重写）

`live_guards_pass` 与 `target_identity_matches` **完整保留**现有 `cleanup.py` 的复核，
v1 不新造执行器：

| 现有复核 | 位置 | v1 契约中的角色 |
|---|---|---|
| 保护闸优先（KB protect/ignore 先于普通谓词） | `predicates.py` `ProtectionGate` | `current_protect_rules_allow` |
| 选择唯一性 + actionable + confirm/critical 授权 | `cleanup.py` `select_cleanup_items` | 授权语义基线；selector 来源改为 Finding ID |
| 非特权动作阻断、symlink ancestor 拒绝 | `cleanup.py` `_validate_cleanup_scope` | `live_guards_pass` |
| Darwin cache 根重新发现 + inode/owner 复核 | `cleanup.py` `_validate_cleanup_scope` | `target_identity_matches` |
| 失效启动项 live re-stat + 云占位 + inode 复核 | `cleanup.py` `_validate_startup_item` | `live_guards_pass` |
| 进程快照 + 运行状态保护 | `cleanup.py` `execute_cleanup` / `processes.py` | 运行中 → `actionable=false` |
| 同卷 Trash no-follow + `renameatx_np(RENAME_EXCL\|RENAME_NOFOLLOW_ANY)` | `cleanup.py` `trash_directory_for` / `_move_to_trash` | `move_to_trash` 动作实现 |
| Docker binding 复核（CLI realpath/context/host/Engine ID） | `cleanup.py` `_prune_docker_item` / `docker.py` | 未来 docker pack 的 specialized 动作 |

`SF_DATALESS`/疑似云占位在枚举与最终移动前都检查（现有 `FileFacts.is_probable_cloud_placeholder`）。
这些措施降低 TOCTOU，但不是对同 UID 恶意进程的绝对隔离——沿用
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §6 的既有边界声明。

## 7. 授权语义（决策 4 的运行时侧）

- **Finding ID ≠ 授权**：解析出 Finding 只是定位目标，执行仍需 `user_confirmed`。
- **什么构成授权**：用户对**当前 Finding**明确表达执行意图，Agent 才加 `--yes`；`confirm`/
  `critical` 仍需对应风险 flag。授权语义的正负测试见 [AR-06](ar-06-codex-p0-acceptance.md) §3。
- **过期或策略变化必须重新确认**：`run_expired` 或 `strategy_hash_mismatch` 时不得沿用旧授权。
- **批量授权**：`--finding` 可重复，但批量执行 all-or-nothing；不允许「授权一个、顺带执行同
  等级其他项」。
- **Agent 可自动生成预览，但不能自动执行**：不带 `--yes` 的 `clean` 永远只读。

## 8. CleanupOutcome 回执

执行结果复用现有 `CleanupReport` 语义（见 [AR-01](ar-01-object-model.md) §8）：区分
`moved_to_trash_bytes`（暂存、尚未释放）与 `permanently_deleted_bytes`（Trash 清空或
Docker prune）。`partial`（如 Docker 不可逆副作用未知）与 `failed`/`blocked`/`not_run`
状态沿用现有定义；不得把「发现可回收容量」汇报为「已释放空间」。

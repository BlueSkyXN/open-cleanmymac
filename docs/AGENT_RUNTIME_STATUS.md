# Agent Runtime 当前实现：0.24.0a1

**当前提供 Codex 与 WorkBuddy 只读探测、持久化 Run/Finding、展示、计划预览；生产清理策略尚未启用。**
经典 `scan`、`clean <category>`、`analyze`、`purge` 和 TUI 保留，与 Agent 命令并存。
这是 Alpha 预发行范围，不代表稳定版或全部真实环境验收完成；具体 tag 和产物以 GitHub Releases 为准。

```text
内置 Codex（7 条）及 WorkBuddy（1 条）pack，均 active/report_only
       │ inspect：只读探测目标，写入本机 Run bundle
       ▼
Run v2 + Finding ── show ── clean --run --finding（预览）
       │
       └── --yes：当前生产包不具备动作条件，返回 blocked / exit 1

未来生产动作：真实结构证据 + 明确策略审批 + 固定 pack hash
           → 独立确认和重新探测 → 复用经典同卷 Trash 执行器
```

## 当前可用与未实现

`inspect codex`、`inspect workbuddy`、`inspect all`、`show`、`strategy list/show/verify`、Finding 计划预览已实现。
`inspect all` 指全部已安装 pack，当前为 Codex 和 WorkBuddy，不等于经典五域或所有规划中的产品。
`inspect` 不删除候选，但会创建 Run Store，不能宣称完全不写磁盘。

内置生产审批集合 `APPROVED_PACK_HASHES` 为空。仅在 JSON 中写 `trusted` 或改
`action.supported=true` 不能使策略获得动作能力；外部 `--packs-dir` 也不能执行。
测试中的 synthetic approval 只服务临时目录，不是正式策略来源。
Codex 动作所需的 `require_structure_match=true` 当前仍明确阻止执行，不能把名称匹配和
目录年龄包装成动作验证。WorkBuddy 只读结构和个人来源见 [EXPERIENCE.md](EXPERIENCE.md)，并未获得动作审批。
正式 promotion、其他未交付 pack、`explore`、`lab`
和 MCP 均不是本版本已完成能力。

## 存储、范围和版本

Run Store 默认在绝对 `$XDG_STATE_HOME/openclean/runs`，否则为
`~/.local/state/openclean/runs`。`inspect --home PATH` 的 `~` locator、分区日志发现、默认规则
与默认 Run Store 均以 PATH 为基准；该选项用于隔离开发，不改变真实环境变量。
自定义 HOME 的 Run 默认位于 `PATH/.local/state/openclean/runs`，后续 `show/clean` 预览应显式
传入 `--run-store`。生产 `--yes` 仅接受当前 HOME 与默认 Store，不接受外部 pack 或非默认 Store。

每个 bundle 原子写入，一个 Run 与其全部 Findings 一次读取。目录 `0700`、文件 `0600`，
不接纳链接、错误所有者、缺字段、额外字段、重复 JSON 键、错误类型或非有限数字。
Run ID 与文件名、Finding 清单、归属、策略版本、目标/证据/计量/评估必须一致。
容量上限：64 个 Run、每个 8 MiB、合计 64 MiB；按最早写入顺序淘汰，读取不刷新次序，
**不是 LRU**。TTL 最长 24h，过期读取会删除该条并报错，不会自动重扫。

持久化 bundle 为 schema 2，Run 新增 `home`、`strategy_versions`。旧 bundle 不补造信息，
需重新 `inspect`；没有自动迁移或执行降级通道。

## 计划不等于可直接执行的凭证

`resolve_plan` 检查归属、完整性、期限、运行时版本、策略审批/hash/version、Finding 可动作性、
风险确认和目标类型。`can_execute=true` 只描述这一步的条件，不保证稍后的实时状态。
重复选择同一个物理路径的不同 Findings 被拒绝。

`execute_plan` 要求再次传入 Run、registry 和明确用户确认，重新生成计划、比对目标，并在
Run 的 HOME 重新探测进程、句柄、年龄、文件身份与当前保护规则。执行使用新探测的 Item，
不直接信任持久化 Item 的 domain 等路由字段。只支持逐目标 filesystem → move_to_trash，
聚合根永不动作。无法读取实时状态、目标变化或部分扫描会停止整批启动。

**整批拒绝只针对执行前检查，不是文件系统事务。** 真正开始多个移动后，操作系统仍可能让
部分项目成功、其余失败；结果逐项记录，不宣称自动回滚或“要么全成要么全没动”。
经典 `cleanup.py` 原生执行边界保持不变；本版本未新增 Linux 产品执行支持。

## JSON 与退出码

CLI envelope 使用 `schema_version=2`。`clean` 返回 `mode`、`executed`、`plan.plan_items[]`
和执行阶段的 `outcome`，不再输出歧义字段 `plan.items`。Run bundle schema 2 是独立版本。
全阻止或混合批次：`executed=false`、`outcome.complete=false`、逐项 `blocked/not_run`、exit 1；
执行已尝试但失败：`executed=true`、`execution_failed`、exit 1。
成功预览即使没有可执行项仍 exit 0，消费者必须读取 `can_execute` 与原因。
不完整 inspect：`status=incomplete`、`complete=false`、exit 1。
参数/规则/选择归属错误为 exit 2；不存在/过期 Run、缺少 pack、动作被拒绝为 exit 1。

`--redact-paths` 同时处理目标路径、HOME、Store、说明中的路径与 Run/Finding ID，
`selection_replayable=false`。脱敏占位符不能用于选择。原始本机 bundle 仍保留精确证据，
不可把脱敏输出视为已清除本机存储。

## 开发与验证

macOS 本地默认只运行 `make check` 和受影响测试；全量测试、构建、归档与安装验证由
GitHub Actions 完成，不要求本机额外 Python 版本或精确开发工具版本。按需运行 `make preview`
只处理 TemporaryDirectory，不应拿真实 HOME 来演示写操作。
Linux 可以检查纯逻辑，但不能代替 Darwin `renameatx_np`、SF_DATALESS、真实 lsof、签名帮助器
和真实 Docker 的验证。私人交接包的 `local/README.md` 提供本次环境、原始日志与接手步骤。
公开 wheel/sdist 不含 `local/`、`analysis/` 或 `.git/`。

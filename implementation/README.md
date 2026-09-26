# implementation

[仓库 README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md)

`openclean` 的 Python 实现层。运行时只使用标准库，要求 macOS 和 Python 3.11+；
CI 配置为 macOS 15 / 26 双版本、Python 3.11；通过情况以对应提交的 Actions 为准。
许可证为随包 [GPL-3.0](LICENSE)。用户安装与安全默认见
[仓库 README](../README.md)；本页只记录 CLI、JSON、选择和规则契约。

## 安装与入口

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./implementation
.venv/bin/openclean --version
.venv/bin/python -m openclean --help
```

源码 checkout 也可直接运行：

```bash
cd implementation
PYTHONPATH=. python3 -m openclean --version
PYTHONPATH=. python3 openclean_cli.py --version
```

`openclean_cli.py` 是 checkout/sdist 便捷入口；wheel 的正式入口是 console script 和
`python -m openclean`。低于 Python 3.11 时 help/version 仍可读，业务命令返回
`unsupported_python`。

## 命令面

以下示例默认只读：

```bash
openclean scan
openclean scan --domain developer --domain ai --json
openclean scan --domain project --project-root ~/Code
openclean clean                         # 四类候选预览
openclean clean dev --no-interactive
openclean purge ~/Projects --no-interactive
openclean analyze ~ --top 20 --no-interactive
openclean large ~/Downloads --min-size 100MiB --top 50 --json
openclean ignore list --json
openclean config --json
openclean optimize ram --json           # 预期 unavailable，退出 1
openclean cat
```

写操作示例仅用于说明契约，不应在未审阅候选时直接运行：

```text
openclean clean dev --yes
openclean clean trash --select EXACT_TRASH_ROOT --include-confirm --yes
openclean purge PATH --yes
openclean analyze PATH --select EXACT_CHILD --yes --no-interactive
openclean ignore add PATH
openclean config --analytics off
openclean config --update-knowledge HTTPS_URL --knowledge-public-key publisher-public.pem
```

前四条会修改文件；清空 Trash 和 Docker prune 是永久操作。验证写路径应运行
`make preview`。

## 后续源码的缓存保护与 Purge 说明（未发布）

通用用户缓存与 Darwin 用户缓存先使用静态进程归属；未命中的一级 bundle-ID 型子目录
通过系统 `mdfind -0` 查询，再核对实际 `.app/Contents/Info.plist` 的 `CFBundleIdentifier`，
并在 `/Applications`、`/System/Applications`、`~/Applications` 的一级应用中补查。
不推断 helper 后缀，不依赖更新器版本字段，不将查不到应用解释为卸载残留。
依据为 macOS `mdfind(1)` 的公开元数据查询接口及公开的 CFBundleIdentifier / bundle 结构。

同次扫描共享查询缓存：最多 8 个 ID，每次 Spotlight 超时不超过 0.5 秒，累计处理预算 4 秒；
标准安装目录最多检查 512 个一级条目，单次 Spotlight 最多处理 32 个结果。
预算在查询和条目之间检查，不是所有文件系统 I/O 的硬超时。拒绝符号链接和云占位元数据；
同 ID 多个已验证安装位置合并保护，不任意选一个。查询失败、未找到和预算不足在 note 中区分，
保留未知候选原有可执行性及不默认预选的行为，不表示已证明安全。
应用路径标记沿用 `running_process_markers`，执行前按当前进程复核；应用移动或重装后应重新扫描。
`--redact-paths` 隐藏路径型标记，普通进程名不变。

Purge 的 `note` 区分依赖重装、环境重建、索引和构建后果；恢复取决于项目配置、源码、工具链
和依赖源。`age_days` / 默认预选依据产物及其内容的最新 mtime，不评估整个项目是否活跃。
本次仅改善解释，不改变发现、选择或安全等级，也不新增 rebuild_cost / 缓存标签评分。

### AI 浏览器缓存路径补齐

Codex 的 `~/Library/Application Support/Codex` 根、`Default`、`codex-browser-app`、
`Partitions/codex-browser-app` 和 `Default/Partitions/codex-browser-app` 仅识别精确
Cache/Code Cache/GPU/Dawn 缓存；根级另含 shader 和 CRX 下载缓存。它们为 confirm、
默认不选，运行中或进程检测失败不可执行，移动前再次核对进程。Cookies、History、
IndexedDB、Local Storage、会话、WidevineCdm、WasmTtsEngine 和整个 profile 不作为缓存候选。
上述精确根的 `Service Worker/CacheStorage` 复用 retention 诊断，归入 AI 域；即使应用已退出，
仍固定 `actionable=false`，不提供删除执行器。未识别的分区与近似目录名不自动扩面。

WorkBuddy 的 `com.workbuddy.workbuddy.BundleMigration` 与
`com.tencent.workbuddy.mac.BundleMigration` 各自校验对应 bundle ID 的 app/ZIP 元数据，
复用 staged/installed 版本状态机及执行前复核；两代 ID 不自动互相替代。
多个安装副本版本冲突继续报 `version_unknown`，不按最高版本或应用名称猜测目标。

已有 Antigravity 数据根增加 Default 下的 Cache、Code Cache、GPUCache、DawnGraphiteCache、
DawnWebGPUCache、Service Worker/CacheStorage；已有 chrome-devtools-mcp 数据根增加
Default/DawnCache（旧版名称）和根级 GrShaderCache。仅匹配精确路径，不新增 Profile *、
自定义根、Cookies、Login Data、IndexedDB、会话或整个 profile 扫描；根级旧规则保留。
进程快照同时能识别包含数据根名称的 Chrome 启动参数，因此 Agent 退出而 Chrome 仍运行时
继续阻断；进程探测失败也阻断。AI 候选仍不默认选，清理需要既有显式授权和执行前复核。
依据与版本见 [能力地图](../docs/CAPABILITIES.md#浏览器缓存补齐的公开依据)。

## Agent Runtime 命令面（附加）

当前 `0.24.0a2` 内置 Codex 的 7 条策略及 WorkBuddy 的 1 条结构策略，均为 active/report_only，支持探测、
持久化展示和计划预览，不启用生产清理动作。与经典命令和 TUI 并存。

```bash
openclean inspect codex --json
openclean inspect workbuddy --json
openclean inspect all --json
openclean show --run RUN_ID --finding FINDING_ID --json
openclean clean --run RUN_ID --finding FINDING_ID --json   # 仅预览
openclean strategy list --json
openclean strategy verify --json
```

`inspect` 不修改候选，但写入本机 Run Store。未安装 pack 返回 `pack_not_found` / exit 1。
内置包的 `clean --run ... --yes` 返回 blocked / exit 1，不能绕过不可动作原因。
WorkBuddy 经典 AI 域与该包共用 expired/Worker/Electron 只读 detector，经验依据见
[EXPERIENCE.md](../docs/EXPERIENCE.md)。不要求经典可执行功能先迁入 Pack；Agent 授权内的经典调用见
[AI_USAGE.md](../docs/AI_USAGE.md)。

Run bundle schema 2 包含 HOME、策略版本和完整 Finding 清单；单文件 8 MiB、总计 64 MiB、
最多 64 Run，最早写入优先淘汰，最长 TTL 24h。旧格式/过期/损坏数据需重新 inspect，绝不自动重扫。
`--home` 同时控制 locator 展开、日志分区、默认规则和默认 Store；外部 pack 与自定义 Store 只作预览。
`--ignore` 与规则会在 Agent 探测和执行前重新应用。

CLI envelope 为 schema 2，计划数组仅为 `plan.plan_items[]`。混合阻止批次返回
`executed=false`、`complete=false`、逐项 `blocked/not_run`，不把空报告当成功。
执行前计划检查与实时检查分开；不保证多个文件移动具有事务回滚。
脱敏输出的 Run/Finding ID 不可回放。

详细契约、错误与仍未实现的能力见 [Agent Runtime 当前状态](../docs/AGENT_RUNTIME_STATUS.md)；
已有对象、接口与执行契约见 [specs/agent-runtime/](../specs/agent-runtime/_index.md)；
全量迁移与退役经典能力的旧路线已撤销，未批准扩展不作为当前开发任务。

## 大文件扫描

`large [path]` 递归扫描指定目录，省略路径时使用家目录。它与一级目录 `analyze`、垃圾
分类 `scan` 并存，包含所选范围内应用/缓存中的普通文件，不能把命中视为垃圾。

- `--min-size` 默认 `100MiB`，支持正字节数、十进制 KB/MB/GB/TB 和二进制 KiB/MiB/GiB/TiB，
  如 `1.5GiB`。按表观大小筛选和降序排列，实际分配块独立报告。
- `--top` 默认 50，0 显示全部；`--max-entries` 默认 200000，限制检查的文件/目录项总数。
- 复用 `--ignore`、`--rules`、`--json`、`--redact-paths`。跳过符号链接、云占位、保护路径、
  特殊文件及其它设备/文件系统。硬链接只保留一个已发现路径、容量只累计一次。
- `latest_mtime` / `age_days` 是修改时间和距今天数，不是最后访问/使用时间；本版不接入
  Spotlight，也不提供旧文件年龄过滤或文件删除参数。

JSON schema v2 新增 `command=large`，已有命令格式不变。`items` 复用文件元数据投影；
`size_basis=logical`、`min_size_bytes` 说明筛选口径，`logical_bytes` / `allocated_bytes`
是全部已匹配文件合计，`matched_file_count` / `returned_file_count` 区分发现和显示数量。
`truncated` 仅表示 top 截断；扫描预算耗尽、权限或元数据错误令 `complete=false` 并返回 1，
同时保留部分结果和 issues。取消返回 130。参数/根路径/规则无效返回 2。
`skipped` 明示忽略、读取失败、链接、云占位、跨文件系统、重复硬链接和特殊文件数量；
`complete=true` 只表示完成上述边界内的遍历。所有项 `actionable=false`、`preselected=false`，
`reclaimable_bytes` / `preselected_bytes` 为 0；分配块也不代表 APFS 独占或删除收益。

## 选择与执行

无参数 `openclean` 在 TTY 下打开五项主菜单：Clean、Purge、Analyze、Optimize、Config。
方向键移动、Enter 进入，`M` 打开 More（Cat/返回）。主菜单 `Q/Esc` 退出，次级菜单返回；
子任务完成后保留结果，按 Enter 返回原菜单。终端初始化失败退回行式菜单。
主菜单 Analyze 先选择家目录、当前目录、自定义目录或启动盘，再传递明确路径；其余任务默认范围不变。
直接运行 `analyze` 的默认路径仍为 `/`；菜单不附加 `--yes`，Optimize 只展示现有 refusal。
非 TTY 无参数仍输出帮助并退出 0，显式命令与 JSON 不进入菜单。

- 没有 `--yes` 时，`clean`/`purge`/`analyze` 即使带选择参数也只预览。
- 没有 `--select` 时，普通 `safe` 扫描点可默认预选；扫描点可以独立关闭默认选择，AI 域
  即使是可重建缓存也统一默认不选。`--all`/`--include-confirm`/`--include-critical`
  分别扩展对应批量层级。
- 一旦指定 `--select`，选择集从空开始，不继承默认预选；confirm/critical 精确目标仍需
  对应 `--include-confirm`/`--include-critical` 作为风险授权，但不会顺带选择同等级其他项。
- Purge 对 `.vitepress` 只识别其一级 `cache`、`dist`，不把整个目录、配置或主题源码视为产物。
- 硬链接按 device/inode 跨扫描点合并计量；共享容量归属不改变其它路径的选择资格。
  被扣除共享容量的候选可能显示 0 字节，note 会解释原因；这不是其内容为空或无需审阅。
- `--select` 与 `--all` 语义冲突，CLI 在扫描前返回 exit 2。
- `requires_explicit_selection=true` 的环境来源项和 Docker prune 不会被默认选择、`--all`
  或 tier 批量参数选中，必须使用完整路径或 identifier 精确选择。
- `--force --yes` 只执行默认预选项，并拒绝与任何扩大选择参数组合。
- 普通用户态路径移动到同卷 Trash；`clean trash` 永久删除内容但保留 Trash 根目录。
- Docker Build Cache/Images/Containers 分别映射固定官方 prune 命令且均需 identifier
  精确选择；Local Volumes 始终不可执行。
- 特权系统项、ApplicationLanguages、云保护项、只读诊断和不支持资源无法被参数强制解锁。
- `analyze` 的一级候选统一为 `critical + requires_explicit_selection`；非交互执行必须精确
  `--select`，全屏执行在 `Y` 后还需按 `!` 完成 critical 二次确认。
- updater 候选统一为 critical 且要求精确选择；`pending_update`、`installed_app_missing`
  和 `version_unknown` 不可执行，同版/旧版残留在执行前仍会重新比较版本。

连接 TTY 时，`clean`、`purge` 和 `analyze` 默认进入 curses 界面（先扫描页后审阅页）。
JSON、管道、`--no-interactive` 和任何参数化选择 flag 不打开 TUI；`--interactive`
显式要求全屏界面，需要 stdin/stdout 均连接终端，且不能与 `--json` 或参数化选择
组合（冲突在扫描前以 `invalid_mode_options` 拒绝，exit 2）。TUI 的选择只是选择；
实际执行仍要求启动命令带 `--yes`，并在汇总页再次按 `Y`。模式矩阵与集中路由见
[docs/MODES.md](../docs/MODES.md)；快捷键见 [docs/PREVIEW.md](../docs/PREVIEW.md)。

扫描页在 clean/purge/analyze 间复用同一交互：`Space` 请求暂停/继续（“已暂停”只在实际
工作线程全部到达暂停点后显示，阻塞 I/O 期间保持“暂停已请求”），`Q` 取消审阅（exit 0），
`Ctrl-C` 中断（exit 130）；取消后等待 worker 完成收尾，不重开扫描。

Clean/Purge 的逐项列表新增 `I` 只读详情：方向键滚动、Esc/左键返回原位置，`Q` 取消审阅。
Space/Enter 仍在逐项列表切换选择，详情页不改变选择。详情只读取当前 Item 的路径/identifier、
说明、年龄、句柄和诊断字段；未测数据标为未知。Clean 文本报告同步提供简短诊断摘要，
JSON 字段与容量口径不变。子集锚点不代表父目录可删除，retention 年龄桶有重叠、不可累加。

### 终端显示

CLI 的 scan/clean/purge 文本报告把候选信息、完整路径和阻断说明分行；摘要区分发现量、
当前可执行量、当前选择（scan 为默认预选）和只读/阻断量。文本输出保持无 ANSI 控制序列，
JSON schema、选择参数和执行资格不因显示方式改变。大文件表头按终端显示列对齐中文。

TUI 默认 `OPENCLEAN_THEME=auto` 使用终端自身的前景/背景，焦点整行反白，标题与危险提示
辅以下划线，选择/阻断保留文字标记。白底、深色的 iTerm2 和 Terminal 配置均沿用其默认色；
不使用可能受独立配置影响的粗体颜色或淡化提示，不猜测背景、不查询终端或读取终端偏好。

| 环境变量 | 显示行为 |
|---|---|
| 未设置 / `OPENCLEAN_THEME=auto` | 沿用终端默认色，无自定义状态配色；随终端默认色变化 |
| `OPENCLEAN_THEME=light` | 白色或浅色底使用深蓝、棕、红、绿状态色 |
| `OPENCLEAN_THEME=dark` | 深色底使用亮蓝、黄、红、绿状态色 |
| 设置 `NO_COLOR`（包括空值） | 优先禁用自定义状态配色，保留反白/下划线/文字状态 |

显式配色需要终端支持至少 256 色，使用 16 以上的色索引、保留默认背景，不改动终端调色板。
请按实际背景选择 light/dark；低颜色能力、`TERM=dumb` 或颜色初始化失败时沿用默认色，
未知主题值视作 auto。此变量仅影响 TUI，CLI 文本和 JSON 不受影响。

长文本按显示列省略，Clean/Purge 仍可按 I 查看完整路径；快捷键按整组换行。
窗口小于 48 列 × 14 行时保留选择并提示调整尺寸，只接受退出，不处理隐藏的确认按键。
Analyze 在扫描前显示加载页，后台分批计量；主线程处理按键，进度限频，不以启发百分比冒充实际完成率。
`Q` 取消审阅，`Ctrl-C` 中断返回 130；取消先传递到扫描检查点，再等待工作线程收尾。
不可中断的系统调用或已有外部查询仍需等待其返回/超时，不把发出取消显示为已经停止。
返回上级复用当前会话的目录视图与光标，`R` 重新扫描；缓存最多 16 个视图、累计 20,000 条，
超大结果可显示但不缓存，退出即丢弃。缓存不是新的执行凭证：提交前重新分析各选择来源，
身份、大小、风险或可执行性变化时撤销相应选择，最终执行仍由原有预检与实时保护负责。

`I` 查看当前项路径、容量、风险和说明；`E` 查看全部 issues，方向键滚动、Esc 返回。
不可执行项显示“只读”，焦点阻断原因和用户操作反馈分别显示；本页不可操作时标为仅浏览。
TUI 补充空目录/零占用浏览行，这些行不可选择，不改变非交互或 JSON 的候选集合。
权限失败的未知容量显示“未测”，不冒充真实 0 B。提交结果的完整性同时考虑当前页面和
所选项重新分析的来源；不完整正常提交返回 1。用户主动 Q 放弃审阅仍返回 0，不代表扫描完整。
Analyze 的公共参数、JSON schema v2、`--top` 显示截断和 critical 执行条件保持不变。

## JSON schema v2

成功结果包含 `schema_version=2`：

- `potential_bytes`：发现的物理占用；
- `reclaimable_bytes`：清理域中 `actionable=true` 候选占用；`analyze` 顶层及每个 entry
  固定为 `0`，因为空间占用本身不等于垃圾；
- `requires_privilege_bytes`：当前需要特权 helper 的占用；
- `unsupported_bytes`：既不可执行又非特权候选的占用。

`scan` 额外返回 `command`、`mode` 和 `requested_domains`。`analyze` 返回 `top`、
`truncated`、`entry_count_total`、`entry_count_returned`。已枚举的 Analyze 子项在计量前消失时，
返回 `path_disappeared`、`complete=false` 和退出码 1，同时保留其它已测结果；
普通扫描点对应的软件未安装、目录不存在仍正常跳过。路径候选的
`cross_device_paths` 表示递归时跳过的其它文件系统挂载点数量；非零候选不可执行。
`device_id` 是本次启动中的文件系统设备标识；顶层 `volumes` 按设备分别汇总
`mount_point`、`system_disk` 和容量。updater 项额外返回 `updater_status`、
`installed_version`、`staged_version` 与 `updater_external_install`。

只读诊断使用 `diagnostic_kind`，固定 `actionable=false`、`reclaimable_bytes=0`：

- `retention`：文件数、打开句柄以及 7/14/30 天物理容量；
- `sqlite_freelist`：page/freelist、内部空闲容量与比例、数据库总大小和 WAL/SHM/journal；
- `updater_temp`：动态 ShipIt app 的版本状态；
- `codex_transient`：`total_count`、`open_handle_count`；marketplace staging 另有
  `measured_count` 和 `measurement_complete`；
- `crashpad_pairing`：`paired_artifact_count`、`recent_artifact_count`；
- `open_unlinked`：`potential_bytes=0`，逻辑上限只进入 `logical_bytes`、`total_count`、
  `related_process_count` 和 `open_handle_count`。

`resource_kind=filesystem_subset` 的 `path` 只是聚合锚点，字节数仅覆盖命中子集。
`optimize --json` 返回：

```json
{
  "schema_version": 2,
  "command": "optimize ram",
  "mode": "guard",
  "status": "unavailable",
  "executed": false,
  "reason": "..."
}
```

参数、规则、路径、选择和配置错误使用：

```json
{
  "schema_version": 2,
  "command": "scan",
  "status": "error",
  "executed": false,
  "exit_code": 2,
  "error": {"code": "usage_error", "message": "..."}
}
```

默认 JSON 包含精确绝对路径。所有 JSON 子命令可显式增加 `--redact-paths`，在最终
序列化阶段把同一文档内路径映射成稳定 opaque ref；输出声明
`selection_replayable=false`，不能直接用于后续 `--select`。`complete=true` 只表示
没有 blocking issue；仍应检查 `issues`。

清理回执中的 `moved_to_trash_bytes` 与 `permanently_deleted_bytes` 是操作计量，
不是执行前后磁盘可用空间的净增量；文本与 TUI 不承诺等量释放空间。

Agent `clean` 的嵌套计划也应脱敏路径和 Run/Finding ID。已发布 `0.24.0a1` 在此处存在
元组遍历遗漏；`0.24.0a2` 已在源码修复，旧安装包输出不能因带有 `redaction.enabled=true` 就视为可公开分享。
升级后重新 inspect；不同 runtime version 的旧 Run 不能直接用于执行。

## 自建规则

默认规则路径是 `~/.config/openclean/rules.json`；`--rules FILE` 使用显式单文件。
规范格式只支持 JSON：

```json
{
  "schema_version": 1,
  "ignore": {
    "paths": ["~/Library/Caches/KeepMe"],
    "globs": ["**/CacheStorage/keep-*"],
    "regexes": ["/important-[^/]+$"]
  },
  "protect": {
    "paths": ["/System", "/usr"]
  },
  "applications": {
    "com.example.app": {
      "name": "Example",
      "protected": true,
      "additional_files": ["~/Library/Application Support/Example"],
      "deep_search": false
    }
  }
}
```

`paths` 必须是绝对路径或 `~` 路径，并匹配自身和后代。`globs`/`regexes` 匹配规范化
绝对路径。`protect` 和 `ignore` 都阻止扫描/执行，且 KnowledgeBase 保护闸最先求值。

## 签名托管知识库

项目不内置更新 URL、公钥或第三方私有规则。更新只由显式命令触发，接受的 envelope：

```json
{
  "envelope_schema_version": 1,
  "sequence": 42,
  "created_at": "2026-08-31T12:00:00+08:00",
  "signature_algorithm": "openssl-dgst-sha256",
  "rules": {"schema_version": 1},
  "signature": "<base64 signature>"
}
```

客户端限制 2 MiB、只接受 HTTPS、拒绝 URL 凭据，使用用户提供的 PEM 公钥调用系统
OpenSSL 验证 SHA-256 签名。成功后钉住公钥指纹和递增 sequence；`os.replace` 是安装
提交边界。托管 `knowledge.json` 与用户 `rules.json` 分层合并，远程更新不覆盖用户
ignore。

## 路径安全

扫描和执行拒绝候选或 ancestor symlink；环境变量缓存根只允许位于 `~/Library/Caches`
或 `~/.cache`，并强制精确选择。已知应用缓存、Darwin user cache 直接子项、updater
残留和只读诊断都在模型层标记不可执行或要求精确选择，不能被参数解锁。`analyze`
同时比较每个一级候选的 `st_dev` 与 `statvfs().f_fsid`。普通移动使用 no-follow 目录
fd 和 Darwin `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)`。Docker prune 绑定
扫描时 CLI realpath、context/host、endpoint 和 Engine ID，执行前复核。细节见
[架构说明](../docs/ARCHITECTURE.md) 和 [安全政策](../SECURITY.md)。

`GOCACHE`、`GOMODCACHE`、`HOMEBREW_CACHE` 与已有的 `UV_CACHE_DIR`、`POETRY_CACHE_DIR`
共用以上限制，来源标记为 `environment`、至少 confirm、默认不选且必须精确选择。
内置默认路径继续扫描；环境变量增加候选，不证明工具正在使用该目录。外置卷、自定义 HOME
之外的缓存与按卷 pnpm store 仍未支持；路径被拒绝时报告 `unsafe_environment_path`，
不能把这项增强当作任意缓存迁移后的发现能力。

Clean、Purge、Analyze 与执行器共用应用/updater 范围判定。环境变量目标同样复用静态
归属、已注册扫描点（含 `.cache` 内工具）和有界 bundle ID 查询；精确根、后代及包含
实际已知子项的父容器都保留运行保护，相似名称不匹配。含 updater 暂存区的父目录/子目录
不支持整体或部分清理，须重新扫描并精确审阅受支持的 updater 根。
同路径的具体分类不能覆盖另一入口的运行保护或不可执行判定；updater 元数据、`critical`
及精确选择要求保留到执行前复核，版本证据不一致时要求重新扫描。普通缓存的通用分级仍
由具体分类细化，不因合并统一升级风险等级。
批量预检、创建 Trash 前及移动前分别重新识别适用保护；旧扫描没有进程标记或 updater
状态，也不会跳过实时检查。扫描后新增暂存包（包括同版包）、版本变化或运行中的保护对象
会使旧选择失效。探测先检查逐级路径，不穿过符号链接、云占位或忽略/保护目录。

规则读取错误（包括非 UTF-8、JSON 嵌套过深）统一返回 `rules_error`/exit 2；
`config` 的对应错误为 `config_error`/exit 2。JSON、脱敏 JSON 和文本错误通道保持原有格式，
不改写损坏的规则或配置文件。

## 退出码

| code | 含义 |
|---:|---|
| `0` | 命令按契约完成 |
| `1` | 有 blocking issue/outcome 失败，或能力明确 unavailable |
| `2` | 参数、规则、路径、选择或配置错误 |
| `130` | 用户中断 |

`ignore add/remove` 是幂等配置操作：目标已被覆盖或不存在时仍返回 `0`，JSON 中
`changed=false`。

## 开发检查

从仓库根运行：

```bash
make check
make test-focused TEST_PATTERN=test_agent_identifiers.py
```

本地不要求开发依赖，只运行轻量检查和受影响测试；GitHub Actions 负责 `make ci-check`、
`make package`、`make release-check` 和 wheel 安装验证。完整目标仍可按需在本机复现。

wheel 只含运行时包；sdist 有意包含 tests、preview、TUI 资产生成器、release checker、
`openclean_cli.py`、README 和 TODO。剩余工作见 [TODO.md](TODO.md)。检查结果以当前
checkout 的轻量结果和 exact-head CI 分别报告。

本包随仓库以 [GNU GPL v3](LICENSE) 许可。GitHub Release 是唯一正式发布渠道；
实际 tag、附件与未发布修复分别核对发行页和 CHANGELOG，不以源码版本号推断安装包已更新。
项目不通过 PyPI、Homebrew 或其他包管理器分发。

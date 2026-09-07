# implementation

[仓库 README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md)

`openclean` 的 Python 实现层。运行时只使用标准库，要求 macOS 和 Python 3.11+；
当前 CI 只验证 Python 3.11。许可证为随包 [GPL-3.0](LICENSE)。用户安装与安全默认见
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

## Agent Runtime 命令面（附加）

与上面的经典五域命令**并存**，面向 AI Agent 的“探测 → 审阅 → 授权执行”闭环。P0 仅
分发 `codex` 策略包；其余 `TARGET`（`qoder`/`workbuddy`/`macos-system`/…）为已规划 pack，
`inspect` 会 fail-closed 返回 `pack_not_found`（exit 1），不伪装完成。

只读：

```bash
openclean inspect codex --json          # 探测并固化 Run/Finding 到本机 Run Store
openclean inspect all --json            # 已加载 pack 的并集
openclean show --run RUN_ID --finding FINDING_ID --json   # 单个 Finding 完整证据
openclean strategy list --json          # 已安装策略包与 hash
openclean strategy show --id codex.marketplace.old-staging --json
openclean strategy verify --json
```

写操作（Finding 驱动，仅 `trusted` 策略可执行）：

```text
openclean clean --run RUN_ID --finding FINDING_ID                          # 预览（不写）
openclean clean --run RUN_ID --finding FINDING_ID --include-confirm --yes   # 执行到同卷 Trash
```

契约要点：

- `inspect` 产出的 `run_id`/`finding_id` 固化在本机私有 Run Store
  （`$XDG_STATE_HOME/openclean/runs`，回退 `~/.local/state/...`；目录 `0700`、文件 `0600`，
  默认 24h TTL）。过期 Run 拒绝复用且**绝不自动重扫**。
- `clean --run --finding` 只接受 Run Store 里的 `finding_id`，不接受任意路径；选择即授权边界。
- `can_execute` 是八项合取（用户 `--yes`、策略 `trusted`、动作 supported、Run 未过期、
  pack hash 匹配、目标 identity 匹配、当前 protect/ignore 允许、实时 guard 通过）；任一不成立
  该项 `can_execute=false` 并带 `block_reasons`，整批 all-or-nothing。
- 聚合根（`filesystem_subset`，如 `.staging` 汇总）永不作为动作目标；trusted 策略用逐目标枚举。
- `--redact-paths` 同时脱敏 `run_id`/`finding_id`（`run:redacted`/`finding:redacted`），输出
  `redaction.selection_replayable=false`，不能回放执行。
- JSON envelope：`inspect` 返回 `run_id`/`requested_target`/`totals`/`findings[]`/`issues[]`/
  `redaction`；`show` 返回单个 Finding（`target`/`measurement`/`assessment`/`evidence`/
  `recommendation`）；`clean` 返回 `mode`/`executed`/`plan.items[]`/`outcome`。
- 退出码沿用经典语义：`1` 含 `pack_not_found` 与选中项全部被阻断；`2` 含 `finding_not_in_run`。

对象模型、Run Store、执行不变量与策略生命周期的完整契约见
[specs/agent-runtime/](../specs/agent-runtime/_index.md)。

## 选择与执行

- 没有 `--yes` 时，`clean`/`purge`/`analyze` 即使带选择参数也只预览。
- 没有 `--select` 时，普通 `safe` 扫描点可默认预选；扫描点可以独立关闭默认选择，AI 域
  即使是可重建缓存也统一默认不选。`--all`/`--include-confirm`/`--include-critical`
  分别扩展对应批量层级。
- 一旦指定 `--select`，选择集从空开始，不继承默认预选；confirm/critical 精确目标仍需
  对应 `--include-confirm`/`--include-critical` 作为风险授权，但不会顺带选择同等级其他项。
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

连接 TTY 时，`clean`、`purge` 和 `analyze` 默认进入 curses 界面。JSON、管道、
`--no-interactive` 和任何参数化选择 flag 不打开 TUI。TUI 的选择只是选择；实际执行仍
要求启动命令带 `--yes`，并在汇总页再次按 `Y`。快捷键见
[docs/PREVIEW.md](../docs/PREVIEW.md)。

## JSON schema v2

成功结果包含 `schema_version=2`：

- `potential_bytes`：发现的物理占用；
- `reclaimable_bytes`：清理域中 `actionable=true` 候选占用；`analyze` 顶层及每个 entry
  固定为 `0`，因为空间占用本身不等于垃圾；
- `requires_privilege_bytes`：当前需要特权 helper 的占用；
- `unsupported_bytes`：既不可执行又非特权候选的占用。

`scan` 额外返回 `command`、`mode` 和 `requested_domains`。`analyze` 返回 `top`、
`truncated`、`entry_count_total`、`entry_count_returned`。路径候选的
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
make package
make release-check
```

wheel 只含运行时包；sdist 有意包含 tests、preview、TUI 资产生成器、release checker、
`openclean_cli.py`、README 和 TODO。剩余工作见 [TODO.md](TODO.md)。检查结果以当前
checkout 的 `make check` 为准。

本包随仓库以 [GNU GPL v3](LICENSE) 许可。GitHub Release 是唯一计划的正式发布渠道，当前尚未创建
Release；项目不通过 PyPI、Homebrew 或其他包管理器分发。

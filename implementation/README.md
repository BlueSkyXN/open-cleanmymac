# implementation

[仓库 README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md)

这是 `openclean` 的独立 Python 实现层。运行时只使用标准库，要求 macOS 和 Python
3.11+；当前 CI 只验证 Python 3.11。许可证为随包提供的
[GPL-3.0](LICENSE)。净室边界、功能矩阵和用户安全说明见
[仓库 README](../README.md)。

## 安装与入口

```bash
# 从 Git checkout 根安装
python3 -m venv .venv
.venv/bin/python -m pip install ./implementation
.venv/bin/openclean --version
.venv/bin/python -m openclean --help

# 从 implementation/ 或解压后的 sdist 根安装
python3 -m pip install .
```

源码 checkout 也可直接运行：

```bash
cd implementation
PYTHONPATH=. python3 -m openclean --version
PYTHONPATH=. python3 openclean_cli.py --version
```

`openclean_cli.py` 是 checkout/sdist 便捷入口；wheel 的正式入口是 console script 和
`python -m openclean`。低于 Python 3.11 的源码运行时仍可查看 help/version；业务命令会
在进入扫描前返回 `unsupported_python`，不会输出内部 traceback。

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

前四条会修改文件；清空 Trash 和 Docker prune 是永久操作。最后三类会写本地配置或联网
安装规则。验证写路径应运行 `make preview`，它只使用 `TemporaryDirectory`。

## 选择与执行

- 没有 `--yes` 时，`clean`/`purge`/`analyze` 即使带选择参数也只预览。
- 没有 `--select` 时，`safe` 可默认预选，`--all`/`--include-confirm`/
  `--include-critical` 分别扩展对应批量层级。
- 一旦指定 `--select`，选择集从空开始，不继承默认预选；confirm/critical 精确目标仍需
  对应 `--include-confirm`/`--include-critical` 作为风险授权，但不会顺带选择同等级其他项。
- `--select` 与 `--all` 语义冲突，CLI 在扫描前返回 exit 2。
- `requires_explicit_selection=true` 的环境来源项和 Docker prune 不会被默认选择、`--all`
  或 tier 批量参数选中，必须使用完整路径或 identifier 精确选择。
- `--force --yes` 只执行默认预选项，并拒绝与任何扩大选择参数组合。
- 普通用户态路径移动到同卷 Trash；`clean trash` 永久删除内容但保留 Trash 根目录。
- Docker Build Cache/Images/Containers 分别映射固定官方 prune 命令且均需 identifier
  精确选择；Local Volumes 始终不可执行。
- 特权系统项、ApplicationLanguages、云保护项和不支持资源无法被参数强制解锁。

## TUI

连接 TTY 时，`clean`、`purge` 和 `analyze` 默认进入 curses 界面。JSON、管道、
`--no-interactive` 和任何参数化选择 flag 不打开 TUI。`analyze --line-interactive`
提供只读行式导航。

TUI 的选择只是选择；实际执行仍要求启动命令带 `--yes`，并在汇总页再次按 `Y`。
快捷键见 [docs/PREVIEW.md](../docs/PREVIEW.md)。

## JSON schema v2

成功结果包含 `schema_version=2`。通用容量字段：

- `potential_bytes`：发现的物理占用；
- `reclaimable_bytes`：`actionable=true` 候选占用；
- `requires_privilege_bytes`：当前需要特权 helper 的占用；
- `unsupported_bytes`：既不可执行又非特权候选的占用。

`scan` 额外返回 `command`、`mode` 和 `requested_domains`。`analyze` 返回 `top`、
`truncated`、`entry_count_total`、`entry_count_returned`。`optimize --json` 返回：

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
序列化阶段把同一文档内路径映射成稳定 opaque ref，并处理 message/note 和解析前错误；
输出会声明 `selection_replayable=false`，不能直接用于后续 `--select`。`complete=true`
只表示没有 blocking issue；仍应检查 `issues` 中的安全跳过和动态来源提示。

## 自建规则

默认规则路径是 `~/.config/openclean/rules.json`；`--rules FILE` 使用显式单文件。
规范格式只支持 JSON，不支持 YAML：

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
  "created_at": "2026-07-30T12:00:00+08:00",
  "signature_algorithm": "openssl-dgst-sha256",
  "rules": {"schema_version": 1},
  "signature": "<base64 signature>"
}
```

客户端限制 2 MiB、只接受 HTTPS、拒绝 URL 凭据，使用用户提供的 PEM 公钥调用系统
OpenSSL 验证 SHA-256 签名。成功后钉住公钥指纹和递增 sequence；稳定的 `0600`
`<destination>.lock` 把 sequence/key 重判与 `os.replace` 安装放进同一个跨进程临界区，
临时文件同时使用 `fsync`。`os.replace` 是安装提交边界，后置目录同步和 fd 清理只做
best effort，不会把已经安装的 sequence 报成失败。托管 `knowledge.json` 与用户
`rules.json` 分层合并，远程更新不覆盖用户 ignore。

## 路径安全

- 不跟随候选或 ancestor symlink；
- 环境变量缓存根只允许位于 `~/Library/Caches` 或 `~/.cache`，并强制精确选择；
- 目录大小按物理块计量，硬链接 device/inode 去重；Darwin `SF_DATALESS` 与 zero-block
  启发式在目录枚举前阻止 dataless/疑似云占位，它们不计回收量；
- 执行前批量复核保护规则、device/inode、owner、mount、云和运行中进程；
- 后代目录以 no-follow fd 打开，并在 `scandir(fd)` 前重新 `fstat` 类型、owner、device
  和 dataless 状态；
- 普通移动逐组件打开 no-follow 目录 fd，并使用 Darwin
  `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)` 原子拒绝覆盖；
- Trash 目标目录必须属于当前用户并使用私有权限；新建目录在可信父目录 fd 下相对创建，
  no-follow 打开并绑定 identity 后才在 fd 上设置权限；
- Trash rename 后的源/目标目录 fd 独立关闭；后置 close 失败保留已移动事实并返回
  `partial`；
- Trash 永久清空只删除最终审计快照；审计后新增项保留，部分删除返回 `partial`；
- Docker 候选携带不进入 JSON 的 scan-time resource binding；执行前重新解析 CLI realpath
  并复核明确 context/host、endpoint TLS mode 和 Engine ID，CLI realpath 变化或即时复核
  发现不一致时拒绝执行；
- Docker prune 启动后的 timeout/非零退出返回副作用未知的 `partial`；
- 任一批量预检失败时整个批次不启动。

详见 [架构说明](../docs/ARCHITECTURE.md) 和 [安全政策](../SECURITY.md)。

## 退出码

| code | 含义 |
|---:|---|
| `0` | 命令按契约完成 |
| `1` | 有 blocking issue/outcome 失败，或能力明确 unavailable |
| `2` | 参数、规则、路径、选择或配置错误 |
| `130` | 用户中断 |

`ignore add/remove` 是幂等配置操作：目标已被覆盖或不存在时仍返回 `0`，并在 JSON 中
给出 `changed=false`。

## 开发检查

从仓库根运行：

```bash
make lint
make test
make preview
make docs-assets
make check
make package
make release-check
```

直接命令：

```bash
cd implementation
ruff check openclean tests scripts
python3 -W error -m py_compile openclean/*.py tests/*.py scripts/*.py
PYTHONPATH=. python3 -W error -m unittest discover -s tests -q
PYTHONPATH=. python3 scripts/preview_all.py --json
PYTHONPATH=. python3 scripts/capture_tui_assets.py --check
python3 -m build --no-isolation
python3 scripts/check_release_artifacts.py --json
```

wheel 只含运行时包；sdist 有意包含 tests、preview、TUI 资产生成器、release checker、
`openclean_cli.py`、README 和 TODO，以便源码归档自验证。两种归档都必须排除
`analysis/`、`local/`、缓存、`.DS_Store` 和敏感材料。

剩余工作见 [TODO.md](TODO.md)。

当前本地验证基线为 319 个 `unittest` 和 19/19 个隔离预览场景；最终结果仍以当前
checkout 的 `make check` 输出为准。

## 许可证

本包随仓库以 [GNU GPL v3](LICENSE) 许可。PyPI / Homebrew / GitHub Release 渠道尚未开通。

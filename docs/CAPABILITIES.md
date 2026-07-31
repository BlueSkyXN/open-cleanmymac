# CLI 能力与边界地图

[README](../README.md) · [功能预览](PREVIEW.md) ·
[架构](ARCHITECTURE.md) · [安全](../SECURITY.md) ·
[规格索引](../specs/_index.md) · [实现说明](../implementation/README.md)

本文把“材料里出现过的能力”和“当前 `openclean` CLI 承诺交付的能力”分开。内部组件或
Desktop 背景事实不会自动变成 CLI backlog；高风险能力可以有意保持只读或 fail-closed。

## 状态定义

| 状态 | 含义 |
|---|---|
| `available` | 当前代码可执行，并有自动化验证 |
| `read-only` | 可发现或报告，没有写路径 |
| `guarded-unavailable` | 命令面存在，但明确拒绝并返回非零退出码 |
| `external-prerequisite` | 仍需签名、正式服务或真实外部环境，不能只靠 Python 代码完成 |
| `not-implemented` | 规格中有背景或候选设计，但当前没有可调用实现 |
| `out-of-scope` | Desktop/后台能力，不属于当前 CLI 范围 |

`origin_kind` 仅用于解释来源范围：`public-cli`、`internal`、`desktop-background` 或
`project-extension`，不表示复用参考实现代码或私有数据。

## 用户能力矩阵

| capability | command | status | boundary / exclusion |
|---|---|---|---|
| 五域聚合扫描 | `scan` | `available` | `scan` 始终只读；扫描点是保守公开子集 |
| 分类清理 | `clean junk\|dev\|ai` | `available` | 默认预览；`--yes` 只执行当前已审阅选择 |
| Trash 审阅与清空 | `clean trash` | `available` | confirm；内容永久删除，根目录保留 |
| 项目产物清理 | `purge [path]` | `available` | 只处理公开产物字典；普通项移到同卷 Trash |
| 空间分析 | `analyze [path]` | `available` | 不自动删除 Time Machine 快照 |
| 精确参数选择 | `clean/purge --select` | `available` | 从空选择集开始；tier flag 只作风险 gate；拒绝 `--select + --all` |
| 文本/JSON/TUI 输出 | 全局 | `available` | JSON 含绝对路径；分享前需脱敏 |
| 用户 ignore | `ignore list\|add\|remove` | `available` | 写入用户 `0600` JSON；不内置私有规则 |
| CLI 配置 | `config` | `available` | analytics 仅是偏好；当前没有遥测上传 |
| 签名托管知识库客户端 | `config --update-knowledge` | `external-prerequisite` | 客户端已完成；项目尚无正式 URL、公钥和发布流程 |
| Docker 容量与固定 prune | `scan/clean dev` | `external-prerequisite` | 本地代码可用；真实 daemon 三条 prune 尚未单独验收；Volumes 永远拒绝 |
| 失效启动项 | `scan/clean junk` | `available` | 仅可确认失效的用户项可执行；系统项需要 helper |
| ApplicationLanguages | `scan/clean junk` | `read-only` | 固定 `critical + actionable=false`；不修改签名 app |
| Time Machine 本地快照提示 | `analyze` | `read-only` | 只显示名称/数量；不宣称精确大小，不删除 |
| dataless/疑似占位保护 | 所有文件扫描/执行 | `available` | 不保证识别全部已 materialized cloud-synced 文件；真实 provider fixture 待验收 |
| RAM/purgeable 能力状态 | `optimize ram\|purgeable` | `guarded-unavailable` | 没有已验证、安全、公开的等价执行接口 |
| 特权系统清理 | 候选只读可见 | `external-prerequisite` | 需 native host/helper、SMAppService、签名、entitlements、安装验收 |
| universal binary thinning | 无 | `not-implemented` | 当前不实现；修改签名/兼容性风险高 |
| Desktop GUI、菜单栏、后台 agent | 无 | `out-of-scope` | 当前产品只开发 CLI；curses TUI 属于 CLI |
| 应用卸载、恶意软件扫描等 Desktop 功能 | 无 | `out-of-scope` | 不属于当前公开 CLI 对齐目标 |

## 实现与验证索引

| capability | origin_kind | implementation | validation |
|---|---|---|---|
| 五域聚合扫描 | `public-cli` | `cli.py`、`engine.py`、`scanpoints.py` | 单测 + `scan-all-domains` preview |
| 分类清理 | `public-cli` | `cleanup.py`、`tui.py` | 选择/执行单测 + 临时 Trash preview |
| Trash 审阅与清空 | `public-cli` | `macos.py`、`cleanup.py` | 两个合成 Trash 根的无扩面执行 preview |
| 项目产物清理 | `public-cli` | `engine.py`、`cleanup.py` | 项目发现/年龄/嵌套/执行测试 |
| 空间分析 | `public-cli` | `analyzer.py`、`space_tui.py`、`navigator.py` | 单层排序/TUI/精确执行测试 |
| 精确参数选择 | `project-extension` | `cleanup.py`、`cli.py` | no-collateral selection 单测 + preview |
| 文本/JSON/TUI 输出 | `public-cli` | `cli.py`、`tui.py`、`space_tui.py` | schema v2、错误 envelope、状态机及 SVG 资产测试 |
| 用户 ignore | `public-cli` | `knowledge_base.py` | lifecycle、权限、规范路径回执、原子写测试 |
| CLI 配置 | `public-cli` | `config.py` | analytics lifecycle/readback |
| 签名托管知识库客户端 | `project-extension` | `knowledge_update.py` | HTTPS/验签/防回滚/原子安装测试 |
| Docker 容量与固定 prune | `project-extension` | `docker.py` | parser/白名单/报告隔离测试 |
| 失效启动项 | `internal` | `startup_items.py` | plist 解析、重判、用户态执行测试 |
| ApplicationLanguages | `internal` | `application_languages.py` | metadata/语言/签名风险测试 |
| Time Machine 本地快照提示 | `internal` | `macos.py`、`analyzer.py` | `tmutil` parser 与根卷分支测试 |
| dataless/疑似占位保护 | `project-extension` | `models.py`、扫描器、`cleanup.py` | `SF_DATALESS`、zero-block、禁止枚举/最终复核测试 |
| RAM/purgeable 能力状态 | `public-cli` | `cli.py` | guard JSON/text/退出码测试 |
| 特权系统清理 | `internal` | 仅模型与 IPC 规格 | fail-closed 测试 |
| universal binary thinning | `internal` | 仅规格 | 无写路径 |
| Desktop GUI、菜单栏、后台 agent | `desktop-background` | 无 | 不适用 |
| 应用卸载、恶意软件扫描等 Desktop 功能 | `desktop-background` | 无 | 不适用 |

## 验证边界

当前自动化基线覆盖 278 个 `unittest` 和 19 个 `TemporaryDirectory` 隔离预览场景。
这能证明当前 checkout 的本地逻辑、归档和合成写路径，但不能替代以下验收：

- 真实 iCloud Drive/第三方 File Provider 的 dataless 状态保持；
- 用户明确授权的 Docker 测试 daemon before/after；
- native XPC helper 的签名、安装、升级与回滚；
- 项目自有托管知识库服务、公钥和灾备流程；
- 不同 macOS/Python 版本的兼容矩阵。

这些外部边界不会在 preview 中伪造为成功。对应状态和原因会继续以结构化
`guarded-unavailable` 或 `external-prerequisite` 展示。

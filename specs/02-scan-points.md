# 02 · 扫描点字典（核心规格）

> 来源：`analysis/raw/*.txt`（从二进制 strings 提取的**事实**）。
> 这是 CleanMyMac "判定什么该清"的扫描点全集。属功能性事实，可据此独立实现。
> 说明：应用级精细规则由知识库动态提供（见 03），此处为**硬编码 + 类别框架**。

图例：`~`=用户主目录；`/`=系统根（需相应权限）；相对名=在项目/工具目录内匹配。

---

## 域 A · 系统垃圾（System Junk）

引擎以**类别标识符**组织系统垃圾，共 26 类（事实清单）：

### 缓存类（Caches）
| 类别标识 | 说明 |
|---|---|
| UserCache / SystemCache / allUsersCaches | 用户级 / 系统级 / 全局缓存（`~/Library/Caches`、`/Library/Caches`） |
| SandboxContainersCache | 沙盒容器内缓存（`~/Library/Containers/*/Data/Library/Caches`） |
| ElectronAppsCache | Electron 应用缓存（GPUCache/Code Cache/CacheStorage 等） |
| DropboxCache / SpotifyCache | 特定第三方应用缓存（知识库驱动，按应用指纹定位） |
| GradleCache | `~/.gradle/caches`（含 `build-cache-*`） |
| AppStoreDownloadsCache | App Store 下载残留 |
| CoreSymbolicationdCache | 符号化守护进程缓存（`/System/Library/Caches/com.apple.coresymbolicationd` 等） |
| DarwinUserCache | `darwinCacheDirectory`（getconf DARWIN_USER_CACHE_DIR 下） |
| DocumentationCache / HardcodedPathCache | 文档缓存 / 硬编码路径缓存 |
| XcodeCoreSimulatorCaches / XcodeModuleCaches / XcodeDeviceLogs | Xcode 模拟器缓存、模块缓存、设备日志 |

本项目对 `~/Library/Caches` 按一级子项展示。命中公开维护的应用归属规则时，即使候选从
通用 UserCache 入口发现，也必须应用对应的运行进程保护：运行中或无法读取进程状态时仍
报告占用，但 `actionable=false`，退出应用后需重新扫描。未命中归属规则的普通缓存不伪造
进程关联。

已知 updater 根额外进行版本状态判定，包括 Codex Sparkle、WorkBuddy BundleMigration、
Qoder ShipIt/Qoder updater、Lark update 和 TRAE updater。`staged > installed`、已安装应用
缺失或版本不可确认时强制保护；同版/旧版残留统一为 critical、要求精确选择，并在执行前
重判。只读取 app `Info.plist` 或 ZIP 顶层 bundle metadata，不执行暂存程序。
`DARWIN_USER_TEMP_DIR` 中命名为 Qoder ShipIt 动态根的完整 app 副本也会复用版本判定，
但该类临时副本固定只读、不可执行。

### 日志类（Logs）
| 类别标识 | 说明 |
|---|---|
| UserLogs / SystemLogs / LocalLogs | `~/Library/Logs`、`/Library/Logs`、应用本地日志 |
| AppleLogs | Apple 系统日志（aslf/DiagnosticReports） |
| DiagnosticLogs | `~/Library/Logs/DiagnosticReports`、`/Library/Logs/DiagnosticReports`（`.ips`/`.crash`/`.panic`/`.diag`） |
| SandboxLogs / QuarantineLogs | 沙盒日志；`~/Library/Preferences/com.apple.LaunchServices.QuarantineEvents*` |

本项目额外对 WorkBuddy logs/traces、Lark SDK logs、Shadowrocket logs 和 TRAE logs 做
retention-aware 只读诊断：只读取目录项、物理块、mtime、文件数量、进程和打开句柄状态，
分别报告 7/14/30 天容量；不读取正文，不把任一阈值设为默认删除策略，也不提供批量执行器。

### 其它系统垃圾
- **BrokenStartupItems**：失效的启动项（指向已不存在二进制的 LaunchAgents/Daemons）。
- **SystemMigration**：`/Library/SystemMigration/History`（系统迁移残留）。
- **dyld 缓存**：`/private/var/db/dyld`（QuarantineRoot 下）。
- **ApplicationLanguages**：应用内多余语言包（`.lproj`，按用户语言判断）。本项目只做
  保守只读审计；修改签名 app 的删除执行不在当前范围。
- **Universal binary 瘦身**：识别 universal 二进制中非本机架构（x86_64/arm64）切片。

### 应用级缓存定位样例（事实，知识库命中）

```
~/Library/Application Support/Adobe/Common/Media Cache Files        (Adobe 媒体缓存)
~/Library/Application Support/Spotify/PersistentCache/Storage       (Spotify 缓存)
~/Library/Application Support/Telegram Desktop/tdata/user_data      (Telegram)
~/Library/Containers/com.apple.Photos/.../Photos Desktop/0          (照片桌面缓存)
~/Library/Containers/com.tencent.*(微信/腾讯视频/QQ音乐) 各自缓存
~/Library/Containers/org.telegram.desktop/.../tdata/user_data
~/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram
```

> 净室说明：上述为二进制中提取的**事实**；具体的"厂商→路径"全量明细由实现侧
> 按 macOS 通用约定**独立采集**，不复用 MacPaw 的应用指纹库（见 specs/03 红线）。

---

## 域 B · 开发工具垃圾（Developer Junk）

精确到工具的**缓存目录**（事实，用户主目录相对）：

```
Library/Caches/pip                        Library/Caches/Yarn
Library/Caches/CocoaPods                  Library/Caches/go-build
Library/Caches/deno                       Library/Caches/mise
Library/Caches/Homebrew                   Library/pnpm/store
Library/Caches/com.microsoft.VSCode/Cache
Library/Logs/JetBrains
.cargo/registry/cache                     .gradle/caches/build-cache-1
.cargo/git/checkouts  .cargo/git/db        .rustup/downloads
go/pkg/mod                                  .npm/_npx  .npm/_prebuilds  .npm/_logs
```

VS Code 缓存子项（`Library/Application Support/Code/` 下）：
`CachedData`、`CachedExtensionVSIXs`、`GPUCache`、`WebStorage`、
`DawnGraphiteCache`、`DawnWebGPUCache`、`Service Worker/CacheStorage`

---

## 域 C · 项目构建产物（Project Artifacts）

**在代码项目目录内匹配**的构建/依赖产物目录名（事实，约 40 条的高价值字典）：

```
前端/JS:   node_modules  .next  .nuxt  .output  .turbo  .vite  .vitepress
          .angular  .astro  .svelte-kit  .parcel-cache  .rollup.cache
          .swc  .wireit  .expo  .docusaurus
Python:    .venv  .eggs  .mypy_cache  .pytest_cache  .pyre  .pytype
          .ruff_cache  .tox  .nox  .pdm-build  __pycache__
Swift/iOS: .build  .swiftpm  DerivedData  (Library/Developer/Xcode/DerivedData)
JVM/其它:  .gradle  .kotlin  .bloop  .metals  .stack-work  .ccls-cache  .cpcache  .cxx
Infra:     .terraform  .dart_tool
杂项:      .Trash(项目内)  .git(不删，仅测大小)  meson.build  *.csproj/fsproj/vbproj(定位)
```

行为契约：进入候选项目根 → 命中上表目录 → 计大小 → 标记为可安全重建（`node_modules`/`.venv`/构建缓存可重新生成）。

---

## 域 D · AI 工具数据（AI Tool Scanning）

**新兴扫描域**：清理各 AI 编码/桌面工具的缓存、日志、临时与浏览器配置缓存。
（事实清单，用户主目录相对；`AICacheSource`/`AIJunkIgnoreRules` 驱动）

```
Claude:    .claude/cache  .claude/debug  .claude/telemetry
          .claude/stats-cache.json  .claude/mcp-needs-auth-cache.json
          .claude/plugins/install-counts-cache.json
Codex:     .codex/cache/codex_apps_server_info  .codex/cache/codex_apps_tools
           .codex/tmp  .codex/.tmp
Gemini:    .gemini/tmp  .gemini/antigravity-browser-profile/**(Cache/Code Cache/GPUCache/
          DawnGraphiteCache/DawnWebGPUCache/Service Worker·CacheStorage/GraphiteDawnCache/
          component_crx_cache/extensions_crx_cache)
OpenCode:  .cache/opencode  .local/share/opencode/log
Cursor:    .local/share/cursor-agent
chrome-devtools-mcp: .cache/chrome-devtools-mcp/chrome-profile/{,Default/}**
                     (兼容旧根布局和 Chromium Default profile；同上浏览器缓存子项)
杂项:      *.log
```

Codex `~/.codex/logs_2.sqlite` 另使用 `mode=ro&immutable=1` 读取 page size/count/freelist，
仅报告内部空闲页、比例和 WAL/SHM/journal/句柄状态。该结果固定不可执行，不删除数据库，
也不自动运行 `VACUUM`。

关联 bundleID（事实，用于定位容器）：`com.anthropic.claudefordesktop`、
`com.openai.codex`、`com.openai.sky.CUAService`、`com.google.antigravity`、`com.todesktop.230313mzl4w4u92`

---

## 域 E · 空间透镜（Space Lens）

磁盘占用分析（非删除导向），扫描根与特殊处理：

```
扫描根:  /System/Volumes/Data/  /home  /net  /private/var/vm
跳过/特殊: /Library/Reminders
特殊项:  /private/var/vm（swap/睡眠镜像，标注但默认不删）
能力:    卷备份大小缓存（VolumeBackupSizeCacher，Time Machine 本地快照感知）
```
行为契约：递归统计目录大小 → 大文件/大目录排序 → 区分“真实占用”与“可清除/可 purge
空间”。本项目的 `analyze` 只将一级候选视为占用项：每项同时固定自身 `st_dev` 与
`statvfs().f_fsid`，从而识别 macOS 上 device 相同但文件系统挂载不同的 APFS root/Data
边界；跳过其它挂载点并报告 `cross_device_paths`。顶层和 entry 的
`reclaimable_bytes` 固定为 `0`。候选统一为 critical、要求精确选择；跨卷跳过项存在时
该候选不可执行。只读 `lstat` / `scandir` 遇到 `EINTR` 时透明重试。

扫描/清理 JSON 按 scan-time device 输出 `volumes`，使系统盘与外置卷的 potential、
reclaimable、preselected、privileged 和 unsupported 容量分别可见。device ID 不是持久 ID。

---

## 域 F · 废纸篓（Trash Junk）

```
/Volumes/<每个挂载卷>/.Trashes/<uid>     (外接卷废纸篓)
~/.Trash                                (用户废纸篓)
项目内: Documents/.Trash/  <proj>/.Trash
```
行为契约：枚举所有挂载卷的 `.Trashes` → 计大小 → 清空（保留用户确认）。

---

## 域 G · 应用与残留（Applications / Leftovers）

> 本节是 Desktop/关联模块的背景功能事实，**不属于当前公开 CLI 对齐范围**。本项目不据此
> 实现应用卸载、跨应用残留删除或 helper 移除；只能在后续独立立项和公开规格确认后评估。

应用枚举与关联残留定位（事实）：

```
应用位置:  /Applications  /System/Applications  ~/Applications
残留/关联:
  /Library/LaunchAgents  /Library/LaunchDaemons  ~/Library/LaunchAgents
  /Library/PrivilegedHelperTools  /Library/SystemExtensions
  /Library/ColorPickers  /Library/PreferencePanes  /Library/Widgets
  ~/Library/Application Support/<app>  ~/Library/Containers/<bundleID>/Data
  /Library/Application Support/Setapp/LaunchAgents/Setapp.app
虚拟机镜像识别(不删，仅提示): *.pvm  *.vmwarevm  *.backupdb
元数据: .com.apple.containermanagerd.metadata.plist
```
参考行为：以 bundleID 为键，聚合"应用本体 + 容器 + 支持文件 + 启动项 + 帮助器"。
这不是 `openclean` 当前必须实现的行为契约。

---

## 跨域通用规则

- **忽略/保护**：任何项命中知识库的忽略规则（正则/路径）或受保护应用，则标记不可删（见 03）。
- **大小统计**：目录递归求和，硬链接去重，符号链接不跟随到外部（防重复计数）。
- **安全等级**：每项标注 安全可删 / 需确认 / 系统关键(不删)。

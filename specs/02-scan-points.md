# 02 · 扫描点字典（核心规格）

> 来源：从参考对象公开字符串提取的功能性事实。
> 这是 CleanMyMac "判定什么该清"的扫描点全集，可据此独立实现。
> 应用级精细规则由知识库动态提供（见 03），此处为硬编码 + 类别框架。
> 本项目实际交付的保守子集、只读诊断和有意不实现项见
> [docs/CAPABILITIES.md](../docs/CAPABILITIES.md) 与 [_index.md](_index.md)。

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

### 日志类（Logs）
| 类别标识 | 说明 |
|---|---|
| UserLogs / SystemLogs / LocalLogs | `~/Library/Logs`、`/Library/Logs`、应用本地日志 |
| AppleLogs | Apple 系统日志（aslf/DiagnosticReports） |
| DiagnosticLogs | `~/Library/Logs/DiagnosticReports`、`/Library/Logs/DiagnosticReports`（`.ips`/`.crash`/`.panic`/`.diag`） |
| SandboxLogs / QuarantineLogs | 沙盒日志；`~/Library/Preferences/com.apple.LaunchServices.QuarantineEvents*` |

### 其它系统垃圾
- **BrokenStartupItems**：失效的启动项（指向已不存在二进制的 LaunchAgents/Daemons）。
- **SystemMigration**：`/Library/SystemMigration/History`（系统迁移残留）。
- **dyld 缓存**：`/private/var/db/dyld`（QuarantineRoot 下）。
- **ApplicationLanguages**：应用内多余语言包（`.lproj`，按用户语言判断）。
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

> 净室说明：上述为功能性路径事实；具体的"厂商→路径"全量明细由实现侧按 macOS
> 通用约定独立采集，不复用参考软件的应用指纹库（见 specs/03 红线）。

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
Gemini:    .gemini/tmp  .gemini/antigravity-browser-profile/**(Cache/Code Cache/GPUCache/
          DawnGraphiteCache/DawnWebGPUCache/Service Worker·CacheStorage/GraphiteDawnCache/
          component_crx_cache/extensions_crx_cache)
OpenCode:  .cache/opencode  .local/share/opencode/log
Cursor:    .local/share/cursor-agent
chrome-devtools-mcp: .cache/chrome-devtools-mcp/chrome-profile/{,Default/}**
                     (兼容旧根布局和 Chromium Default profile；同上浏览器缓存子项)
杂项:      *.log
```

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
空间”。

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

> 本节是 Desktop/关联模块的背景功能事实，**不属于当前公开 CLI 对齐范围**。

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

"""扫描点数据表 · 独立实现（依据 specs/02-scan-points.md）。

本表由本项目依据"功能性事实"独立整理与表达，
数据组织、注释、结构均为本项目原创，与 MacPaw 产品的内部表达无关。
路径为 macOS 上各类缓存/构建产物的公开常识位置。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanPoint:
    """一个扫描点：判定某类可清理项的位置。"""
    category: str          # 类别（如 "pip 缓存"）
    paths: tuple[str, ...]  # 候选路径（支持 ~ 与绝对路径；相对名用于项目内匹配）
    safety: str = "safe"    # safe | confirm | critical
    note: str = ""
    domain: str = ""
    env_paths: tuple[str, ...] = ()
    scanner: str | None = None
    running_process_markers: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    expand_children: bool = False
    child_globs: tuple[str, ...] = ()
    child_extensions: tuple[str, ...] = ()
    path_provider: str | None = None
    requires_privilege: bool = False

    def __post_init__(self) -> None:
        if (self.child_globs or self.child_extensions) and not self.expand_children:
            raise ValueError("child 过滤器只能用于 expand_children 扫描点")
        if any("**" in pattern for pattern in self.path_globs):
            raise ValueError("path_globs 不支持递归 **")
        if any(not extension.startswith(".") for extension in self.child_extensions):
            raise ValueError("child_extensions 必须使用带点扩展名")
        if self.scanner is not None and self.path_provider is not None:
            raise ValueError("scanner 与 path_provider 不能同时使用")


# ── 域 B · 开发工具缓存 ─────────────────────────────────────────────
DEVELOPER_JUNK: list[ScanPoint] = [
    ScanPoint("pip 缓存", ("~/Library/Caches/pip",)),
    ScanPoint(
        "Poetry 缓存",
        ("~/Library/Caches/pypoetry",),
        env_paths=("POETRY_CACHE_DIR",),
    ),
    ScanPoint("uv 缓存", ("~/.cache/uv",), env_paths=("UV_CACHE_DIR",)),
    ScanPoint("Yarn 缓存", ("~/Library/Caches/Yarn",)),
    ScanPoint("pnpm store", ("~/Library/pnpm/store",)),
    ScanPoint("npm 缓存", ("~/.npm/_cacache",)),
    ScanPoint("CocoaPods 缓存", ("~/Library/Caches/CocoaPods",)),
    ScanPoint("Go 构建缓存", ("~/Library/Caches/go-build",)),
    ScanPoint("Deno 缓存", ("~/Library/Caches/deno",)),
    ScanPoint("Bun 缓存", ("~/.bun/install/cache",)),
    ScanPoint("mise 缓存", ("~/Library/Caches/mise",)),
    ScanPoint("Homebrew 缓存", ("~/Library/Caches/Homebrew",)),
    ScanPoint("Cargo 注册缓存", ("~/.cargo/registry/cache",)),
    ScanPoint("Rustup 下载", ("~/.rustup/downloads",)),
    ScanPoint("Gradle 构建缓存", ("~/.gradle/caches/build-cache-1",)),
    ScanPoint("Maven 本地仓库", ("~/.m2/repository",)),
    ScanPoint(
        "Docker 资源",
        (),
        "confirm",
        "通过 Docker CLI 读取 daemon 报告，不猜测 Docker Desktop 内部路径",
        scanner="docker",
    ),
    ScanPoint("JetBrains 日志", ("~/Library/Logs/JetBrains",), "confirm"),
    ScanPoint("VSCode 缓存", tuple(
        f"~/Library/Application Support/Code/{p}" for p in (
            "Cache", "CachedData", "CachedExtensions", "CachedExtensionVSIXs",
            "Code Cache", "GPUCache", "WebStorage", "DawnGraphiteCache",
            "DawnWebGPUCache", "Service Worker/CacheStorage")),
        running_process_markers=(
            "Visual Studio Code.app",
            "Code Helper",
        )),
]

# ── 域 A · 系统/用户缓存与日志（通用骨架，知识库前）──────────────────
SYSTEM_JUNK: list[ScanPoint] = [
    ScanPoint("用户缓存", ("~/Library/Caches",), "confirm",
              "按一级子项逐项审阅；通用缓存不默认选择",
              expand_children=True),
    ScanPoint(
        "系统缓存",
        ("/Library/Caches",),
        "confirm",
        "按一级子项只读报告；清理需要特权帮助器",
        expand_children=True,
        requires_privilege=True,
    ),
    ScanPoint(
        "沙盒容器缓存",
        (),
        "confirm",
        "按容器缓存的一级子项逐项审阅；通用缓存不默认选择",
        path_globs=("~/Library/Containers/*/Data/Library/Caches",),
        expand_children=True,
    ),
    ScanPoint(
        "Darwin 用户缓存",
        (),
        "confirm",
        "由 getconf 发现当前用户路径，并按一级子项逐项审阅",
        expand_children=True,
        path_provider="darwin-user-cache",
    ),
    ScanPoint(
        "用户日志",
        ("~/Library/Logs",),
        "confirm",
        "按一级子项逐项审阅；通用日志不默认选择",
        expand_children=True,
    ),
    ScanPoint(
        "系统日志",
        ("/Library/Logs",),
        "confirm",
        "按一级子项只读报告；清理需要特权帮助器",
        expand_children=True,
        requires_privilege=True,
    ),
    ScanPoint(
        "诊断报告",
        ("~/Library/Logs/DiagnosticReports",),
        expand_children=True,
        child_extensions=(".ips", ".crash", ".panic", ".diag", ".hang"),
    ),
    ScanPoint(
        "系统诊断报告",
        ("/Library/Logs/DiagnosticReports",),
        "confirm",
        "只读报告；清理需要特权帮助器",
        expand_children=True,
        child_extensions=(".ips", ".crash", ".panic", ".diag", ".hang"),
        requires_privilege=True,
    ),
    ScanPoint(
        "系统迁移残留",
        ("/Library/SystemMigration/History",),
        "confirm",
        expand_children=True,
        requires_privilege=True,
    ),
    ScanPoint(
        "Xcode DerivedData",
        ("~/Library/Developer/Xcode/DerivedData",),
        running_process_markers=("Xcode.app/Contents/MacOS/Xcode",),
        expand_children=True,
    ),
    ScanPoint(
        "Xcode 设备支持",
        ("~/Library/Developer/Xcode/iOS DeviceSupport",),
        "confirm",
        running_process_markers=("Xcode.app/Contents/MacOS/Xcode",),
        expand_children=True,
    ),
    ScanPoint(
        "Xcode 文档缓存",
        ("~/Library/Developer/Xcode/DocumentationCache",),
        running_process_markers=("Xcode.app/Contents/MacOS/Xcode",),
        expand_children=True,
    ),
    ScanPoint(
        "Xcode 设备日志",
        ("~/Library/Developer/Xcode/iOS Device Logs",),
        "confirm",
        running_process_markers=("Xcode.app/Contents/MacOS/Xcode",),
        expand_children=True,
    ),
    ScanPoint(
        "Xcode Archives",
        ("~/Library/Developer/Xcode/Archives",),
        "critical",
        "可能包含不可重建的发布归档，永不默认选择",
        running_process_markers=("Xcode.app/Contents/MacOS/Xcode",),
        expand_children=True,
    ),
    ScanPoint(
        "CoreSimulator 缓存",
        ("~/Library/Developer/CoreSimulator/Caches",),
        running_process_markers=("CoreSimulator", "Simulator.app"),
        expand_children=True,
    ),
    ScanPoint(
        "失效启动项",
        (
            "~/Library/LaunchAgents",
            "/Library/LaunchAgents",
            "/Library/LaunchDaemons",
        ),
        "confirm",
        "仅报告 Program 或 ProgramArguments[0] 可确认不存在的 launchd plist",
        scanner="broken-startup-items",
    ),
    ScanPoint(
        "应用语言包",
        ("/Applications", "/System/Applications", "~/Applications"),
        "critical",
        "按首选语言和应用开发语言保留；含非字符串资源时跳过",
        scanner="application-languages",
    ),
]

# ── 域 C · 项目构建产物（项目内匹配的目录名）─────────────────────────
PROJECT_ARTIFACT_NAMES: tuple[str, ...] = (
    # JS/前端
    "node_modules", ".next", ".nuxt", ".output", ".turbo", ".vite",
    ".vitepress", ".angular", ".astro", ".svelte-kit", ".parcel-cache",
    ".rollup.cache", ".swc", ".wireit", ".expo", ".docusaurus",
    # Python
    ".venv", "venv", ".eggs", ".mypy_cache", ".pytest_cache", ".pyre",
    ".pytype", ".ruff_cache", ".tox", ".nox", ".pdm-build", "__pycache__",
    "pycache",
    # Swift/iOS
    ".build", ".swiftpm", "DerivedData",
    # JVM/其它
    "target", ".gradle", ".kotlin", ".bloop", ".metals", ".stack-work",
    ".ccls-cache", ".cpcache", ".cxx",
    # Infra
    ".terraform", ".dart_tool",
    # CocoaPods / PHP / CMake 等公开 CLI 文档列出的依赖与构建目录
    "Pods", "vendor",
)

PROJECT_ARTIFACT_GLOBS: tuple[str, ...] = (
    "cmake-build-*",
)

PROJECT_MARKER_NAMES: tuple[str, ...] = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "Package.swift",
    "Podfile",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "CMakeLists.txt",
    "composer.json",
    "pubspec.yaml",
    "meson.build",
)

PROJECT_MARKER_GLOBS: tuple[str, ...] = (
    "*.csproj",
    "*.fsproj",
    "*.vbproj",
)

# CleanMyMac CLI v1.0.0 `purge` 的公开默认扫描位置。
DEFAULT_PROJECT_ROOT_NAMES: tuple[str, ...] = (
    "Projects",
    "Code",
    "dev",
    "GitHub",
    "Workspace",
)

# ── 域 D · AI 工具数据 ─────────────────────────────────────────────
AI_TOOL_JUNK: list[ScanPoint] = [
    ScanPoint(
        "Claude 缓存",
        (
            "~/.claude/cache",
            "~/.claude/debug",
            "~/.claude/telemetry",
            "~/.claude/stats-cache.json",
            "~/.claude/mcp-needs-auth-cache.json",
            "~/.claude/plugins/install-counts-cache.json",
        ),
        running_process_markers=("claude",),
    ),
    ScanPoint(
        "Codex 缓存",
        (
            "~/.codex/tmp",
            "~/.codex/cache/codex_apps_server_info",
            "~/.codex/cache/codex_apps_tools",
        ),
        running_process_markers=("codex",),
    ),
    ScanPoint(
        "Gemini 临时",
        (
            "~/.gemini/tmp",
            *(f"~/.gemini/antigravity-browser-profile/{path}" for path in (
                "Cache",
                "Code Cache",
                "GPUCache",
                "DawnGraphiteCache",
                "DawnWebGPUCache",
                "Service Worker/CacheStorage",
                "GraphiteDawnCache",
                "component_crx_cache",
                "extensions_crx_cache",
            )),
        ),
        running_process_markers=("gemini", "antigravity"),
    ),
    ScanPoint(
        "OpenCode",
        ("~/.cache/opencode", "~/.local/share/opencode/log"),
        running_process_markers=("opencode",),
    ),
    ScanPoint(
        "Cursor agent",
        ("~/.local/share/cursor-agent",),
        "confirm",
        running_process_markers=("Cursor.app", "cursor-agent"),
    ),
    ScanPoint(
        "chrome-devtools-mcp",
        tuple(
            f"~/.cache/chrome-devtools-mcp/chrome-profile/{path}"
            for path in (
                "Cache",
                "Code Cache",
                "GPUCache",
                "DawnGraphiteCache",
                "DawnWebGPUCache",
                "Service Worker/CacheStorage",
                "GraphiteDawnCache",
                "component_crx_cache",
                "extensions_crx_cache",
            )
        ),
        running_process_markers=("chrome-devtools-mcp",),
    ),
]

# ── 域 F · 废纸篓 ──────────────────────────────────────────────────
TRASH: list[ScanPoint] = [
    ScanPoint("废纸篓", ("~/.Trash",), "confirm"),
]

DOMAINS: dict[str, list[ScanPoint]] = {
    "system": SYSTEM_JUNK,
    "developer": DEVELOPER_JUNK,
    "ai": AI_TOOL_JUNK,
    "trash": TRASH,
    # project 域走目录名匹配，单独处理
}

SAFETY_ORDER = {"safe": 0, "confirm": 1, "critical": 2}

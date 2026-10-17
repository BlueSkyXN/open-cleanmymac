"""将同次扫描的类型证据转换成只读审阅行；不访问文件、不生成清理 Item。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .engine import human
from .models import Item

_KINDS = frozenset({"application_binaries", "application_languages", "downloads", "disk_image", "codex_home"})
_FILE_TYPES = {
    "disk_image": "磁盘镜像候选", "in_progress": "进行中后缀线索",
    "installer": "安装包", "archive": "压缩文件", "other": "普通文件",
}


def _size(value) -> str:
    return human(value) if isinstance(value, int) and value >= 0 else "未知"


def _modified(value) -> str:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return "未知"


@dataclass(frozen=True)
class EvidenceEntry:
    path: str
    summary: str
    lines: tuple[str, ...]

    @property
    def name(self) -> str:
        return Path(self.path).name


class EvidenceIndex:
    def __init__(self, diagnostics=()):
        self._records: dict[tuple[str, str, str], dict] = {}
        for record in diagnostics:
            if record.get("kind") in _KINDS:
                key = (record.get("anchor_path", ""), record.get("category", ""), record["kind"])
                self._records[key] = record

    def record(self, item: Item) -> dict | None:
        # 语言审计沿用经典 Item；其它同路径诊断按 kind 区分，不能互相借用证据。
        kind = item.diagnostic_kind or "application_languages"
        return self._records.get((str(item.path), item.category, kind))

    def entries(self, item: Item) -> tuple[EvidenceEntry, ...]:
        record = self.record(item)
        if record is None:
            return ()
        kind = record["kind"]
        rows = record.get("localizations" if kind == "application_languages" else "files", [])
        return tuple(_entry(kind, row, record) for row in rows)


def _entry(kind: str, row: dict, record: dict) -> EvidenceEntry:
    path = str(row.get("path", "未知"))
    lines = [
        f"路径：{path}",
        f"来源：{record['category']} · {record['anchor_path']}",
        "状态：只读证据，不提供选择或清理；容量不等于可释放空间",
        f"所属报告完整：{'是' if record.get('complete') else '否，保留部分证据'}",
    ]
    if kind == "codex_home":
        labels = {
            "configured_reference": "被配置引用", "supported_profile": "命名 profile",
            "finder_metadata": "Finder 元数据", "backup_or_example": "备份/示例线索",
            "supplemental_directory": "额外资料目录", "supplemental_document": "额外文本材料",
            "unclassified": "归属待确认",
        }
        label = labels.get(row.get("classification"), "归属待确认")
        amount = _size(row.get("allocated_bytes")) if row.get("entry_type") == "file" else "目录未计量"
        summary = f"{amount} · {label}"
        lines.extend((
            f"用途分类：{label}", str(row.get("note", "")),
            f"逻辑大小：{_size(row.get('logical_bytes'))}", f"已分配占用：{amount}",
            "小文件：" + ("是（不超过 1 MiB，不代表无用）" if row.get("small_file") else "否/目录"),
            "引用检查：仅用户 config/profile 的模型目录与指令路径；未检查项目、脚本或内嵌模板",
            "配置引用检查完整：" + ("是" if record.get("reference_scan_complete") else "否"),
        ))
    elif kind == "application_binaries":
        slices = row.get("slices", [])
        architectures = ", ".join(str(s.get("architecture", "未知")) for s in slices) or "未知"
        amount = _size(row.get("other_architecture_logical_bytes"))
        summary = f"{amount} 其它架构逻辑量 · {architectures}"
        lines.extend((f"架构：{architectures}", f"其它架构逻辑量：{amount}",
                      f"整个文件已分配占用：{_size(row.get('file_allocated_bytes'))}"))
        for part in slices:
            evidence = ("fat 表声明，未验证归档内每个对象"
                        if part.get("architecture_evidence") == "fat_table_only"
                        else "fat 表与 Mach-O 文件头")
            lines.append(f"{part.get('architecture', '未知')}：{_size(part.get('logical_bytes'))} 逻辑；依据：{evidence}")
    else:
        allocated, logical = _size(row.get("allocated_bytes")), _size(row.get("logical_bytes"))
        lines.extend((f"已分配占用：{allocated}", f"逻辑大小：{logical}"))
        if kind == "application_languages":
            language = row.get("language", "未知")
            summary = f"{allocated} · 语言 {language}"
            lines.extend((
                f"语言标识：{language}",
                f"应用开发语言：{record.get('development_language') or '未知，不推断冗余'}",
                f"保留的首选语言：{', '.join(record.get('preferred_languages', [])) or '未知'}",
                f"资源类型：{', '.join(row.get('resource_types', [])) or '未知'}",
                f"普通资源文件数：{row.get('file_count', '未知')}",
                f"本地化目录完整：{'是' if row.get('complete') else '否'}",
            ))
        else:
            label = _FILE_TYPES.get(row.get("file_type"), "未知类型")
            summary = f"{allocated} · {label}"
            lines.extend((
                f"类型：{label}",
                f"修改时间（UTC）：{_modified(row.get('latest_mtime'))}",
                "时间依据：mtime，不代表下载时间或最后使用时间",
                "使用情况：未知，不能据此判定不需要",
                "文件上下文：" + ("开发文件" if row.get("context") == "development_files" else "用户文件"),
            ))
            if kind == "disk_image":
                mount = {"mounted": "已挂载", "not_mounted": "未挂载", "unknown": "未知"}
                lines.extend((
                    f"挂载状态：{mount.get(row.get('mount_state'), '未知')}；未挂载不等于不使用",
                    f"格式描述：{row.get('format_description') or '未知'}",
                    "类型依据：后缀候选与系统格式描述，不读取镜像内部文件",
                ))
    return EvidenceEntry(path, summary, tuple(lines))

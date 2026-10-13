"""只读报告应用中的 Mach-O 多架构文件；不裁剪或修改签名。

格式依据 Apple SDK 的 mach-o/fat.h、mach-o/loader.h 和 mach/machine.h。
slice 字节数是逻辑量，不代表 APFS 独占空间或可安全删除的容量。
"""

from __future__ import annotations

import os
import stat
import struct
import subprocess
from collections.abc import Callable, Iterable

from .application_languages import _discover_applications
from .models import FileFacts, Item, ScanIssue, ScanResult
from .predicates import Predicate
from .resource_walk import WalkBudget, open_resource, walk_resources

_FAT = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}
_THIN = {
    b"\xfe\xed\xfa\xce": (">", 28),
    b"\xce\xfa\xed\xfe": ("<", 28),
    b"\xfe\xed\xfa\xcf": (">", 32),
    b"\xcf\xfa\xed\xfe": ("<", 32),
}
_CPU = {
    7: "i386",
    0x1000007: "x86_64",
    12: "arm",
    0x100000C: "arm64",
    18: "ppc",
    0x1000012: "ppc64",
}


def native_cpu_type() -> int | None:
    """hw.optional.arm64 在 Rosetta 进程中仍可用于确认 Apple Silicon。"""
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return 0x100000C
        if (
            result.returncode == 0
            and result.stdout.strip() == "0"
            and os.uname().machine == "x86_64"
        ):
            return 0x1000007
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _thin_cpu(fd: int, offset: int, size: int) -> tuple[int, int] | None:
    header = os.pread(fd, min(32, size), offset)
    layout = _THIN.get(header[:4])
    if layout is None:
        return None
    endian, minimum = layout
    if len(header) < minimum:
        raise ValueError("Mach-O header 被截断")
    cpu, subtype, _kind, _commands, command_bytes = struct.unpack_from(
        endian + "iiIII", header, 4
    )
    if minimum + command_bytes > size:
        raise ValueError("Mach-O load commands 超过 slice 边界")
    return cpu, subtype


def read_macho_slices(fd: int, size: int) -> list[dict[str, object]]:
    """仅 pread 有界文件头；fat 表和各 slice 必须互相一致。"""
    header = os.pread(fd, min(8, size), 0)
    layout = _FAT.get(header[:4])
    if layout is None:
        cpu = _thin_cpu(fd, 0, size)
        return (
            []
            if cpu is None
            else [
                {
                    "cpu_type": cpu[0],
                    "cpu_subtype": cpu[1],
                    "architecture": _CPU.get(cpu[0], f"cpu:{cpu[0]}"),
                    "offset": 0,
                    "logical_bytes": size,
                }
            ]
        )
    if len(header) != 8:
        raise ValueError("fat header 被截断")
    endian, wide = layout
    count = struct.unpack_from(endian + "I", header, 4)[0]
    if not 1 <= count <= 64:
        raise ValueError("fat 架构数量无效或超过 64")
    record_size = 32 if wide else 20
    end = 8 + count * record_size
    if end > size:
        raise ValueError("fat 架构表超过文件边界")
    table = os.pread(fd, count * record_size, 8)
    if len(table) != count * record_size:
        raise ValueError("fat 架构表读取不完整")
    records: list[dict[str, object]] = []
    identities: set[tuple[int, int]] = set()
    intervals: list[tuple[int, int]] = []
    for index in range(count):
        values = struct.unpack_from(
            endian + ("iiQQII" if wide else "iiIII"), table, index * record_size
        )
        cpu, subtype, offset, length, align = values[:5]
        if (wide and values[5] != 0) or align > 63 or offset % (1 << align):
            raise ValueError("fat slice 对齐或保留字段无效")
        if offset < end or length < 28 or offset + length > size:
            raise ValueError("fat slice 超过文件边界")
        if (cpu, subtype) in identities or any(
            offset < right and left < offset + length for left, right in intervals
        ):
            raise ValueError("fat slice 重复或重叠")
        inner_cpu = _thin_cpu(fd, offset, length)
        archive = inner_cpu is None and os.pread(fd, 8, offset) == b"!<arch>\n"
        if not archive and inner_cpu != (cpu, subtype):
            raise ValueError("fat 表与 Mach-O slice 架构不一致")
        identities.add((cpu, subtype))
        intervals.append((offset, offset + length))
        name = _CPU.get(cpu, f"cpu:{cpu}")
        if cpu == 0x100000C and subtype & 0xFFFFFF == 2:
            name = "arm64e"
        elif cpu == 0x1000007 and subtype & 0xFFFFFF == 8:
            name = "x86_64h"
        records.append(
            {
                "cpu_type": cpu,
                "cpu_subtype": subtype,
                "architecture": name,
                "offset": offset,
                "logical_bytes": length,
                "slice_format": "ar_archive" if archive else "mach_o",
                "architecture_evidence": "fat_table_only"
                if archive
                else "fat_and_macho_headers",
            }
        )
    return records


def scan_application_binaries(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    *,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
    max_entries: int = 200_000,
    native_cpu: int | None = None,
) -> ScanResult:
    from .engine import Cancelled

    result = ScanResult()
    task = "通用二进制文件"
    budget = WalkBudget(max_entries)
    native_cpu = native_cpu if native_cpu is not None else native_cpu_type()
    seen: set[tuple[int, int]] = set()
    try:
        applications = _discover_applications(
            roots, protection, result.issues, checkpoint, on_progress, task
        )
        for application in applications:
            issue_start = len(result.issues)
            files: list[dict[str, object]] = []
            for facts, parent_fd in walk_resources(
                application,
                protection,
                result,
                task,
                budget=budget,
                checkpoint=checkpoint,
                on_progress=on_progress,
                descend=lambda f: f.path.name != "_CodeSignature",
            ):
                if (
                    not stat.S_ISREG(facts.stat.st_mode)
                    or facts.path.suffix == ".class"
                ):
                    continue  # Java class 的 magic 与 fat header 相同。
                identity = (facts.stat.st_dev, facts.stat.st_ino)
                if identity in seen:
                    continue
                try:
                    with open_resource(facts, parent_fd) as fd:
                        slices = read_macho_slices(fd, facts.logical_size)
                except (OSError, ValueError) as exc:
                    result.issues.append(
                        ScanIssue(
                            "permission_denied"
                            if isinstance(exc, PermissionError)
                            else "path_disappeared"
                            if isinstance(exc, FileNotFoundError)
                            else "binary_metadata_invalid",
                            str(exc),
                            task,
                            facts.path,
                        )
                    )
                    continue
                seen.add(identity)
                if len(slices) < 2:
                    continue
                extra = (
                    sum(
                        int(s["logical_bytes"])
                        for s in slices
                        if s["cpu_type"] != native_cpu
                    )
                    if native_cpu is not None
                    and any(s["cpu_type"] == native_cpu for s in slices)
                    else None
                )
                files.append(
                    {
                        "path": str(facts.path),
                        "slices": slices,
                        "file_allocated_bytes": facts.allocated_size,
                        "other_architecture_logical_bytes": extra,
                    }
                )
                if len(files) >= 4096:
                    result.issues.append(
                        ScanIssue(
                            "scan_limit_reached",
                            "多架构明细达到 4096 项上限",
                            task,
                            application,
                        )
                    )
                    break
            if files:
                complete = not any(i.blocking for i in result.issues[issue_start:])
                known = all(
                    f["other_architecture_logical_bytes"] is not None for f in files
                )
                extra = (
                    sum(int(f["other_architecture_logical_bytes"]) for f in files)
                    if known
                    else None
                )
                total = sum(int(f["file_allocated_bytes"]) for f in files)
                note = "其它 CPU 架构的逻辑字节单列，不代表可裁剪或可释放空间；不修改应用签名"
                result.items.append(
                    Item(
                        application,
                        0,
                        task,
                        "critical",
                        note=note,
                        resource_kind="filesystem_subset",
                        diagnostic_kind="application_binaries",
                        logical_size=extra,
                        resource_total_size=total,
                        actionable=False,
                        action_block_reason="应用架构仅只读诊断，未提供裁剪执行器",
                        preselected=False,
                        identity=FileFacts.from_path(application).identity,
                        total_count=len(files),
                    )
                )
                result.diagnostics.append(
                    {
                        "kind": "application_binaries",
                        "anchor_path": str(application),
                        "category": task,
                        "complete": complete,
                        "native_cpu_type": native_cpu,
                        "other_architecture_logical_bytes": extra,
                        "file_allocated_bytes": total,
                        "measurement_basis": "slice_logical_bytes_not_reclaimable",
                        "files": files,
                    }
                )
            if budget.exhausted:
                break
    except Cancelled:
        result.cancelled = True
    return result

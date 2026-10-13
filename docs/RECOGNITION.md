# 只读类型识别与扫描覆盖

当前源码为经典 `scan` 和 `clean junk` 增加应用架构、嵌套语言资源、Chrome 旧组件、
下载文件与磁盘镜像诊断。它们帮助解释占用和识别依据；新增对象不会自动成为清理目标。
发行边界见 [CHANGELOG](../CHANGELOG.md)。

## 使用范围

```bash
openclean scan --domain system --json
openclean clean junk --no-interactive --json
openclean scan --domain system --file-root ~/Downloads --json
openclean scan --domain system --file-root ~/Downloads --file-root ~/Documents/Images --json
```

`--file-root` 仅用于 `scan` 的下载/镜像审阅，可重复，替代默认的 `~/Downloads`。
扫描域必须包含 `system`，路径必须是存在的非 symlink 目录；扫描器继续检查祖先、
云占位和文件系统边界。此参数不更改缓存、应用扫描根，也不扩张清理选择范围。
默认文件审阅不遍历整个 HOME 或外置卷。`.app`、`.sparsebundle`、`.download`
目录不在普通下载文件遍历范围，当前不报告这些目录内部的容量。

## 已有识别类型

| 类型 | 识别依据 | 当前边界 |
|---|---|---|
| 通用二进制文件 | Apple Mach-O thin/fat 文件头、架构表与 slice 交叉校验 | 只读；fat 静态库按架构表声明报告，不验证归档中每个对象。其它 CPU 架构不一定可移除。主机架构未知或缺少本机架构时，不估算其它架构容量；Java class 不作为 fat Mach-O |
| 应用语言包 | 首选语言、开发语言、Base 与嵌套 `.lproj` 资源 | 混合文件、框架资源也可审计；开发语言未知单独说明，不默认假定英语。只读、不修改签名 |
| 应用旧版本组件 | Google Chrome 应用版本、framework 的 Info.plist、Versions/Current 三方核对 | 当前仅支持公开的 Chrome framework 版本布局；未知或矛盾证据不判为旧版；全部只读 |
| 磁盘镜像候选 | `.dmg/.img/.iso/.sparseimage` 后缀线索、系统 `file` 格式描述、`hdiutil info -plist` 挂载证据 | 后缀不是格式确认，未挂载不是不使用；不自动挂载、解密或解包 |
| 下载文件审阅 | 安装包、压缩文件、镜像、进行中后缀和其它普通文件 | 小文件也可见；mtime 不是下载时间或最后使用时间；用户文档不因出现在下载目录而成为垃圾 |
| 扫描覆盖 | 当前 `ScanIssue` 中的阻断错误及对应路径 | 访问原因不明时为 `undetermined`，不凭 EPERM/EACCES 推断 FDA、TCC、SIP 或 POSIX 权限 |

镜像与下载来自同一次文件遍历，不重复计算同一文件。`testdata`、`node_modules`、`.git`
路径组件会显示开发文件上下文；镜像使用情况仍为未知。链接、云占位和跨文件系统对象
按既有规则排除，不把排除当成已确认的零占用。

挂载查询失败返回 `unknown`。空的已挂载镜像清单支持 `not_mounted`；非空清单中能
精确找到候选路径时为 `mounted`，未精确匹配则保留 `unknown`，不假设各种系统版本的
清单格式完全一致。格式查询使用已验证文件描述符，检查身份，并设置超时。

架构扫描跳过 `_CodeSignature` 签名元数据；不因此声称已验证签名。零块文件仅被视为
疑似占位，不确定它是否真实位于云端。重复的边界跳过提示在文本中汇总，JSON 保留逐路径记录。

资源遍历最多检查 200000 个目录项和 64 层目录；多架构明细每应用最多 4096 项，
文件审阅每根最多 4096 项。达到限制保留已完成结果并报告不完整。镜像格式查询最多
64 次，其余保留格式未知；不会通过无限扫描填补证据。

## JSON 与计量

CLI envelope 继续使用 `schema_version=2`；原有字段和精确选择含义保留。有相关证据时
新增可选的 `diagnostics` 数组，旧消费者可忽略；使用严格字段白名单的消费者需允许此扩展。
Item 持久化字段集合、Run bundle 版本和现有 pack 保持不变。新诊断不通过 Agent pack 执行。

所有诊断含 `kind`、`category`、`complete`；对象诊断另含 `anchor_path`，该路径是证据锚点，
不是执行 selector。对外分享使用 `--redact-paths`，锚点、明细路径及错误说明均脱敏。

| `kind` | 主要明细 | 计量解释 |
|---|---|---|
| `application_binaries` | `native_cpu_type`、`files[].path`、`slices[]`、`other_architecture_logical_bytes` | slice 是逻辑字节；`file_allocated_bytes` 是涉及文件的已分配占用，不能相加或据此承诺裁剪收益 |
| `application_languages` | `development_language`、`preferred_languages`、`localizations[]` | 每个本地化目录有路径、语言标识、资源类型、文件数和物理/逻辑计量；未知开发语言为 null |
| `application_updates` | `installed_version`、`staged_version`、`current_version`、`updater_status` | 旧组件的物理/逻辑占用单列；版本判断不授权修改应用 |
| `downloads` / `disk_image` | `files[].file_type`、时间依据、上下文；镜像增加挂载状态和格式描述 | 普通文件元数据；没有“已不需要”的自动判断 |
| `scan_coverage` | `unreadable_path_count`、`failures[].path/task/code/cause/message` | 当前扫描的阻断范围，不能据此推算未读取字节数 |

应用架构、旧组件、下载和镜像项采用只读 `filesystem_subset`，`bytes/potential_bytes`
为 0 表示不并入清理候选总量；其实际计量使用 `resource_total_bytes`、`logical_bytes` 和
`diagnostics` 中的字段显示。零值不表示这些对象不占空间。文字列表标明“逻辑”或“单列”。
这些项的 `reclaimable_bytes` 始终为 0，不与语言资源、依赖缓存或其它聚合诊断重复相加。
语言资源保留原有子集物理计量及只读执行边界。

格式与容量只是当前文件快照。系统未授权的位置仍会使 `complete=false`，新增诊断不会
改变访问权限、关闭应用或自动运行提权命令。

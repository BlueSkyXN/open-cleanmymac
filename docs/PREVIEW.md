# 全功能隔离预览

本项目提供一个可重复、无需触碰真实用户数据的功能预览：

```bash
make preview
```

机器可读版本：

```bash
cd implementation
PYTHONPATH=. python3 scripts/preview_all.py --json
```

## 隔离保证

预览脚本在 `TemporaryDirectory` 中创建独立 `HOME`、规则文件、配置、Trash、项目、缓存、
应用语言包和分析文件。所有允许写入的演示都只作用于这些临时夹具。脚本结束时临时目录
由 Python 清理，并在结果中输出：

```json
{
  "workspace": "TemporaryDirectory",
  "real_user_data_modified": false,
  "passed": true,
  "scenario_count": 19
}
```

脚本会 mock 动态进程、Docker、Trash 和语言偏好发现，避免读取或调用真实 Docker daemon、
真实用户 Trash、正式规则服务和特权 helper。它不需要也不会请求 sudo。

## 覆盖场景

| 分组 | 场景 |
|---|---|
| 基础 | version、cat |
| 扫描 | system/developer/ai/trash/project 五域聚合 |
| 只读清理 | clean junk/dev/ai/trash |
| 项目 | purge 发现、分组、7 天预选 |
| 空间 | analyze 一级排序、卷信息 |
| 本地状态 | ignore add/list/remove、config analytics on/off |
| 临时执行 | clean junk/dev/ai、clean trash、purge、analyze |
| 安全拒绝 | optimize ram、optimize purgeable |

`clean`、`purge` 和 `analyze` 的临时执行场景会验证：候选确实从夹具位置移走、普通项进入
临时同卷 Trash、清空临时 Trash 后根目录仍存在，以及 JSON report 的受影响字节合理。

## 不在隔离预览中伪造的能力

| 能力 | 预览状态 | 原因 |
|---|---|---|
| SMAppService/XPC 特权清理 | `external-prerequisite` | 需要 native app/helper、签名身份、Team ID 和真实安装验收 |
| Docker 真实 prune | `external-prerequisite` | 需要用户自己的 Docker CLI/daemon，且操作不可通过 Trash 恢复 |
| 签名托管知识库真实更新 | `external-prerequisite` | 需要项目 HTTPS channel 和正式公钥 |
| optimize ram/purgeable | `guarded-unavailable` | 没有已验证、安全、公开的等价接口 |
| universal binary thinning | `not-implemented` | 会影响签名与兼容性，当前不修改应用二进制 |

这些场景属于产品边界，不是 preview failure。

## 手工只读预览

安装后可以在真实机器上运行以下只读命令：

```bash
openclean scan --json
openclean clean junk --no-interactive
openclean clean dev --no-interactive
openclean clean ai --no-interactive
openclean clean trash --no-interactive
openclean purge ~/Projects --no-interactive
openclean analyze ~ --no-interactive --top 20
openclean ignore list --json
openclean config --json
openclean optimize ram --json
openclean optimize purgeable --json
```

最后两个命令预期退出码为 `1`，并输出 `status=unavailable`；这是安全锁，不是执行失败。

不要为了“体验完整流程”在真实数据上盲目添加 `--yes`。需要验证写路径时，优先使用本页
的隔离脚本，或自行在单独的临时目录中建立夹具。

## TUI 快捷键

### clean / purge

- `↑/↓`：移动；
- `Enter/→`：进入分组；`←`：返回；
- `Space`：切换当前项；`A`：切换当前组可执行项；
- `Enter`：进入汇总；
- `Y`：确认执行（仍要求启动命令带 `--yes`）；
- `!`：critical 项的独立二次确认；
- `Q`：取消。

### analyze

- `↑/↓`：移动；`Enter/→`：进入目录；`←`：返回；
- `Space/A`：选择；`O`：Finder reveal；
- `Delete`：查看选择汇总；
- `Y`：确认执行（仍要求 `--yes`）；`Q`：取消。

TTY 布局依赖终端尺寸。CI 验证逻辑和取消路径，不进行像素级视觉验收。

## 解释 JSON 与退出码

- `0`：命令按其契约完成；
- `1`：结果不完整、执行 outcome 失败，或能力明确 unavailable；
- `2`：参数、规则、路径或选择错误；
- `130`：用户中断。

`--json` 模式下，参数和运行时错误也会输出可解析 envelope。所有结果可能含绝对路径；
保存或分享前应脱敏。

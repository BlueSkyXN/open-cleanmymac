# 07 · 底层判定引擎规格（谓词系统 + 实体模型）

> 净室规格：还原 CleanMyMac 的**底层判定引擎**（扫描系统的真正底座）。
> 来源：参考对象判定层的类型与行为事实。只描述行为契约。
> 这是"判定一项该不该清"的统一抽象，所有扫描域都构建于其上。

---

## 1. 核心抽象：谓词（Predicate）

**一切"是否忽略"的判定都被抽象为谓词对象**，协议方法：

```
shouldIgnoreObject(obj) -> Bool     // obj 多态: NSString路径 | NSURL | 文件树节点
```

谓词**正交、可组合**——每种判定职责一个谓词类，用组合谓词把它们并联/串联：

| 谓词 | 构造 | 语义（何时判"忽略"） |
|---|---|---|
| **组合谓词** | `init(type, subpredicates)` | type=AND：所有子谓词都忽略才忽略；type=OR：任一忽略即忽略。短路求值。 |
| **KB 忽略谓词** | `init(knowledgeBase)` | 命中知识库忽略/保护规则 → 忽略（核心安全闸） |
| **文件名谓词** | `init(fileNamePattern)` | 文件名匹配模式(glob/正则) → 忽略 |
| **大小谓词** | `init(fileSizeLimit)` | 文件大小低于/超过阈值 → 忽略 |
| **存在谓词** | `init(fileManager)` | 目标文件不存在 → 忽略（判"失效启动项/偏好"用） |
| **可达谓词** | — | 资源/网络可达性 |
| **访问谓词** | `init(fileManager)` | 文件是否"可被处理"（基类桩 `_isFileCanBeHandled` 恒真，子类按 supportedTypes 覆写） |

### KB 忽略谓词的精确匹配算法（核心安全闸）

```
func shouldIgnoreObject(obj) -> Bool:
    if obj 是 NSString路径:  url = fileURL(path);  path = obj
    if obj 是 NSURL:         url = obj;            path = url.path
    if obj 是 文件树节点:     url = node.url;       path = node.path
    # 双查：URL 与 path 各查一次知识库
    if knowledgeBase.shouldIgnoreURL(url):   return true   # URL 命中 → 忽略
    if knowledgeBase.shouldIgnorePath(path): return true   # path 命中 → 忽略
    return false
```
**契约：任何项先过此谓词；URL ∪ path 任一命中知识库即判忽略（不可清）。**

---

## 2. 实体 / 结果模型（可序列化）

判定产出的"项"用统一实体模型承载，且**支持 NSSecureCoding 序列化**
（这正是知识库 `.cmmkb` 的落盘格式 = NSKeyedArchiver 对象图 + 压缩 + 外层编码）：

```
Entity { value(标题), size, autoselected }
├─ FileModel       + isCloudFile(iCloud 占位文件) [, creationDate]
├─ FileSystemEntity
├─ FileInfo        { path, size, isDirectory }
├─ TreeEntity      (树形, 空间透镜用)
└─ PurgeableSpaceEntity  (可 purge 空间)

ScanResult / CompoundScanResult / MutableScanResult
CompoundScanTask  { identifier, tasks[] }   // 任务组合,递归执行
```

**云文件维度**：`isCloudFile` 标记 iCloud 等"占位/数据less"文件——判定大小时不能
把未下载的云占位当作可释放（删了也不释放本地空间）。**这是独立实现必须对齐的细节。**

---

## 3. URL 序列遍历

```
URLIterator / FileURLSequenceModel / GroupedSequenceModel
```
→ 把"待扫路径集合"抽象为可迭代序列，供统一遍历；Grouped 支持按组分桶
（如按应用分组缓存项）。

---

## 4. 一次判定的完整组装（缓存为例）

```
pathsProvider 产出候选 URL 序列
   │
   ▼
CompoundPredicate(AND)[
    FileNamePredicate(pattern),        # 类别形态过滤
    FileIgnorePredicate(knowledgeBase) # KB 安全闸
]
   │ 逐项求值(先 FileIgnore 安全闸, 再形态)
   ▼
存活项 → FileSizing 计物理大小 → 包装为 FileModel(含 isCloudFile)
   │
   ▼
入 ScanResult, 标安全级
```

---

## 5. 对独立实现的指导

1. **谓词协议**：`should_ignore(item) -> bool`，item 带 path 与 URL。
2. **组合谓词**：AND / OR，支持短路。
3. **KB 忽略谓词**：查自建规则；路径规则在最外层优先短路。
4. **文件名/大小/存在谓词**：用于失效项、按类型过滤、按大小过滤。
5. **云文件**：实体需能标记占位/dataless，避免把未下载对象计入可释放空间。
6. **安全闸顺序**：KB 忽略谓词永远最先求值。

Reachability 与 FileAccess 的专项语义、以及本项目实际采用的 Darwin `SF_DATALESS`
启发式，见 [_index.md](_index.md) 与 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。

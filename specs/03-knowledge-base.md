# 03 · 自建规则、忽略与配置

> 文档 ID：OC-03 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT、SRC-EXPERIENCE；见 [索引](_index.md)。

## 1. 职责边界

KnowledgeBase 负责规则匹配，RulesStore 负责用户规则持久化；
Strategy Registry 负责 Agent pack。两者不是同一个格式，也不相互改名替代。
参考软件的私有数据库、应用指纹、容器编码或内部查询签名不是本项目协议。

规则内容来自可追溯的公开资料、独立实现和已验证经验。
新增 detector 的来源要求见 [02](02-scan-points.md)，个人研究流程见 [AR-05](agent-runtime/ar-05-research-governance.md)。

## 2. 文件与规则字段

默认用户规则为 `~/.config/openclean/rules.json`；托管规则为同目录 `knowledge.json`。
`--rules FILE` 使用显式单文件，`--ignore` 是本次调用的附加忽略。
Agent 自定义 HOME 的默认规则/Store 解析见 [AR-03](agent-runtime/ar-03-cli-and-io-contract.md)。

| 字段 | 作用 | 契约与限制 |
|---|---|---|
| `schema_version` | 本地规则格式版本，当前 1 | 与 CLI/Run/pack 版本独立 |
| `ignore.paths/globs/regexes` | 忽略路径或模式 | 路径必须绝对或 HOME-relative；路径匹配自身和后代 |
| `protect` | 系统/明确保护路径或规则 | 与 ignore 一样阻止处理，不能由普通扫描规则覆盖 |
| `applications` | 应用名、保护、附加文件、deep_search 等声明 | 可解析字段不等于全部已接到生产 scanner；不从声明直接删除附加文件 |
| `_managed` | 托管来源状态 | 更新器管理，不是用户自授权限入口 |

完整字段、示例与格式验证以 [实现契约](../implementation/README.md)、
[knowledge_base.py](../implementation/openclean/knowledge_base.py) 为准，不新增 DSL。

## 3. 行为需求

| 需求 | 契约 | 验收 |
|---|---|---|
| REQ-RULE-001 保护优先 | KB protect/ignore 在普通谓词之前求值，执行前重新使用当前规则 | VAL-RULE-001：被保护路径及后代不被普通匹配规则重新放行 |
| REQ-RULE-002 用户 ignore | list 读取；add/remove 显式修改用户 JSON，幂等并返回 changed | VAL-RULE-002：重复增删不破坏规则，回读一致，不混写托管文件 |
| REQ-RULE-003 规则分层 | 默认托管与用户规则合并，远程更新不覆盖用户 ignore | VAL-RULE-003：用户规则在更新前后保留，非法格式返回错误 |
| REQ-RULE-004 配置副作用 | analytics 仅是持久偏好；目前无遥测上传；读配置不伪造网络操作 | VAL-RULE-004：显示值与显式写入一致，错误非零 |
| REQ-RULE-005 原子写 | 沿用私有权限、临时写入/fsync/原子安装与错误回执 | VAL-RULE-005：失败不报告成功，配置文件权限与提交边界符合实现 |

`ignore add/remove`、`config --analytics`、显式规则更新是现有配置写入口，不另要求清理用
`--yes`；它们仍需要用户对配置变更的指令。不要将“清理默认预览”误写成“所有命令不带 --yes 完全不写盘”。

## 4. 托管规则更新

现有客户端通过显式 `config --update-knowledge HTTPS_URL --knowledge-public-key PEM`
触发网络与写入：HTTPS、大小上限、签名校验、公钥钉扎、sequence 防回滚和原子安装。
采用自己的 JSON/envelope，不加载厂商私有数据库。

REQ-RULE-006：验证失败不得安装新规则或覆盖用户 ignore；必须报告失败。
VAL-RULE-006：签名/sequence/URL/超限/并发更新反例与成功回读通过隔离测试。

正式 HTTPS channel、正式公钥和发布运维仍是外部前提，不设置假默认地址或生成凭据冒充交付。
上述命令是已存在客户端，不代表正式规则服务已经运行；本规格不授权网络更新。

## 5. 验收锚点

[test_knowledge_base.py](../implementation/tests/test_knowledge_base.py)、
[test_rules_store.py](../implementation/tests/test_rules_store.py)、
[test_scan_rules_integration.py](../implementation/tests/test_scan_rules_integration.py)、
[test_knowledge_update.py](../implementation/tests/test_knowledge_update.py)、
[test_config_cli.py](../implementation/tests/test_config_cli.py)。

签名环境和正式服务的真实验证与单元测试分开记录。

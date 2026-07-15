---
name: dingtalk-doc-multica-sync
description: Use when binding a Multica agent to a DingTalk document knowledge base and syncing its core definitions (entry AGENTS.md + skills/) from a DingTalk node into the agent. Actively triggered, one command; dynamic data (memories/storage) is out of scope.
license: MIT
metadata:
  author: d1-2004
  version: '1.0.0'
---

# DingTalk ⇄ Multica Sync

把一个 **Multica agent** 绑定到一个 **钉钉文档知识库节点**,并主动同步它的**核心定义**。装上这个 skill,Multica 的 agent 就直接挂在一份文档知识库上:改文档 → 跑一次同步 → agent 的身份与技能随之更新。人在钉钉里编辑的,和 agent 用的,是同一份东西。

## 何时用

- 想让一个 Multica agent 的定义(instructions + skills)由一份钉钉文档知识库来管。
- 在钉钉里改了 Agent 的 `AGENTS.md` 或某个 `skills/<name>/SKILL.md`,要把变更发布到 Multica。
- 需要一个**可审阅、主动触发**的发布动作,而不是后台自动漂移。

## 前置

- **`dws`** —— 钉钉全产品 CLI(读文档树)。`dws: command not found` → `export PATH="$HOME/.local/bin:$PATH"`。
- **`multica`** —— Multica CLI(写 agent / skill),已登录目标 workspace。
- Python 3.9+(仅用标准库)。

## 一次同步 = 三步(pull → push → verify)

```bash
# 0. 填绑定:把 agents.md 里 ```json 块的占位符改成真实 ID
#    - dingtalk.source_node = 钉钉知识库根节点(创建 Agent 的文档)
#    - multica.agent_id     = 目标 Multica agent

# 1. 只物化,看清范围(只读侧,安全)
python3 scripts/sync.py pull

# 2. 物化 + 打印将写入 Multica 的计划(默认干跑,不写)
python3 scripts/sync.py sync

# 3. 确认后真正写入,回读校验 + 完成质检
python3 scripts/sync.py sync --yes

# 随时单独复检 agent 是否完备
python3 scripts/sync.py qc
```

`pull` 把入口文档与 `skills/` 拉到 `./.agent-workspace`;`push` 把入口文档写成 agent 的 **instructions**、每个 `skills/<name>/` 建/更新为一个 skill(name+description 写全)并连同 `dws` 基础技能挂载;`verify` 回读逐一比对;`qc` 检查 agent 是否完备。

## 边界(第一阶段,主动触发)

| | 同步 | 说明 |
|---|---|---|
| 入口文档 `AGENTS.md` | ✅ | → Multica agent instructions |
| `skills/<name>/SKILL.md` + `references/*.md` | ✅ | → 一个 Multica skill(+ 附件),挂载到 agent |
| `dws` 基础技能 | ✅ | 默认挂载(`base_skills`);钉钉原生 Agent 靠 dws 说话,缺了跑不动 |
| `memories/` `storage/` `artifacts/` | ❌ | 动态数据/运行状态,不是定义。第二阶段由 DWS 按场景运行时拉取 |
| 二进制/脚本资产(`.py` `.xlsx` …) | ❌ | 非定义;钉钉侧是文件节点,本阶段跳过 |

同步范围由 `agents.md` 的 `sync.include` / `sync.exclude` 决定;基础技能由 `base_skills` 决定,默认即上表。

## 关键约束(沿用底座实测教训)

- **主动发起**:本 skill 不创建任何定时/触发自动化。要更新就改文档再手动 `sync --yes`。
- **dws 默认必挂**:`base_skills` 默认 `["dws"]`,钉钉原生 Agent 全靠它跟钉钉说话;缺了 `qc` 报错。
- **技能元数据不留空**:name 剥掉 `SKILL:` 前缀;description 无 frontmatter 时从正文首段提炼;create/update 都写全,老技能空描述也补正。
- **完成有质检**:`sync --yes` 末尾自动 `qc`(instructions/dws/每技能 name+description+content+挂载),半成品当场拦。
- **接口 success ≠ 内容真写入**:每次写完都 `skill get` / `agent get` 回读比对才算成功(`verify` 已内建)。
- **坏响应不当空目录**:钉钉 `doc list` 的坏 envelope 会被判为「列表失败」而非「空」,拒绝据此漏拉。
- **写前干跑**:`sync` 默认不写,先打印计划;`--yes` 才落 Multica。
- **skill 去重**:同名 skill 靠 `.sync-state.json`(name→id)记住,重跑是更新而非重复创建;跨 Agent 撞名用 `multica.skill_name_prefix` 隔离。

## 装进 Multica(闭环用法)

先导入技能库:

```bash
multica skill import --url https://github.com/d1-2004/dingtalk-doc-multica-sync
```

- **创建智能体时把它加进技能集** —— 智能体自此带着「从钉钉文档节点同步定义」的能力。填好 `agents.md`,跑 `sync --yes`,它的 instructions 与技能就从文档拉齐。
- **也是一份写 instruction 的说明** —— 目标 agent 的 `instructions` 应当就是钉钉里的入口文档(`AGENTS.md`)。等价命令:`multica agent update <agent-id> --instructions "$(cat .agent-workspace/AGENTS.md)"`。

闭环:本 skill 既是挂在智能体上的一个技能,又是维护这个智能体定义的工具 —— 同步对象可以正是它自己所在的 agent。改文档 → 重新同步 → 定义更新 → 回到文档。

## 绑定配置

见仓库根的 [`agents.md`](./agents.md) —— 它声明了「哪份文档创建 Agent、入口是什么、同步到哪个 agent、同步什么」。

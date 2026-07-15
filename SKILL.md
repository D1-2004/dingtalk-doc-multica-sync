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

# 3. 确认后真正写入,并回读校验
python3 scripts/sync.py sync --yes
```

`pull` 把入口文档与 `skills/` 拉到 `./.agent-workspace`;`push` 把入口文档写成 agent 的 **instructions**、每个 `skills/<name>/` 建/更新为一个 skill 并挂载;`verify` 用 `agent get` / `skill get` 回读逐一比对。

## 边界(第一阶段,主动触发)

| | 同步 | 说明 |
|---|---|---|
| 入口文档 `AGENTS.md` | ✅ | → Multica agent instructions |
| `skills/<name>/SKILL.md` + `references/*.md` | ✅ | → 一个 Multica skill(+ 附件),挂载到 agent |
| `memories/` `storage/` `artifacts/` | ❌ | 动态数据/运行状态,不是定义。第二阶段由 DWS 按场景运行时拉取 |
| 二进制/脚本资产(`.py` `.xlsx` …) | ❌ | 非定义;钉钉侧是文件节点,本阶段跳过 |

同步范围由 `agents.md` 的 `sync.include` / `sync.exclude` 决定,默认即上表。

## 关键约束(沿用底座实测教训)

- **主动发起**:本 skill 不创建任何定时/触发自动化。要更新就改文档再手动 `sync --yes`。
- **接口 success ≠ 内容真写入**:每次写完都 `skill get` / `agent get` 回读比对才算成功(`verify` 已内建)。
- **坏响应不当空目录**:钉钉 `doc list` 的坏 envelope 会被判为「列表失败」而非「空」,拒绝据此漏拉。
- **写前干跑**:`sync` 默认不写,先打印计划;`--yes` 才落 Multica。
- **skill 去重**:同名 skill 靠 `.sync-state.json`(name→id)记住,重跑是更新而非重复创建;跨 Agent 撞名用 `multica.skill_name_prefix` 隔离。

## 导入到 Multica

```bash
multica skill import --url https://github.com/d1-2004/dingtalk-doc-multica-sync
```

导入后,给要管的 agent 挂上本 skill,填好 `agents.md`,即可在 Multica 侧发起同步。

## 绑定配置

见仓库根的 [`agents.md`](./agents.md) —— 它声明了「哪份文档创建 Agent、入口是什么、同步到哪个 agent、同步什么」。

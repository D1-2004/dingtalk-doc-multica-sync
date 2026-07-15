<!--
  agents.md —— 同步绑定配置(sync manifest)
  ───────────────────────────────────────────────────────────────────────────
  这是本仓库唯一的**同步配置文件**。一份 agents.md = 一次「Multica agent ⇄ 钉钉
  文档知识库」的绑定。脚本 skills/dingtalk-doc-multica-sync/scripts/sync.py 只读这份
  文件,在钉钉与 Multica 之间双向同步 Agent 的核心定义。把下面 ```json 代码块里的占
  位符改成你自己的真实 ID,即完成绑定。脚本解析的是**第一个 ```json 代码块**;散文不影响解析。
-->

# agents.md — 同步绑定配置

> 一句话:声明「哪份钉钉文档创建这个 Agent」「入口是什么」「同步到哪个 Multica agent」「同步什么、不同步什么」。

## 这个 Agent 由哪份文档创建?入口是什么?

- **创建 Agent 的文档(source of truth)= `dingtalk.source_node`。**
  它是钉钉文档空间里一个**知识库根节点**(folderId / nodeId)。这棵树就是 Agent 的
  全部定义之家:根下的入口文档是身份,`skills/` 子树是技能。人在钉钉客户端编辑的、
  和同步推给 Multica 的,是**同一份东西**。

- **入口(entry)= `dingtalk.entry`,默认 `AGENTS.md`。**
  根节点下的这份文档就是 Agent 的大脑 —— 身份、行为准则、路由。同步时它成为
  Multica agent 的 **instructions**。换句话说:改钉钉里的 `AGENTS.md`,再跑一次
  同步,Multica 上这个 agent 的「人格」就更新了。

- **同步到哪(target)= `multica.agent_id`。** 绑定的目标 Multica agent。

## 绑定(把占位符换成真实 ID 即可)

```json
{
  "dingtalk": {
    "source_node": "PUT-DINGTALK-ROOT-NODE-ID-HERE",
    "entry": "AGENTS.md"
  },
  "multica": {
    "agent_id": "PUT-MULTICA-AGENT-ID-HERE",
    "skill_name_prefix": "",
    "base_skills": ["dws"]
  },
  "sync": {
    "include": ["AGENTS.md", "skills/**"],
    "exclude": ["memories/**", "storage/**", "artifacts/**", "tools/**"]
  }
}
```

字段说明:

| 字段 | 含义 |
|------|------|
| `dingtalk.source_node` | 钉钉知识库根节点 ID(创建/定义 Agent 的文档)。可直接粘贴 `https://alidocs.dingtalk.com/i/nodes/<id>` 整条链接,脚本会抽出 `<id>`。 |
| `dingtalk.entry` | 入口文档名(根节点下作为 agent instructions 的那份)。默认 `AGENTS.md`。 |
| `multica.agent_id` | 目标 Multica agent 的 UUID。没有就先 `multica agent create --name "…" --runtime-id <runtime>` 建一个,把返回的 id 填进来。 |
| `multica.skill_name_prefix` | 可选。给同步出的 skill 名统一加前缀(如 `"fde/"`),避免同一 workspace 里多个 Agent 的同名 skill 撞车。默认空。 |
| `multica.base_skills` | 默认给 agent 挂上的**基础技能名**列表。默认 `["dws"]` —— 钉钉原生 Agent 全靠 dws CLI 跟钉钉说话,**没有 dws 技能整套都跑不动**,所以默认必挂。要加别的公共底座技能就往列表里加名字;这些技能须已存在于 workspace 技能库(按名解析),缺了质检会标红。 |
| `sync.include` | 只同步匹配这些 glob 的定义文档。默认 = 入口文档 + 整棵 `skills/`。 |
| `sync.exclude` | 从 include 里再挖掉的部分。默认排除全部动态数据。 |

## 同步什么 · 不同步什么(第一阶段边界)

**同步(核心定义)**:入口文档 `AGENTS.md` + `skills/<name>/` 下的文本定义
(`SKILL.md` 及其 `references/*.md`)。这些是 Agent 的「身份 + 技能」,是需要
随文档演进而被推到 Multica 的部分。

**不同步(动态数据)**:`memories/`、`storage/`、`artifacts/`,以及任何二进制/
脚本资产。它们是上下文与运行状态,不是定义。**第二阶段**由 DWS 按场景在运行时
动态拉取,不进这条同步链。所以本文件默认 `exclude` 了它们 —— 这是有意的,不是遗漏。

## 怎么触发(同步永远是主动发起的)

脚本在 `skills/dingtalk-doc-multica-sync/scripts/sync.py`(下面用 `$SYNC` 代指)。

```bash
SYNC=skills/dingtalk-doc-multica-sync/scripts/sync.py

# 正向:钉钉 → Multica
python3 $SYNC pull          # 只物化,看清范围(只读,安全)
python3 $SYNC sync          # 干跑:打印将写入 Multica 的计划,不写
python3 $SYNC sync --yes    # 真写入 + 回读校验 + 完成质检
python3 $SYNC qc            # 随时单独复检 agent 是否完备

# 反向:Multica → 钉钉(有人在 Multica 改了定义,写回源文档)
python3 $SYNC push-dingtalk         # 干跑:打印钉钉侧哪些会被写回
python3 $SYNC push-dingtalk --yes   # 保真写回
```

`sync --yes` 末尾自动跑**质检(qc)**:instructions 非空、`dws` 已挂载、知识库指针可达
(env `DINGTALK_KB_ROOT` 或 AGENTS.md 声明,`sync --yes` 会自动写 env)、每个技能
name/description/content 非空且已挂载。有硬项不过就报「N 项待修」并非零退出。

`push-dingtalk` **保真写回**:走钉钉原生解析器渲 JSONML + 反棘轮 + 乐观锁 + 回读,
`_斜体_`↔`*斜体*` 这类钉钉重序列化判为一致、免写;钉钉侧有并发改动则拒绝覆盖。

本工具**不注册任何定时/触发自动化**。要更新,改哪边就往另一边同步一次 —— 一次可审阅的
发布动作,不是后台悄悄发生的事。

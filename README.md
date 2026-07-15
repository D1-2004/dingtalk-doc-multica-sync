# dingtalk-doc-multica-sync

> 一个 Skill:在有 [`dws`](https://github.com/) 的前提下,把一个 **Multica agent** 和一个 **钉钉文档知识库节点** 链接互通 —— 主动同步 Agent 的核心定义。

装上它,Multica 的 agent 就直接绑在一份钉钉文档知识库上:**改文档 → 跑一次同步 → agent 的身份与技能随之更新**。人在钉钉客户端里编辑的,和 agent 用的,是同一份东西。配置与文档从此同源。

## 心智模型

```
   钉钉文档知识库(source of truth)              Multica agent(部署目标)
   <root node>/                                 ┌───────────────────────┐
   ├── AGENTS.md    ── 入口/身份 ──────────────▶ │ instructions          │
   ├── skills/                                   │                       │
   │   ├── <a>/SKILL.md ──── 一技能一档 ───────▶ │ skill <a>  (挂载)      │
   │   └── <b>/SKILL.md ──────────────────────▶ │ skill <b>  (挂载)      │
   ├── memories/    ✗ 不同步(动态数据)          └───────────────────────┘
   └── storage/     ✗ 不同步(运行状态)
                         │
                    scripts/sync.py           ← 主动触发,一条命令
                    (pull → push → verify)
```

- **钉钉文档 = 定义的家。** Agent 的身份(入口 `AGENTS.md`)与技能(`skills/`)都活在文档里,可被人和 agent 同源读写。
- **Multica agent = 运行的家。** 同步把定义投影成 agent 的 instructions + 挂载的 skills。
- **同步 = 主动发起的发布。** 不是后台漂移;是一条可审阅、可干跑、写后回读的命令。

## 只同步定义,不同步动态数据

| | 同步 | 说明 |
|---|---|---|
| 入口 `AGENTS.md` | ✅ | → agent instructions(身份/行为准则/路由) |
| `skills/<name>/SKILL.md` + `references/*.md` | ✅ | → 一个 Multica skill(+ 附件),挂载到 agent |
| `memories/` `storage/` `artifacts/` | ❌ | 上下文与运行状态,不是定义 |
| 二进制 / 脚本资产 | ❌ | 非定义;钉钉侧是文件节点,本阶段跳过 |

**为什么记忆不同步:** 记忆等动态数据属于上下文与状态存储,应由 DWS 在运行时按场景动态拉取(**第二阶段**),而不是被塞进「定义同步」这条链里。所以本仓库刻意把它们排除在同步范围外 —— 这是设计,不是遗漏。

## 前置

- **`dws`** — 钉钉全产品 CLI(读文档树)。
- **`multica`** — Multica CLI(写 agent / skill),已登录目标 workspace。
- **Python 3.9+**(脚本仅用标准库)。

## 快速上手

**1. 填绑定** —— 编辑 [`agents.md`](./agents.md) 里的 ` ```json ` 配置块:

```json
{
  "dingtalk": { "source_node": "<钉钉知识库根节点 ID>", "entry": "AGENTS.md" },
  "multica":  { "agent_id": "<目标 Multica agent 的 UUID>" }
}
```

没有目标 agent?先建一个:`multica agent create --name "MyAgent" --runtime-id <runtime>`,把返回的 id 填进去。

**2. 只物化,看清范围(安全)**

```bash
python3 scripts/sync.py pull
```

**3. 干跑同步(默认不写,只打印计划)**

```bash
python3 scripts/sync.py sync
```

**4. 确认后真正写入,并回读校验**

```bash
python3 scripts/sync.py sync --yes
```

之后每次要更新:在钉钉里改文档 → 再跑一次 `sync --yes`。

## 它怎么工作

`scripts/sync.py` 三段:

1. **pull** — 从 `dingtalk.source_node` 逐层并发 `dws doc list` 递归发现文件夹树,`dws doc read` 拉取入口文档与 `skills/` 下的 adoc 文档,反解钉钉 adoc 转义后物化到 `./.agent-workspace`。动态目录与二进制被显式跳过并列出。
2. **push** — 入口文档 → `multica agent update --instructions`;每个 `skills/<name>/` → `multica skill create/update`(+ `skill files upsert` 附件)→ `multica agent skills add` 挂载。name→id 存进 `.sync-state.json`,重跑是更新而非重复创建。
3. **verify** — `multica agent get` / `multica skill get` 回读,与本地物化逐一比对(接口 success 不算数,内容一致才算)。

## 导入到 Multica

本 skill 支持直接导入:

```bash
multica skill import --url https://github.com/d1-2004/dingtalk-doc-multica-sync
```

导入后把它挂到要管的 agent 上,填好 `agents.md`,即可发起同步。

## 与「钉钉原生 Agent」底座的关系

这个 skill 是钉钉原生 Agent 范式的一块:Agent 的定义外挂在钉钉文档,干净工作区从文档拉起,钉钉是唯一持久层。完整底座(创建 / 冷启动 boot / 自管理 / 自进化)见 [`D1-2004/dingtalk-agent`](https://github.com/D1-2004)。本仓库只做一件事,并把它做干净:**把定义从文档同步到 Multica agent**。

## 设计取舍与坑

- **主动 > 自动。** 同步是一次可审阅的发布,不注册定时/触发。要更新就改文档再手动跑。
- **只读侧先行。** `pull` 与 `sync`(无 `--yes`)都不写 Multica,先看清范围与计划。
- **回读定成败。** dws/multica 的接口 `success` 不等于内容真写入;一律回读比对。
- **坏响应不当空目录。** 钉钉 `doc list` 的坏 envelope 被判「列表失败」,拒绝据此漏拉整棵子树。
- **同名 skill 隔离。** 同一 workspace 多个 Agent 若有同名 skill,用 `multica.skill_name_prefix` 加前缀避免撞车。

## License

MIT

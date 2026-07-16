# dingtalk-doc-multica-sync

> 一个 Skill:在有 [`dws`](https://github.com/) 的前提下,把一个 **Multica agent** 和一个 **钉钉文档知识库节点** 双向链接互通 —— 主动、保真地同步 Agent 的核心定义。

装上它,Multica 的 agent 就直接绑在一份钉钉文档知识库上:**改文档 → 同步 → agent 更新;在 Multica 改 → 写回文档**。人在钉钉客户端里编辑的,和 agent 用的,是同一份东西。配置与文档从此同源。

## 心智模型(双向)

```
   钉钉文档知识库(唯一持久层)                   Multica agent(运行目标)
   <root node>/                                 ┌───────────────────────┐
   ├── AGENTS.md    ═══ 入口/身份 ═════════════▶ │ instructions          │
   ├── skills/          sync(正向)             │  + DINGTALK_KB_ROOT 环境变量
   │   ├── <a>/SKILL.md ═══ 一技能一档 ════════▶ │ skill <a>  (挂载)      │
   │   └── <b>/SKILL.md ◀══ push-dingtalk ═════ │ skill <b>  (挂载)      │
   ├── memories/    ✗ 不同步(动态数据)          │  + dws 基础技能(默认挂) │
   └── storage/     ✗ 不同步(运行状态)          └───────────────────────┘
                         ▲  保真:钉钉原生解析器渲 JSONML + 反棘轮 + 乐观锁 + 回读
                    scripts/sync.py — 主动触发,可审阅、可干跑、写后回读
```

- **钉钉文档 = 定义的家(唯一持久层)。** 身份(入口 `AGENTS.md`)与技能(`skills/`)活在文档里,人和 agent 同源读写。
- **Multica agent = 运行的家。** 正向同步把定义投影成 instructions + 挂载的 skills;反向把 Multica 里的改动保真写回文档。
- **同步 = 主动发起的发布。** 两个方向都默认干跑、写后回读,不后台漂移。

## 只同步定义,不同步动态数据

| | 同步 | 说明 |
|---|---|---|
| 入口 `AGENTS.md` | ✅ | → agent instructions(身份/行为准则/路由) |
| `skills/<name>/SKILL.md` + `references/*.md` | ✅ | → 一个 Multica skill(+ 附件),挂载到 agent |
| `dws` 基础技能 | ✅ | 默认挂载(`base_skills`)—— 钉钉原生 Agent 全靠 dws 说话,缺了跑不动 |
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

> 命令里的脚本路径按仓库结构是 `skills/dingtalk-doc-multica-sync/scripts/sync.py`;下面用 `SYNC` 代指它。

**2. 正向(钉钉 → Multica)**

```bash
SYNC=skills/dingtalk-doc-multica-sync/scripts/sync.py
python3 $SYNC pull          # 只物化,看清范围(只读,安全)
python3 $SYNC sync          # 干跑:打印将写入 Multica 的计划,不写
python3 $SYNC sync --yes    # 真写入 + 回读校验 + 完成质检
```

**3. 反向(Multica → 钉钉)** —— 有人在 Multica 里改了定义,写回这份「唯一持久层」

```bash
python3 $SYNC push-dingtalk         # 干跑:打印钉钉侧哪些会被写回
python3 $SYNC push-dingtalk --yes   # 保真写回钉钉源文档
```

之后要更新:改哪边就往另一边同步一次。都是可审阅的主动动作。

## 它怎么工作

**正向 sync 四段:**

1. **pull** — 从 `dingtalk.source_node` 逐层并发 `dws doc list` 递归发现文件夹树,`dws doc read` 拉取入口文档与 `skills/`,**收敛式反解**钉钉 adoc/HTML 实体转义后物化到 `./.agent-workspace`,并记住每份文档的钉钉节点(供反向写回定位)。
2. **push** — 入口文档 → agent **instructions**;每个 `skills/<name>/` → `multica skill create/update`(**name + description 都写全**:无 frontmatter 时从正文提炼一句话描述;create/update 都设,重跑补正老技能空描述)+ 附件 → 连同 `base_skills`(默认 `dws`)挂载;并把知识库根写进 agent 环境变量 `DINGTALK_KB_ROOT`。`.sync-state.json` 记 name→id,重跑是更新而非重复创建。
3. **verify** — `agent get` / `skill get` 回读逐一比对(接口 success 不算数,内容一致才算)。
4. **qc** — 完成质检:instructions 非空、`dws` 已挂载、知识库指针可达、每技能 name/description/content 非空且已挂载。`sync --yes` 末尾自动跑,也能单独 `qc`。任一硬项不过则非零退出。

**反向 push-dingtalk:** 读 Multica 现状 → 与钉钉**容忍重序列化地对比** → 有真漂移才**保真写回**:走钉钉自己的 Markdown 解析器渲成干净 JSONML、写前反棘轮、带 revision 乐观锁、写后三重回读;检测到钉钉侧并发改动则判 `conflict` 拒绝覆盖。`doc read` 限流自动退避重试。

## 不失真:写回钉钉为什么不能直接 `doc update`

裸 markdown 直接写回,钉钉会**重排版**:标题吞行内代码、表格分隔线补齐、加粗边界重写、`_斜体_`↔`*斜体*`、代码块里的 `_`/`*` 被 HTML 实体逐轮转义成**棘轮**(最坏烂到 8 层,正文乱码且再也同步不进)。这些坑在姊妹仓库 `agent-skills/tools/multica_sync.py` 里逐个踩过,本仓库直接沿用其对策:

- **走钉钉的解析器**:临时文档写 markdown → 回读 JSONML → 用这份干净 JSONML 写回,不自己拼。
- **对称反解 + 渲染等价对比**:两侧都反解转义,`_斜体_`↔`*斜体*` 判为一致、免写(不空转 revision)。
- **反棘轮**:写前把行内代码里被转义的 `_`/`*`/`` ` `` 拆干净。
- **乐观锁 + 回读**:带 revision 写,写后回读正文 + JSONML + revision+1;初读后又变了则拒写。

## 知识库指针:双保险

用这个 skill 的 agent 必须知道自己的知识库在哪,否则改了文档同步不回去、也找不到自己的定义源。所以给两处冗余指针:

- **环境变量**:`sync --yes` 自动把 `DINGTALK_KB_ROOT` / `DINGTALK_KB_ENTRY` 写进 agent 的 `custom_env`(机器可靠读到)。
- **AGENTS.md 声明**:入口文档正文里应写明知识库根节点(人和 agent 同源可读)。

`qc` 分别检查两处,硬性要求**至少其一可达**;两处都在最好。

## 怎么用:装进 Multica,形成闭环

这个 skill 不是给人在本地手跑一次就完的 —— 它是**装进 Multica 智能体里的一块**。两种用法互相咬合成一个闭环。

**用法一:创建智能体时,把它加进技能集(主用法)**

先把它导入 Multica 的技能库 —— URL **指向技能自己的目录**(不是仓库根),这样进来的只有 `SKILL.md` + `scripts/`,不夹带仓库的 README/agents.md/LICENSE 等脚手架:

```bash
multica skill import --url https://github.com/d1-2004/dingtalk-doc-multica-sync/tree/main/skills/dingtalk-doc-multica-sync
```

创建智能体时,把 `dingtalk-doc-multica-sync` 加进这个智能体的技能集。之后智能体自己就带着「与钉钉文档节点双向同步定义」的能力:填好 [`agents.md`](./agents.md) 的绑定,跑一次 `sync --yes`,它的 instructions 与技能就从文档拉齐。

**用法二:指导你写 Multica 的 instruction(已经有 multica CLI 时)**

如果你已经在用 multica CLI,这个 skill 同时是一份「怎么把绑定写进 agent instructions」的说明:目标 agent 的 `instructions` 应当**就是钉钉里的入口文档**(`AGENTS.md`)—— 指向哪个钉钉节点、入口是什么、何时触发同步,都在 `agents.md` 与上面「它怎么工作」里写清了。等价的一条命令:

```bash
# 把入口文档(钉钉 AGENTS.md)写成 agent 的 instructions
multica agent update <agent-id> --instructions "$(cat .agent-workspace/AGENTS.md)"
```

**闭环。** 本 skill 既是**挂在智能体上的一个技能**,又是**维护这个智能体定义的工具** —— 它同步的对象,可以正是它自己所在的那个 agent。改钉钉文档 → agent 重新同步 → instructions 与技能一起更新 → 再回到文档。定义、技能、运行三者从此同源自洽。

## 仓库结构(为什么技能在 `skills/` 下)

```
dingtalk-doc-multica-sync/
├── README.md · LICENSE · agents.md   ← 给人看的仓库脚手架 + 绑定配置模板
└── skills/dingtalk-doc-multica-sync/                  ← 干净的技能本体
    ├── SKILL.md
    └── scripts/sync.py
```

技能自成一个隔离目录,`multica skill import` 指向它时**只带 `SKILL.md` + `scripts/`**。若把 `SKILL.md` 放在仓库根,import 会把 `agents.md`/`README.md`/`.gitignore` 也当成技能文件拖进去 —— 那是杂质。

## 设计取舍与坑

- **双向,都保真。** 正向钉钉→Multica,反向 Multica→钉钉;写回走钉钉原生解析器 + 反棘轮 + 乐观锁 + 回读,`_斜体_`↔`*斜体*` 这类重序列化判为一致、免写。
- **主动 > 自动。** 两个方向都不注册定时/触发,默认干跑,`--yes` 才落地。
- **回读定成败。** 接口 `success` 不等于内容真写入;一律回读比对。
- **坏响应不当空目录。** 钉钉 `doc list` 的坏 envelope 被判「列表失败」;`doc read` 限流自动退避重试,不误报漂移。
- **知识库指针双保险。** env 变量 + AGENTS.md 声明,`qc` 硬性要求至少其一可达。
- **dws 默认必挂。** `base_skills` 默认 `["dws"]`,缺了质检报错。
- **技能元数据不留空。** 名字剥掉 `SKILL:` 前缀,描述无 frontmatter 时从正文提炼;create/update 都写,补正老技能空描述。
- **同名 skill 隔离。** 用 `multica.skill_name_prefix` 加前缀避免多 Agent 撞车。
- **完成有质检。** `sync --yes` 末尾自动 `qc`,半成品当场拦下。

## License

MIT

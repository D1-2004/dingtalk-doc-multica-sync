---
name: dingtalk-doc-multica-sync
description: Use when binding a Multica agent to a DingTalk document knowledge-base node and bidirectionally syncing its core definitions (entry AGENTS.md + skills/). Forward publishes DingTalk -> Multica (instructions + mounted skills + dws base skill + KB env var + quality-check); reverse writes Multica edits back to DingTalk losslessly. Actively triggered; dynamic data (memories/storage) is out of scope.
license: MIT
metadata:
  author: d1-2004
  version: '1.1.0'
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

## 双向同步

**正向(钉钉 → Multica):** 钉钉是源,把定义发布到 agent。

```bash
# 0. 填绑定:agents.md 的 ```json 块 —— dingtalk.source_node + multica.agent_id
python3 scripts/sync.py pull          # 只物化,看清范围(只读,安全)
python3 scripts/sync.py sync          # 物化 + 打印计划(默认干跑,不写)
python3 scripts/sync.py sync --yes    # 真写入 + 回读校验 + 完成质检
python3 scripts/sync.py qc            # 随时单独复检 agent 是否完备
```

**反向(Multica → 钉钉):** 有人在 Multica 里改了技能/instructions,把这份「唯一持久层」拉齐。

```bash
python3 scripts/sync.py push-dingtalk        # 干跑:打印钉钉侧哪些会被写回
python3 scripts/sync.py push-dingtalk --yes  # 保真写回钉钉源文档
```

- `pull`：入口文档 + `skills/` 拉到 `./.agent-workspace`,并记住每份文档的钉钉节点(供反向定位)。
- `sync --yes`：入口 → agent **instructions**;每个 `skills/<name>/` 建/更新为 skill(name+description 写全)+ 连同 `dws` 基础技能挂载;写**知识库环境变量**;`verify` 回读;`qc` 质检。
- `push-dingtalk --yes`：读 Multica 现状 → 与钉钉容忍对比 → 有真漂移才**保真写回**(钉钉原生解析器渲 JSONML + 乐观锁 + 回读),并防盖并发编辑。

## 边界(第一阶段,主动触发)

| | 同步 | 说明 |
|---|---|---|
| 入口文档 `AGENTS.md` | ✅ | → Multica agent instructions |
| `skills/<name>/SKILL.md` + `references/*.md` | ✅ | → 一个 Multica skill(+ 附件),挂载到 agent |
| `dws` 基础技能 | ✅ | 默认挂载(`base_skills`);钉钉原生 Agent 靠 dws 说话,缺了跑不动 |
| `memories/` `storage/` `artifacts/` | ❌ | 动态数据/运行状态,不是定义。第二阶段由 DWS 按场景运行时拉取 |
| 二进制/脚本资产(`.py` `.xlsx` …) | ❌ | 非定义;钉钉侧是文件节点,本阶段跳过 |

同步范围由 `agents.md` 的 `sync.include` / `sync.exclude` 决定;基础技能由 `base_skills` 决定,默认即上表。

## 写回不失真(反向的命根子)

裸 markdown 直接 `doc update` 写回钉钉必失真:钉钉会重排版(标题吞行内代码、表格分隔线补齐、加粗边界重写、`_斜体_`↔`*斜体*`、代码块 HTML 实体逐轮转义成棘轮)。对策全部沿用底座 `multica_sync.py` 踩坑后的实现:

- **走钉钉自己的解析器**:在同目录建临时文档 → 写 markdown → 回读 JSONML → 用这份干净 JSONML 写回目标 → 删临时文档。不自己拼 JSONML。
- **写前反棘轮**:把行内代码里被转义的 `_`/`*`/`` ` `` 拆干净,否则一轮轮恶化到乱码、再也同步不进。
- **容忍重序列化的对比**:两侧对称反解 + 渲染等价视图,把「钉钉重排版」和「内容真被改」分开;`_斜体_`↔`*斜体*` 这类判为一致、免写(不空转 revision)。
- **乐观锁 + 回读**:带 revision 写,写后回读正文 + JSONML + revision+1 三重校验;初读后钉钉侧又变了则判 `conflict` 拒绝覆盖(防盖并发编辑)。
- **限流退避**:`doc read` 有函数级限流,批量读命中 `函数触发限流` 自动退避重试,不当漂移误报。

## 关键约束(沿用底座实测教训)

- **主动发起**:本 skill 不创建任何定时/触发自动化。要更新就改文档再手动 `sync --yes` / `push-dingtalk --yes`。
- **dws 默认必挂**:`base_skills` 默认 `["dws"]`,钉钉原生 Agent 全靠它跟钉钉说话;缺了 `qc` 报错。
- **知识库指针双保险**:`sync --yes` 把知识库根写进 agent 环境变量 `DINGTALK_KB_ROOT`;`qc` 再查 `AGENTS.md` 是否也声明了根节点。两处至少其一可达,Agent 才找得到自己的知识库。
- **技能元数据不留空**:name 剥掉 `SKILL:` 前缀;description 无 frontmatter 时从正文首段提炼;create/update 都写全,老技能空描述也补正。
- **完成有质检**:`sync --yes` 末尾自动 `qc`(instructions/dws/知识库指针/每技能 name+description+content+挂载),半成品当场拦。
- **接口 success ≠ 内容真写入**:每次写完都回读比对才算成功(`verify` / 写回后三重回读 已内建)。
- **坏响应不当空目录**:钉钉 `doc list` 的坏 envelope 会被判为「列表失败」而非「空」,拒绝据此漏拉。
- **写前干跑**:`sync` / `push-dingtalk` 默认不写,先打印计划;`--yes` 才落地。
- **skill 去重**:同名 skill 靠 `.sync-state.json`(name→id)记住,重跑是更新而非重复创建;跨 Agent 撞名用 `multica.skill_name_prefix` 隔离。

## 装进 Multica(闭环用法)

导入技能库时**指向本技能自己的目录**(而不是仓库根),这样进来的只有 `SKILL.md` + `scripts/`,不夹带仓库的 README/agents.md 等脚手架:

```bash
multica skill import --url https://github.com/d1-2004/dingtalk-doc-multica-sync/tree/main/skills/dingtalk-doc-multica-sync
```

- **创建智能体时把它加进技能集** —— 智能体自此带着「与钉钉文档节点双向同步定义」的能力。填好 `agents.md`,跑 `sync --yes`。
- **也是一份写 instruction 的说明** —— 目标 agent 的 `instructions` 应当就是钉钉里的入口文档(`AGENTS.md`)。等价命令:`multica agent update <agent-id> --instructions "$(cat .agent-workspace/AGENTS.md)"`。

闭环:本 skill 既是挂在智能体上的一个技能,又是维护这个智能体定义的工具 —— 同步对象可以正是它自己所在的 agent。改文档 → 同步 → 定义更新;在 Multica 改 → 写回文档。两向都保真。

## 绑定配置

见仓库根的 `agents.md` —— 声明「哪份文档创建 Agent、入口是什么、同步到哪个 agent、同步什么、挂哪些基础技能」。

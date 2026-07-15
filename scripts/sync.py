#!/usr/bin/env python3
"""sync.py — 把「钉钉文档知识库节点」→「Multica agent」主动同步一次(仅核心定义)。

一句话:给定一个钉钉文档根节点(Agent 的知识库),把它下面的**入口文档**(默认
`AGENTS.md`)与整棵 `skills/` 子树拉下来,推到一个 Multica agent 上——入口文档
成为 agent 的 instructions,每个 `skills/<name>/` 成为一个挂载到该 agent 的 skill。

设计边界(第一阶段,主动触发):
  - 只同步**定义**:入口文档 + skills 的文本(SKILL.md 及其 references/*.md)。
  - **不同步**动态数据:memories/ storage/ artifacts/ 以及任何二进制/脚本资产。
    这些属于上下文与状态,第二阶段由 DWS 按场景动态拉取,不进这条同步链。
  - 同步是**主动发起**的一次性动作(一条命令),不注册任何定时/触发自动化。

绑定关系写在同步配置文件 `agents.md` 里(见仓库根)。本脚本只读它,不改它。

用法(在填好的 agents.md 所在目录):
  python3 scripts/sync.py pull                 # 只从钉钉物化定义到 ./.agent-workspace(只读侧,安全)
  python3 scripts/sync.py sync                 # 物化 + 打印将要写入 Multica 的计划(默认干跑,不写)
  python3 scripts/sync.py sync --yes           # 真正写入 Multica,并回读校验
  python3 scripts/sync.py verify               # 把 Multica 现状与本地物化定义逐一比对
  # 通用可选项: --config <agents.md> --work-dir <dir> --force(忽略缓存重拉)

铁律(沿用底座实测教训):
  - dws 用 `--format json`,multica 用 `--output json`;返回信封可能嵌 data/result。
  - 坏的 `doc list` 响应绝不能伪装成「空目录」——严格校验 envelope,否则会静默漏拉。
  - 接口 success ≠ 内容真写入:写完一律 `skill get`/`agent get` 回读比对才算成功。
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

POOL = 16


# --------------------------------------------------------------------------- #
#  CLI 定位与调用
# --------------------------------------------------------------------------- #
def _find_bin(name):
    found = shutil.which(name)
    if found:
        return found
    for cand in (
        os.path.expanduser(f"~/.local/bin/{name}"),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/root/.local/bin/{name}",
    ):
        if os.path.exists(cand):
            return cand
    return name  # 保持原样,让「找不到」的报错自然浮出


DWS = _find_bin("dws")
MULTICA = _find_bin("multica")


def _run(binary, args, tail, timeout):
    # errors="replace":dws/multica 输出偶尔在多字节字符中间截断,strict 解码会直接抛异常打断整条链。
    proc = subprocess.run(
        [binary] + args + tail,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    raw = (proc.stdout or "").strip()
    if not raw or not raw.lstrip().startswith(("{", "[")):
        raw = (proc.stderr or "").strip() or raw
    try:
        return json.loads(raw or "null"), proc.returncode
    except Exception:
        start = raw.find("{")
        if start >= 0:
            try:
                return json.loads(raw[start:]), proc.returncode
            except Exception:
                pass
        return {"_raw": raw[:400]}, proc.returncode


def dws(args, timeout=90):
    return _run(DWS, args, ["--format", "json"], timeout)


def multica(args, timeout=90):
    return _run(MULTICA, args, ["--output", "json"], timeout)


def _dig(data, *keys):
    """在可能嵌了 data/result/skill/agent 一层的信封里找第一个非空键。"""
    if not isinstance(data, dict):
        return None
    pools = [data]
    for container in ("result", "data", "skill", "agent"):
        if isinstance(data.get(container), dict):
            pools.append(data[container])
    for pool in pools:
        for key in keys:
            if pool.get(key) not in (None, "", [], {}):
                return pool[key]
    return None


# --------------------------------------------------------------------------- #
#  同步配置(agents.md 里的 ```json 代码块)
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(r"(PUT-|<[A-Z].*?>|-HERE\b)")


class ConfigError(Exception):
    pass


def load_config(path):
    if not os.path.isfile(path):
        raise ConfigError(f"找不到同步配置文件: {path}")
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if not blocks:
        raise ConfigError(
            f"{path} 里没有 ```json 配置块。请把绑定写进第一个 json 代码块。"
        )
    try:
        cfg = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 的 json 配置块解析失败: {exc}")

    dingtalk = cfg.setdefault("dingtalk", {})
    mult = cfg.setdefault("multica", {})
    sync = cfg.setdefault("sync", {})
    dingtalk.setdefault("entry", "AGENTS.md")
    # 基础技能:钉钉原生 Agent 全靠 dws CLI 跟钉钉说话,没有 dws 技能整套都跑不动。
    # 默认给每个同步出来的 agent 挂上 dws;要加别的公共底座技能就往这个列表里加名字。
    mult.setdefault("base_skills", ["dws"])
    sync.setdefault("include", ["AGENTS.md", "skills/**"])
    sync.setdefault("exclude", ["memories/**", "storage/**", "artifacts/**", "tools/**"])

    # 入口文档一定要在 include 里,否则它会被规则挡在门外。
    if dingtalk["entry"] not in sync["include"]:
        sync["include"] = [dingtalk["entry"]] + list(sync["include"])

    source = dingtalk.get("source_node")
    if not source or _PLACEHOLDER.search(str(source)):
        raise ConfigError(
            "dingtalk.source_node 还是占位符。请把它改成 Agent 知识库根节点的真实 nodeId/folderId。"
        )
    dingtalk["source_node"] = _node_id_of(str(source))

    agent_id = mult.get("agent_id")
    if agent_id and _PLACEHOLDER.search(str(agent_id)):
        mult["agent_id"] = None
    return cfg


def _node_id_of(s):
    m = re.search(r"/nodes/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s


# --------------------------------------------------------------------------- #
#  钉钉 markdown 归一化(反解 adoc 转义层)
# --------------------------------------------------------------------------- #
def normalize_dingtalk_markdown(content):
    content = html.unescape(content or "")
    sentinel = "@@DWS_BACKSLASH@@"
    content = content.replace("\\\\", sentinel)
    content = re.sub(r"\\([+\[\]_><{}().$|])", r"\1", content)
    return content.replace(sentinel, "\\")


def _text_equal(a, b):
    def norm(v):
        v = normalize_dingtalk_markdown(v or "").replace("\r\n", "\n")
        return "\n".join(line.rstrip() for line in v.split("\n")).strip()
    return norm(a) == norm(b)


# --------------------------------------------------------------------------- #
#  include / exclude 匹配
# --------------------------------------------------------------------------- #
def _glob_to_regex(pat):
    out = []
    for token in re.split(r"(\*\*/|\*\*|\*|\?)", pat):
        if token == "**/":
            out.append("(?:.*/)?")
        elif token == "**":
            out.append(".*")
        elif token == "*":
            out.append("[^/]*")
        elif token == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(token))
    return "^" + "".join(out) + "$"


def _match_any(rel, patterns):
    return any(re.match(_glob_to_regex(p), rel) for p in patterns)


def _file_included(rel, include, exclude):
    return _match_any(rel, include) and not _match_any(rel, exclude)


def _should_descend(rel, include, exclude):
    if _match_any(rel, exclude):
        return False
    probe = rel + "/__probe__"
    return any(
        re.match(_glob_to_regex(p), probe) or re.match(_glob_to_regex(p), rel)
        for p in include
    )


# --------------------------------------------------------------------------- #
#  拉取(pull):从钉钉物化定义到本地 work_dir
# --------------------------------------------------------------------------- #
def _list_folder_page(fid, page_token):
    for attempt in range(3):
        args = ["doc", "list", "--folder", fid, "--page-size", "50"]
        if page_token:
            args += ["--page-token", page_token]
        d, _ = dws(args)
        payload = d.get("data") if isinstance(d.get("data"), dict) else d
        failed = (
            not isinstance(payload, dict)
            or d.get("success") is False
            or payload.get("success") is False
            or bool(d.get("error"))
            or bool(payload.get("error"))
            or "_raw" in d
        )
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        has_more = payload.get("hasMore") if isinstance(payload, dict) else None
        next_token = (payload.get("nextPageToken") if isinstance(payload, dict) else None) \
            or d.get("nextPageToken")
        valid = (
            not failed
            and isinstance(nodes, list)
            and isinstance(has_more, bool)
            and (not has_more or (isinstance(next_token, str) and next_token))
        )
        if valid:
            return nodes, has_more, next_token or ""
        time.sleep(1.0 * (attempt + 1))
    return None  # 坏响应:让调用方标记「列表失败」,绝不当成空目录


def _list_folder(fid):
    children, token, seen = [], None, set()
    for _ in range(1000):
        page = _list_folder_page(fid, token)
        if page is None:
            return None
        nodes, more, token = page
        children.extend(nodes)
        if not more:
            return children
        if token in seen:
            return None
        seen.add(token)
    return None


def _leaf_name(node):
    name = node.get("name") or ""
    ext = (node.get("extension") or "").lower()
    if ext and ext != "adoc" and not name.lower().endswith("." + ext):
        return f"{name}.{ext}"
    return name


def discover(root, include, exclude):
    """BFS 发现 work 范围内的叶子文档,并记录被有意跳过的动态节点与二进制。"""
    files, folders, truncated = [], [], []
    skipped_dynamic, skipped_binary = [], []
    level = [("", root)]
    while level:
        fids = [fid for _, fid in level]
        with ThreadPoolExecutor(max_workers=min(POOL, len(fids))) as ex:
            listings = list(ex.map(_list_folder, fids))
        nxt = []
        for (prefix, _), children in zip(level, listings):
            if children is None:
                truncated.append((prefix or "/") + " (列表失败)")
                continue
            for child in children:
                rel = os.path.join(prefix, _leaf_name(child)).replace(os.sep, "/")
                is_folder = child.get("nodeType") == "folder"
                if is_folder:
                    if _should_descend(rel, include, exclude):
                        folders.append(rel)
                        nxt.append((rel, child["nodeId"]))
                    else:
                        skipped_dynamic.append(rel + "/")
                    continue
                # 文件节点
                if not _file_included(rel, include, exclude):
                    skipped_dynamic.append(rel)
                    continue
                if child.get("contentType") not in (None, "ALIDOC"):
                    skipped_binary.append(rel)  # 脚本/附件等资产:非定义,第一阶段不同步
                    continue
                files.append((rel, child))
        level = nxt
    discover.skipped_dynamic = sorted(set(skipped_dynamic))
    discover.skipped_binary = sorted(set(skipped_binary))
    return files, folders, truncated


def _fetch(item):
    rel, node = item
    ext = (node.get("extension") or "").lower()
    if node.get("contentType") == "ALIDOC" and ext == "adoc":
        back, _ = dws(["doc", "read", "--node", node["nodeId"]])
        if back.get("error") or "markdown" not in back:
            return {"rel": rel, "content": None, "error": "doc read failed"}
        return {"rel": rel, "content": normalize_dingtalk_markdown(back["markdown"])}
    return {"rel": rel, "content": None, "error": f"非在线文档({ext or '?'}),跳过"}


def pull(cfg, work_dir, force):
    root = cfg["dingtalk"]["source_node"]
    include, exclude = cfg["sync"]["include"], cfg["sync"]["exclude"]

    if force and os.path.isdir(work_dir):
        shutil.rmtree(work_dir)

    print(f"[pull] 钉钉根节点 {root} → {work_dir}")
    files, folders, truncated = discover(root, include, exclude)

    with ThreadPoolExecutor(max_workers=min(POOL, max(1, len(files)))) as ex:
        results = list(ex.map(_fetch, files))
    # 定点重试:失败的逐个再取一次(治瞬时抖动)
    by_rel = {rel: it for rel, it in ((it[0], it) for it in files)}
    for r in results:
        if r.get("error") and "非在线" not in r["error"]:
            time.sleep(0.8)
            again = _fetch(by_rel[r["rel"]])
            if not again.get("error"):
                r.update(again)
                r.pop("error", None)

    os.makedirs(work_dir, exist_ok=True)
    for folder in folders:
        os.makedirs(os.path.join(work_dir, folder), exist_ok=True)
    written = 0
    for r in results:
        if r.get("content") is None:
            if r.get("error"):
                truncated.append(r["rel"] + f" ({r['error']})")
            continue
        path = os.path.join(work_dir, r["rel"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        Path(path).write_text(r["content"], encoding="utf-8")
        written += 1

    entry = cfg["dingtalk"]["entry"]
    entry_ok = os.path.isfile(os.path.join(work_dir, entry))
    print(f"  发现 {len(folders)} 文件夹 / {len(files)} 定义文档,写入 {written} 份")
    print(f"  入口文档 {entry}: {'✓ 已物化' if entry_ok else '✗ 缺失'}")
    if discover.skipped_dynamic:
        print(f"  跳过动态数据(不同步,留给 DWS 第二阶段): {discover.skipped_dynamic}")
    if discover.skipped_binary:
        print(f"  跳过二进制/脚本资产(非定义): {discover.skipped_binary}")
    if truncated:
        print(f"  ⚠️ 本次物化不完整: {truncated}")
    if not entry_ok:
        raise ConfigError(f"入口文档 {entry} 没拉到,拒绝继续。核对 source_node 与 entry。")
    return {"written": written, "truncated": truncated}


# --------------------------------------------------------------------------- #
#  推送(push):本地定义 → Multica
# --------------------------------------------------------------------------- #
def _frontmatter(md):
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", md, re.DOTALL)
    fields = {}
    if m:
        for line in m.group(1).split("\n"):
            fm = re.match(r"\s*([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$", line)
            if fm:
                fields[fm.group(1)] = fm.group(2).strip().strip("'\"")
    return fields


def _skill_dirs(work_dir):
    skills_root = Path(work_dir) / "skills"
    if not skills_root.is_dir():
        return []
    out = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        out.append(skill_md.parent)
    return out


def _derive_description(md, max_len=200):
    """没有 frontmatter description 时,从正文提炼一句话描述。
    钉钉侧 SKILL.md 一般是 `# SKILL: <名>` + 一段说明,没有 YAML 头。取第一段实义散文
    (跳过标题/子标题/引用/表格/代码块/列表符/强调符),压成一行、截断,作为标准描述字段。"""
    body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n?", "", md, flags=re.DOTALL)
    para, started, in_fence = [], False, False
    for raw in body.split("\n"):
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            if started:
                break            # 第一段结束
            continue
        if line.startswith(("#", ">", "|", "<!--", "![", "---", "===")):
            continue
        text = re.sub(r"^[\-\*\d\.\)、\s]+", "", line)   # 去列表/序号符
        text = re.sub(r"[*`_#>]", "", text).strip()      # 去强调/标记符
        if not text:
            continue
        para.append(text)
        started = True
    desc = re.sub(r"\s+", " ", " ".join(para)).strip()
    if len(desc) > max_len:
        desc = desc[:max_len].rstrip("，,。.、 ") + "…"
    return desc


def _clean_skill_name(name):
    """标准化技能名:剥掉 `SKILL:` / `SKILL：` 前缀,防止把「SKILL: 校招生成长日志」当成名字。"""
    return re.sub(r"^\s*SKILL\s*[:：]\s*", "", name or "").strip()


def _skill_identity(skill_dir, work_dir, prefix):
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(md)
    name = _clean_skill_name(fm.get("name") or skill_dir.name)
    if prefix:
        name = f"{prefix}{name}"
    desc = fm.get("description") or _derive_description(md) or name
    return name, desc


def _resolve_by_name(name):
    """按名字在 workspace 技能库里找一个已存在的技能(用于 dws 等基础技能)。"""
    listing, _ = multica(["skill", "list"])
    items = listing if isinstance(listing, list) else _dig(listing, "skills") or []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("name") == name:
            return it.get("id")
    return None


def _state_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), ".sync-state.json")


def _load_state(config_path):
    p = _state_path(config_path)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"skills": {}}


def _save_state(config_path, state):
    Path(_state_path(config_path)).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _resolve_skill_id(name, cfg, state):
    pins = (cfg.get("multica") or {}).get("skills") or {}
    if name in pins:
        return pins[name], "pinned"
    if name in state.get("skills", {}):
        return state["skills"][name], "state"
    listing, _ = multica(["skill", "list"])
    items = listing if isinstance(listing, list) else _dig(listing, "skills") or []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("name") == name:
            return it.get("id"), "adopted"
    return None, "new"


def push(cfg, work_dir, config_path, dry_run):
    mult = cfg.get("multica") or {}
    agent_id = mult.get("agent_id")
    if not agent_id:
        raise ConfigError(
            "multica.agent_id 未设置。先创建/指定目标 agent:\n"
            "  multica agent create --name \"<Agent 名>\" --runtime-id <runtime> --output json\n"
            "把返回的 id 写进 agents.md 的 multica.agent_id。"
        )
    prefix = mult.get("skill_name_prefix", "")
    base_names = mult.get("base_skills", ["dws"])
    state = _load_state(config_path)

    entry = cfg["dingtalk"]["entry"]
    entry_content = (Path(work_dir) / entry).read_text(encoding="utf-8")
    skill_dirs = _skill_dirs(work_dir)

    plan = []
    plan.append(("agent.instructions", f"← {entry} ({len(entry_content)} 字)"))
    for sd in skill_dirs:
        name, desc = _skill_identity(sd, work_dir, prefix)
        sid, origin = _resolve_skill_id(name, cfg, state)
        refs = [p for p in sorted(sd.rglob("*.md")) if p.name != "SKILL.md"]
        plan.append((f"skill:{name}",
                     f"{'update' if sid else 'create'}({origin}) + {len(refs)} 附件 "
                     f"| desc: {desc[:32]}…" if len(desc) > 32 else
                     f"{'update' if sid else 'create'}({origin}) + {len(refs)} 附件 | desc: {desc}"))
    for bn in base_names:
        bid = _resolve_by_name(bn)
        plan.append((f"base-skill:{bn}", "挂载" if bid else "⚠️ workspace 未找到,无法挂载"))

    print(f"[push] 目标 Multica agent {agent_id}" + (" —— 干跑(不写入)" if dry_run else ""))
    for label, detail in plan:
        print(f"  {'· ' if dry_run else '→ '}{label}: {detail}")
    if dry_run:
        print("  (加 --yes 才会真正写入 Multica)")
        return {"synced": [], "base": []}

    fail = 0
    # 1) 入口文档 → agent instructions
    _, rc = multica(["agent", "update", agent_id, "--instructions", entry_content])
    print(f"  agent.instructions ← {entry}: {'✓' if rc == 0 else '✗'}")
    fail += rc != 0

    # 2) 每个 skills/<name>/ → 一个 skill(+ 附件),并挂载。
    #    create 与 update 都写全 name+description —— 标准 SKILL 同步范式:名字与描述都得对,
    #    重跑也把老技能的空描述补正过来(只更 content 会让描述永远空着)。
    mounted_ids, synced = [], []
    for sd in skill_dirs:
        name, desc = _skill_identity(sd, work_dir, prefix)
        skill_md = str(sd / "SKILL.md")
        sid, _ = _resolve_skill_id(name, cfg, state)
        if sid:
            _, rc = multica(["skill", "update", sid, "--name", name,
                             "--description", desc, "--content-file", skill_md])
        else:
            out, rc = multica(["skill", "create", "--name", name,
                               "--description", desc, "--content-file", skill_md])
            sid = _dig(out, "id")
        if not sid or rc != 0:
            print(f"  skill:{name}: 创建/更新失败 ✗")
            fail += 1
            continue
        state.setdefault("skills", {})[name] = sid
        # 附件(references/*.md 等)按相对 skill 目录的路径 upsert
        for ref in sorted(sd.rglob("*.md")):
            if ref.name == "SKILL.md":
                continue
            rel = ref.relative_to(sd).as_posix()
            _, rc = multica(["skill", "files", "upsert", sid, "--path", rel,
                             "--content-file", str(ref)])
            fail += rc != 0
        mounted_ids.append(sid)
        synced.append((name, sid))
        print(f"  skill:{name} ({sid}): ✓  desc={desc[:40]}")

    # 3) 基础技能:dws 是钉钉原生 Agent 的命根子,默认挂上。缺了 QC 会报。
    base = []
    for bn in base_names:
        bid = _resolve_by_name(bn)
        if bid:
            base.append((bn, bid))
            mounted_ids.append(bid)
        else:
            print(f"  ⚠️ base skill {bn}: workspace 未找到,无法挂载(QC 会标记为缺失)")

    # 4) 挂载(add 不替换已有挂载,重复挂同一 id 不会产生副本)
    if mounted_ids:
        _, rc = multica(["agent", "skills", "add", agent_id,
                         "--skill-ids", ",".join(mounted_ids)])
        print(f"  mount {len(synced)} 同步技能 + {len(base)} 基础技能 → agent: "
              f"{'✓' if rc == 0 else '✗'}")
        fail += rc != 0

    _save_state(config_path, state)
    print(f"[push] 完成,失败 {fail} 项")
    if fail:
        raise SystemExit(1)
    return {"synced": synced, "base": base}


# --------------------------------------------------------------------------- #
#  校验(verify):Multica 现状 vs 本地定义
# --------------------------------------------------------------------------- #
def verify(cfg, work_dir, config_path):
    agent_id = (cfg.get("multica") or {}).get("agent_id")
    if not agent_id:
        raise ConfigError("multica.agent_id 未设置,无从校验。")
    prefix = (cfg.get("multica") or {}).get("skill_name_prefix", "")
    state = _load_state(config_path)
    results = []

    entry = cfg["dingtalk"]["entry"]
    entry_content = (Path(work_dir) / entry).read_text(encoding="utf-8")
    agent_out, _ = multica(["agent", "get", agent_id])
    results.append(("agent.instructions",
                    _text_equal(_dig(agent_out, "instructions"), entry_content)))

    for sd in _skill_dirs(work_dir):
        name, _ = _skill_identity(sd, work_dir, prefix)
        sid, _ = _resolve_skill_id(name, cfg, state)
        if not sid:
            results.append((f"skill:{name}", False))
            continue
        skill_out, _ = multica(["skill", "get", sid])
        local_md = (sd / "SKILL.md").read_text(encoding="utf-8")
        results.append((f"skill:{name}:SKILL.md",
                        _text_equal(_dig(skill_out, "content"), local_md)))
        remote_files = {f.get("path"): f.get("content", "")
                        for f in (_dig(skill_out, "files") or [])
                        if isinstance(f, dict)}
        for ref in sorted(sd.rglob("*.md")):
            if ref.name == "SKILL.md":
                continue
            rel = ref.relative_to(sd).as_posix()
            results.append((f"skill:{name}:{rel}",
                            _text_equal(remote_files.get(rel), ref.read_text(encoding="utf-8"))))

    bad = 0
    print(f"[verify] agent {agent_id}")
    for label, ok in results:
        print(f"  {label}: {'一致 ✓' if ok else '不一致 ✗'}")
        bad += not ok
    print(f"[verify] {'全部一致' if not bad else f'{bad} 项不一致'}")
    return bad


# --------------------------------------------------------------------------- #
#  质检(qc):完成阶段的合规/完备性检查
# --------------------------------------------------------------------------- #
def quality_check(cfg, work_dir, config_path):
    """同步完成后的质检:这个 agent 是不是一个合规、完备的智能体。
    只读,既在 sync --yes 末尾自动跑,也能单独 `qc` 随时复检。"""
    mult = cfg.get("multica") or {}
    agent_id = mult.get("agent_id")
    if not agent_id:
        raise ConfigError("multica.agent_id 未设置,无从质检。")
    prefix = mult.get("skill_name_prefix", "")
    base_names = mult.get("base_skills", ["dws"])
    state = _load_state(config_path)

    agent_out, _ = multica(["agent", "get", agent_id])
    instructions = _dig(agent_out, "instructions") or ""
    mounted = _dig(agent_out, "skills") or []
    mounted_ids = {m.get("id") for m in mounted if isinstance(m, dict)}
    mounted_names = {m.get("name") for m in mounted if isinstance(m, dict)}

    checks = [("instructions 非空", len(instructions.strip()) > 0, f"{len(instructions)} 字")]

    # 基础技能(dws)必须挂上,否则 Agent 调不动钉钉
    for bn in base_names:
        bid = _resolve_by_name(bn)
        ok = (bid in mounted_ids) or (bn in mounted_names)
        checks.append((f"基础技能 {bn} 已挂载", ok,
                       "" if ok else "缺失 → 该 Agent 无法调用钉钉,会出问题"))

    # 每个同步技能:name / description / content 非空,且已挂载
    skill_dirs = _skill_dirs(work_dir)
    no_name, no_desc, empty_content, not_mounted = [], [], [], []
    for sd in skill_dirs:
        name, _ = _skill_identity(sd, work_dir, prefix)
        sid, _ = _resolve_skill_id(name, cfg, state)
        if not sid:
            no_name.append(name)
            not_mounted.append(name)
            continue
        so, _ = multica(["skill", "get", sid])
        if not (_dig(so, "name") or "").strip():
            no_name.append(name)
        if not (_dig(so, "description") or "").strip():
            no_desc.append(name)
        if not (_dig(so, "content") or "").strip():
            empty_content.append(name)
        if sid not in mounted_ids and name not in mounted_names:
            not_mounted.append(name)
    n = len(skill_dirs)
    checks.append((f"{n} 个技能 name 非空", not no_name, "，".join(no_name)))
    checks.append((f"{n} 个技能 description 非空", not no_desc, "，".join(no_desc)))
    checks.append((f"{n} 个技能 content 非空", not empty_content, "，".join(empty_content)))
    checks.append((f"{n} 个技能已挂载到 agent", not not_mounted, "，".join(not_mounted)))

    bad = 0
    print(f"[qc] 质检 agent {agent_id}")
    for label, ok, detail in checks:
        tail = f"  ({detail})" if detail else ""
        print(f"  {label}: {'✓' if ok else '✗'}{tail}")
        bad += not ok
    print(f"[qc] {'质检通过,Agent 完备' if not bad else f'⚠️ {bad} 项待修'}")
    return bad


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="钉钉文档节点 → Multica agent 主动同步")
    ap.add_argument("command", nargs="?", default="sync",
                    choices=["pull", "sync", "verify", "qc"],
                    help="pull=只物化; sync=物化+推送(默认干跑,--yes 才写); "
                         "verify=回读比对; qc=完成质检")
    ap.add_argument("--config", default="agents.md", help="同步配置文件(默认 ./agents.md)")
    ap.add_argument("--work-dir", default="./.agent-workspace", help="定义物化目录")
    ap.add_argument("--yes", action="store_true", help="sync 时真正写入 Multica")
    ap.add_argument("--force", action="store_true", help="忽略已物化目录,清空重拉")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        if args.command == "verify":
            sys.exit(1 if verify(cfg, args.work_dir, args.config) else 0)
        if args.command == "qc":
            sys.exit(1 if quality_check(cfg, args.work_dir, args.config) else 0)
        pull(cfg, args.work_dir, args.force or args.command == "sync")
        if args.command == "pull":
            return
        push(cfg, args.work_dir, args.config, dry_run=not args.yes)
        if args.yes:
            bad_v = verify(cfg, args.work_dir, args.config)
            bad_q = quality_check(cfg, args.work_dir, args.config)
            sys.exit(1 if (bad_v or bad_q) else 0)
    except ConfigError as exc:
        print(f"配置/前置错误: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

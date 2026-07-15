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
import tempfile
import time
import uuid
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
    try:
        proc = subprocess.run(
            [binary] + args + tail,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # 单节点超时不该以未捕获异常炸掉整条同步:返回哨兵,让退避/truncated 标记接管。
        return {"error": {"message": "timeout", "kind": "timeout"}}, -1
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


_RATE_LIMIT_RE = re.compile(r"rate.?limit|限流", re.I)


def dws(args, timeout=90):
    # 钉钉对 doc read 有函数级限流:批量反向同步一口气读几十个节点必被 `函数触发限流` 挡。
    # 命中限流就退避重试,别把一次限流当成「读取失败」误报漂移。限流文本可能落在 error 也可能落在
    # _raw 里,所以扫整个响应 blob,不只看 error 键。
    parsed, rc = _run(DWS, args, ["--format", "json"], timeout)
    for attempt in range(3):
        blob = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else ""
        if not _RATE_LIMIT_RE.search(blob):
            break
        time.sleep(1.5 * (attempt + 1))
        parsed, rc = _run(DWS, args, ["--format", "json"], timeout)
    # dws 的响应恒为对象:None / JSON 数组一律包成 {"_raw":...},免得下游 `out.get(...)` 崩。
    if not isinstance(parsed, dict):
        parsed = {"_raw": str(parsed)[:400]}
    return parsed, rc


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
#  钉钉 markdown 保真:归一化 + 容忍钉钉重序列化的对比 + 反棘轮
#  —— 直接沿用 agent-skills/tools/multica_sync.py 踩过一堆坑之后的实现,别重犯:
#     钉钉会重排版(标题吞行内代码、表格分隔线补齐、加粗边界重写、段落合并、`_` 转义、
#     代码块里 HTML 实体【双重】转义并逐轮恶化成棘轮)。逐行 diff 必假报,裸 markdown
#     写回必失真。对策:两侧对称反解 + 渲染等价对比 + 写回走钉钉自己的解析器。
# --------------------------------------------------------------------------- #
def _normalize_source_markdown(value):
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+$", "", ln) for ln in value.split("\n")]
    return "\n".join(lines).strip()


def _unescape_adoc(value):
    """反解一层 adoc 转义(\\_ \\* \\| ...),保留源文件里真正的反斜杠。"""
    sentinel = chr(0) + "DWS_BS" + chr(0)   # 空字节哨兵:正文不可能出现,零碰撞
    value = (value or "").replace("\\\\", sentinel)
    value = re.sub(r"\\([+\[\]_><{}().$|])", r"\1", value)
    return value.replace(sentinel, "\\")


def _html_unescape_fully(value):
    """钉钉在代码块里会【双重】HTML 转义(`&amp;#95;` = `_`),一次 unescape 解不完。
    收敛式反解到不再变化为止(设上限防病态输入死循环)。"""
    v = value or ""
    for _ in range(5):
        nxt = html.unescape(v)
        if nxt == v:
            break
        v = nxt
    return v


def normalize_dingtalk_markdown(value):
    """把钉钉回读的 markdown 反解成干净源码 —— 物化到本地/推给 Multica 前都过这道。
    旧实现只 html.unescape 一次,会留下 `&#95;` 残渣(失真);现在收敛反解 + 对称去 adoc 转义。"""
    return _normalize_source_markdown(_unescape_adoc(_html_unescape_fully(value)))


def _render_equivalent(value):
    """渲染等价视图:折叠钉钉自动 linkify 的 [文本](节点URL)、去行内强调符、归一化表格
    分隔线与空白 —— 把「钉钉重排版」这类表现层差异和「内容真被改」区分开。"""
    v = re.sub(r"[（(]?\[打开\]\(https://alidocs\.dingtalk\.com/[^)]*\)[)）]?", "", value or "")
    v = re.sub(r"\[([^\]]*)\]\(https://alidocs\.dingtalk\.com/[^)]*\)", r"\1", v)
    # 钉钉把 _斜体_ 归一成 *斜体*:只归一「非词内」的成对下划线强调,不碰 snake_case / CLI 标识 —
    # 否则 user_id→userid、run_in_background→runinbackground 这类**真改动**会被误判成等价(假阴,漏写回)。
    v = re.sub(r"(?<![0-9A-Za-z])_(\S(?:[^_\n]*\S)?)_(?![0-9A-Za-z])", r"*\1*", v)
    v = re.sub(r"\*{1,3}", "", v)          # 钉钉会重排加粗/斜体边界
    v = re.sub(r"^\s*>\s?", "", v)         # 钉钉会丢引用块的 > 前缀
    if re.match(r"^[\s|:\-]+$", v):        # 仅表格分隔行才折叠连字符(|----|→|---|),不碰 --flag/代码里的 -
        v = re.sub(r"-{2,}", "-", v)
    v = re.sub(r"[ \t]+", "", v)
    return v


def _markdown_content_lines(value):
    """比对时容忍钉钉对外层空行的压缩(代码块内原样保留)。"""
    lines, fence = [], None
    for line in value.split("\n"):
        stripped = line.lstrip()
        marker = next((m for m in ("```", "~~~") if stripped.startswith(m)), None)
        if marker:
            lines.append(line)
            fence = None if fence == marker else marker
        elif line.strip() or fence:
            lines.append(line)
    return tuple(lines)


def dingtalk_matches_source(remote, source):
    """True=严格一致 / "render"=仅表现层差异 / False=真漂移。两侧都反解,避免源文件里
    一个 \\_ 就永久假报漂移。"""
    r = _markdown_content_lines(normalize_dingtalk_markdown(remote))
    s = _markdown_content_lines(normalize_dingtalk_markdown(source))
    if r == s:
        return True
    if "".join(_render_equivalent(x) for x in r) == "".join(_render_equivalent(x) for x in s):
        return "render"
    return False


def _unratchet(md):
    """拆 HTML 实体棘轮:钉钉把行内代码里的 `_`/`*`/反引号转义,回读再写回会一层层恶化
    (最坏烂到 8 层 → 正文乱码且再也同步不进)。写回前一律拆干净。"""
    md = re.sub(r"&(?:amp;)*#42;", "*", md)
    md = re.sub(r"&(?:amp;)*#95;", "_", md)
    md = re.sub(r"&(?:amp;)*#96;", "`", md)
    md = re.sub(r"`\*\*([^`\n]+?)\*\*`", r"`\1`", md)
    return md


def _text_equal(a, b):
    """Multica 侧内容(非钉钉,不会重序列化)与本地物化的一致判断:只容忍空白/换行差异。"""
    return _normalize_source_markdown(a) == _normalize_source_markdown(b)


# --------------------------------------------------------------------------- #
#  往钉钉写回(Multica → 钉钉):保真写入 = 钉钉原生解析器渲 JSONML + 乐观锁 + 回读
# --------------------------------------------------------------------------- #
def _read_dingtalk_doc(node_id):
    """读钉钉正文 + revision(乐观锁用)。返回 (markdown, revision, error)。"""
    ver, vrc = dws(["doc", "read", "--node", node_id, "--content-format", "jsonml"])
    revision = ver.get("revision") if isinstance(ver, dict) else None
    if vrc != 0 or not revision or ver.get("error"):
        return None, None, (ver.get("error") or ver.get("_raw") or vrc)
    out, rc = dws(["doc", "read", "--node", node_id])
    if rc == 0 and not out.get("error") and "markdown" in out:
        return out["markdown"], revision, None
    return None, None, (out.get("error") or out.get("_raw") or rc)


def _read_dingtalk_markdown(node_id):
    """只读正文 markdown(不取 revision)—— 比对阶段够用,读一次而非两次,减半限流压力。"""
    out, rc = dws(["doc", "read", "--node", node_id])
    if rc == 0 and not out.get("error") and "markdown" in out:
        return out["markdown"], None
    return None, (out.get("error") or out.get("_raw") or rc)


def _read_dingtalk_jsonml(node_id):
    out, rc = dws(["doc", "read", "--node", node_id, "--content-format", "jsonml"])
    if rc == 0 and not out.get("error") and "jsonml" in out:
        return out["jsonml"], None
    return None, (out.get("error") or out.get("_raw") or rc)


def _render_markdown_jsonml(target_node, content):
    """用钉钉自己的 Markdown 解析器把 content 渲成「干净可回写」的 JSONML:
    在目标同目录建临时文档 → 写 markdown → 回读 JSONML → 删临时文档。
    绕开「裸 markdown 直接写回被重排版失真」。返回 (jsonml, error)。"""
    content = _unratchet(content)
    if len(content) > 10000:
        # 钉钉单篇文档硬顶 1 万字符,超了 doc update 直接被拒。快速失败给准确错因,
        # 省掉 3 轮无效临时文档往返。
        return None, f"内容 {len(content)} 字超钉钉单档 1 万字符上限,拒写(建议拆节点)"
    info, irc = dws(["doc", "info", "--node", target_node])
    folder_id = _dig(info, "folderId")
    if irc != 0 or not folder_id or info.get("error"):
        return None, "目标目录查询失败"
    scratch_name = "dtsync-render-" + uuid.uuid4().hex
    scratch, rendered, error, tmp = None, None, None, None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as f:
            f.write(content)
            tmp = f.name
        created, _ = dws(["doc", "create", "--name", scratch_name, "--folder", folder_id, "--yes"])
        scratch = _dig(created, "nodeId", "dentryUuid", "fileId")
        # 铁律:doc create 会【报错但其实建成功】—— 不看信封,按名回查
        for attempt in range(3):
            if scratch:
                break
            time.sleep(0.8 * (attempt + 1))
            listed, lrc = dws(["doc", "list", "--folder", folder_id])
            payload = listed.get("data") if isinstance(listed.get("data"), dict) else listed
            if lrc == 0 and isinstance(payload, dict):
                scratch = next((it.get("nodeId") for it in (payload.get("nodes") or [])
                                if it.get("name") == scratch_name), None)
        if not scratch:
            return None, "临时文档创建无返回"
        matched = False
        for attempt in range(3):
            dws(["doc", "update", "--node", scratch, "--content-file", tmp,
                 "--content-format", "markdown", "--mode", "overwrite", "--yes"])
            back, brc = dws(["doc", "read", "--node", scratch])
            matched = (brc == 0 and not back.get("error")
                       and dingtalk_matches_source(back.get("markdown", ""), content) is not False)
            if matched:
                break
            time.sleep(0.8 * (attempt + 1))
        if not matched:
            error = "临时文档 markdown 回读不匹配"
        else:
            tree, trc = dws(["doc", "read", "--node", scratch, "--content-format", "jsonml"])
            if trc != 0 or tree.get("error") or not tree.get("jsonml"):
                error = "临时文档 JSONML 读取失败"
            else:
                rendered = tree["jsonml"]
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        if scratch:
            d, drc = dws(["doc", "delete", "--node", scratch, "--yes"])
            if drc != 0 or d.get("error") or d.get("success") is False:
                # 清理失败只告警:渲染本身与删临时文档相互独立,不能因删不掉而否决一次成功的写回。
                # 残留的 dtsync-render-* 会被 discover 过滤,不进 node_map。
                print(f"  ⚠️ 临时渲染文档未清理,残留(不影响本次写回): {scratch}")
    return rendered, error


def dingtalk_write_decision(baseline, latest, expected):
    """写/免写/冲突:防止盖掉钉钉侧的并发编辑,且语义已一致(含表现层等价)就不重复写。
    用容忍钉钉重序列化的对比,避免 `_斜体_`↔`*斜体*` 这类等价差异被判成「要写」而空转 revision。"""
    if dingtalk_matches_source(latest, expected) is not False:
        return "already"                 # 已(语义)一致,免写
    if dingtalk_matches_source(latest, baseline) is False:
        return "conflict"                # 初读后钉钉侧又被改了,别覆盖
    return "write"


def _write_dingtalk_node(node_id, expected_content, baseline_remote):
    """把 expected_content 保真写回钉钉 node。返回 (status, msg):
    status ∈ already / written / conflict / failed。"""
    rendered, rerr = _render_markdown_jsonml(node_id, expected_content)
    if rerr:
        return "failed", f"Markdown→JSONML 失败: {rerr}"
    latest, revision, read_err = _read_dingtalk_doc(node_id)
    if read_err:
        return "failed", f"写前复读失败: {read_err}"
    decision = dingtalk_write_decision(baseline_remote, latest, expected_content)
    if decision == "already":
        return "already", "已一致"
    if decision == "conflict":
        return "conflict", "初读后钉钉侧又变了,拒绝覆盖(防丢并发编辑)"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump({"jsonml": rendered}, f, ensure_ascii=False)
            tmp = f.name
        out, rc = dws(["doc", "update", "--node", node_id, "--content-file", tmp,
                       "--content-format", "jsonml", "--mode", "overwrite",
                       "--revision", str(revision), "--yes"])
        updated = rc == 0 and not out.get("error")
        back, after_rev, berr = _read_dingtalk_doc(node_id)
        content_ok = not berr and dingtalk_matches_source(back or "", expected_content) is not False
        back_tree, terr = _read_dingtalk_jsonml(node_id)
        jsonml_ok = not terr and back_tree == rendered
        try:
            rev_ok = int(after_rev) == int(revision) + 1
        except (TypeError, ValueError):
            rev_ok = False
        ok = updated and content_ok and jsonml_ok and rev_ok
        return ("written" if ok else "failed",
                f"revision {revision}→{after_rev}, jsonml={'exact' if jsonml_ok else 'drift'}, "
                f"content={'ok' if content_ok else 'drift'}")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


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
    """BFS 发现 work 范围内的叶子文档,并记录被有意跳过的动态节点与二进制。
    同时收集文件夹 rel→folderId(供 Multica→钉钉 写回时定位/新建节点)。"""
    files, folders, truncated = [], [], []
    folder_ids = {"": root}
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
                        folder_ids[rel] = child["nodeId"]
                        nxt.append((rel, child["nodeId"]))
                    else:
                        skipped_dynamic.append(rel + "/")
                    continue
                # 文件节点
                if (child.get("name") or "").startswith("dtsync-render-"):
                    continue  # 写回时临时渲染文档的清理残留,不是定义,别进 node_map
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
    discover.folder_ids = folder_ids
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


def pull(cfg, work_dir, force, config_path=None):
    root = cfg["dingtalk"]["source_node"]
    include, exclude = cfg["sync"]["include"], cfg["sync"]["exclude"]

    if force and os.path.isdir(work_dir):
        shutil.rmtree(work_dir)

    print(f"[pull] 钉钉根节点 {root} → {work_dir}")
    files, folders, truncated = discover(root, include, exclude)

    with ThreadPoolExecutor(max_workers=min(POOL, max(1, len(files)))) as ex:
        results = list(ex.map(_fetch, files))
    # 定点重试:失败的逐个再取一次(治瞬时抖动)
    by_rel = {item[0]: item for item in files}
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

    # 记住 rel→钉钉节点 的映射 —— Multica→钉钉 写回时靠它定位每份文档写去哪。
    if config_path:
        node_map = {rel: node.get("nodeId") for rel, node in files if node.get("nodeId")}
        state = _load_state(config_path)
        state["dingtalk"] = {
            "root": root,
            "entry": entry,
            "entry_node": node_map.get(entry),
            "nodes": node_map,
            "folders": discover.folder_ids,
        }
        _save_state(config_path, state)
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


_SKILL_LIST_CACHE = None


def _skill_list():
    """进程内缓存整份 workspace 技能列表(push/qc 会多次按名查共享基础技能,拉一次即可)。"""
    global _SKILL_LIST_CACHE
    if _SKILL_LIST_CACHE is None:
        listing, _ = multica(["skill", "list"])
        items = listing if isinstance(listing, list) else _dig(listing, "skills") or []
        _SKILL_LIST_CACHE = items if isinstance(items, list) else []
    return _SKILL_LIST_CACHE


def _resolve_by_name(name):
    """按名字在 workspace 技能库里找一个已存在的**共享**技能(仅用于 dws 等基础技能)。"""
    for it in _skill_list():
        if isinstance(it, dict) and it.get("name") == name:
            return it.get("id")
    return None


def _mounted_map(agent_out):
    """agent get 的挂载技能列表 → {name: id}(用于「只在本 agent 已挂载范围内」按名认领)。"""
    return {m.get("name"): m.get("id")
            for m in (_dig(agent_out, "skills") or [])
            if isinstance(m, dict) and m.get("name")}


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


def _resolve_skill_id(name, cfg, state, own=None):
    """解析技能 name→id。顺序:配置 pin → 本 config 的 .sync-state → **本 agent 已挂载的同名技能**。
    绝不做 workspace 全量按名认领 —— 否则两个 Agent 各有一个同名技能目录(如 `log`)时,B 会认领并
    覆盖 A 的技能、还挂到 B 上。own = 本 agent 当前挂载的 {name: id}(调用方从 agent get 取)。"""
    pins = (cfg.get("multica") or {}).get("skills") or {}
    if name in pins:
        return pins[name], "pinned"
    if name in state.get("skills", {}):
        return state["skills"][name], "state"
    if own and name in own:
        return own[name], "adopted"
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
    own = _mounted_map(multica(["agent", "get", agent_id])[0])  # 本 agent 已挂载技能,认领只在此范围内

    entry = cfg["dingtalk"]["entry"]
    entry_content = (Path(work_dir) / entry).read_text(encoding="utf-8")
    skill_dirs = _skill_dirs(work_dir)

    plan = []
    plan.append(("agent.instructions", f"← {entry} ({len(entry_content)} 字)"))
    for sd in skill_dirs:
        name, desc = _skill_identity(sd, work_dir, prefix)
        sid, origin = _resolve_skill_id(name, cfg, state, own)
        refs = [p for p in sorted(sd.rglob("*.md")) if p.name != "SKILL.md"]
        desc_disp = (desc[:32] + "…") if len(desc) > 32 else desc
        plan.append((f"skill:{name}",
                     f"{'update' if sid else 'create'}({origin}) + {len(refs)} 附件 | desc: {desc_disp}"))
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
        sid, _ = _resolve_skill_id(name, cfg, state, own)
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
#  知识库指针:双保险(env 变量 + AGENTS.md 声明)
# --------------------------------------------------------------------------- #
KB_ROOT_ENV = "DINGTALK_KB_ROOT"
KB_ENTRY_ENV = "DINGTALK_KB_ENTRY"


def set_kb_env(cfg):
    """双保险之一:把知识库指针写进 agent 的自定义环境变量,保证 Agent 一定读得到自己的
    知识库根节点(另一保险是 AGENTS.md 里的声明,由 qc 检查)。env set 是整体替换,先 get 再合并。"""
    mult = cfg.get("multica") or {}
    agent_id = mult.get("agent_id")
    root = cfg["dingtalk"]["source_node"]
    entry = cfg["dingtalk"]["entry"]
    # env set 是【整体替换】,必须先 get 再合并。若 get 没成功拿到 custom_env,绝不能拿 {} 去 set ——
    # 那会把 agent 已有的全部环境变量抹掉、只剩这两个指针(静默数据丢失)。get 失败就直接告警返回。
    cur, grc = multica(["agent", "env", "get", agent_id])
    if grc != 0 or not isinstance(cur, dict) or cur.get("error") or "_raw" in cur \
            or not isinstance(cur.get("custom_env"), dict):
        print(f"  知识库环境变量:跳过(读取现有 custom_env 失败,不敢覆盖;多因非 owner/admin) ✗")
        return False
    env = dict(cur["custom_env"])
    env[KB_ROOT_ENV] = root
    env[KB_ENTRY_ENV] = entry
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(env, f, ensure_ascii=False)
            tmp = f.name
        out, rc = multica(["agent", "env", "set", agent_id, "--custom-env-file", tmp])
        ok = rc == 0 and not (isinstance(out, dict) and out.get("_raw"))
        print(f"  知识库环境变量 {KB_ROOT_ENV}={root} / {KB_ENTRY_ENV}={entry} → agent: "
              f"{'✓' if ok else '✗ (需 workspace owner/admin 权限)'}")
        return ok
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- #
#  写回(push-dingtalk):Multica → 钉钉源文档(保真写回)
# --------------------------------------------------------------------------- #
def push_dingtalk(cfg, work_dir, config_path, dry_run):
    """把 Multica 上的最新定义**保真**写回钉钉源文档(反方向)。
    用途:有人在 Multica 里改了技能/instructions,想让钉钉这份「唯一持久层」跟上。
    只写有 rel→node 映射的文档(先 pull/sync 建映射);Multica 新增、钉钉侧还没有对应
    节点的,只提示不自动建(避免乱造目录树)。写入走保真三件套:钉钉原生解析器渲 JSONML
    + 乐观锁(revision)+ 回读校验;检测到钉钉侧并发改动则拒绝覆盖。"""
    mult = cfg.get("multica") or {}
    agent_id = mult.get("agent_id")
    if not agent_id:
        raise ConfigError("multica.agent_id 未设置。")
    state = _load_state(config_path)
    dt = state.get("dingtalk") or {}
    node_map = dt.get("nodes") or {}
    entry_node = dt.get("entry_node")
    if not node_map:
        raise ConfigError("没有 rel→钉钉节点 映射。先跑一次 `pull` 或 `sync` 建立映射,再往钉钉写回。")

    prefix = mult.get("skill_name_prefix", "")
    base_names = mult.get("base_skills", ["dws"])
    entry = cfg["dingtalk"]["entry"]

    # 收集 (label, node_id, Multica内容)
    targets, orphans = [], []
    agent_out, _ = multica(["agent", "get", agent_id])
    own = _mounted_map(agent_out)
    instr = _dig(agent_out, "instructions") or ""
    if entry_node and instr:
        targets.append((entry, entry_node, instr))
    elif instr and not entry_node:
        orphans.append(entry)
    seen_names = set()
    for sd in _skill_dirs(work_dir):
        name, _ = _skill_identity(sd, work_dir, prefix)
        seen_names.add(name)
        sid, _ = _resolve_skill_id(name, cfg, state, own)
        if not sid:
            continue
        so, _ = multica(["skill", "get", sid])
        rel_skill = (sd / "SKILL.md").relative_to(work_dir).as_posix()
        content = _dig(so, "content") or ""
        if content:
            if node_map.get(rel_skill):
                targets.append((rel_skill, node_map[rel_skill], content))
            else:
                orphans.append(rel_skill)
        remote_files = {f.get("path"): f.get("content", "")
                        for f in (_dig(so, "files") or []) if isinstance(f, dict)}
        for refrel, refcontent in remote_files.items():
            full = sd.relative_to(work_dir).as_posix() + "/" + refrel
            if node_map.get(full):
                targets.append((full, node_map[full], refcontent or ""))
            else:
                orphans.append(full)
    # Multica 侧独有、遍历不到的挂载技能(钉钉无对应节点):不能靠 work_dir 目录存在与否静默丢弃,列出来。
    for extra in sorted(set(own) - seen_names - set(base_names)):
        orphans.append(f"(Multica独有技能) {extra}")

    print(f"[push-dingtalk] Multica agent {agent_id} → 钉钉源文档(根 {dt.get('root')})"
          + (" —— 干跑(不写)" if dry_run else ""))

    # 逐个:读钉钉现状 → 容忍对比 → 有真漂移才排队写
    writes, read_fail = [], 0
    for label, node, content in targets:
        remote, err = _read_dingtalk_markdown(node)   # 比对阶段只需正文,读一次
        if err:
            print(f"  {'· ' if dry_run else '→ '}{label}: 预读失败 ✗ ({err})")
            read_fail += 1          # 预读失败=可能漏写,必须计入退出码,不能静默跳过报成功
            continue
        match = dingtalk_matches_source(remote, content)
        if match is not False:
            print(f"  {'· ' if dry_run else '→ '}{label}: 已一致"
                  + ("(表现层)" if match == "render" else "") + " —— 免写")
        else:
            print(f"  {'· ' if dry_run else '→ '}{label}: 有漂移 → 待写回")
            writes.append((label, node, content, remote))
    if orphans:
        print(f"  ⚠️ Multica 有、钉钉无对应节点(不自动建,需先在钉钉建好再 pull): {sorted(set(orphans))}")

    if dry_run:
        print("  (加 --yes 才会真正写回钉钉)")
        return 1 if read_fail else 0
    fail = read_fail
    for label, node, content, remote in writes:
        status, msg = _write_dingtalk_node(node, content, remote)
        ok = status in ("written", "already")
        print(f"  {label}: {'✓ ' if ok else '✗ '}{status} ({msg})")
        fail += not ok
    tail = f"(含 {read_fail} 预读失败)" if read_fail else ""
    print(f"[push-dingtalk] {'钉钉侧已全部一致,无需写回' if not fail and not writes else f'完成,失败 {fail} 项'}{tail}")
    return fail


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
    entry_path = Path(work_dir) / entry
    if not entry_path.is_file():
        raise ConfigError(f"工作区 {work_dir} 里没有 {entry};先跑一次 pull/sync 再 verify。")
    entry_content = entry_path.read_text(encoding="utf-8")
    agent_out, _ = multica(["agent", "get", agent_id])
    own = _mounted_map(agent_out)
    results.append(("agent.instructions",
                    _text_equal(_dig(agent_out, "instructions"), entry_content)))

    for sd in _skill_dirs(work_dir):
        name, _ = _skill_identity(sd, work_dir, prefix)
        sid, _ = _resolve_skill_id(name, cfg, state, own)
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
    own = _mounted_map(agent_out)

    # checks: (label, ok, detail, hard)。hard=True 才计入失败;hard=False 只是提示。
    checks = [("instructions 非空", len(instructions.strip()) > 0, f"{len(instructions)} 字", True)]

    # 基础技能(dws)必须挂上,否则 Agent 调不动钉钉
    for bn in base_names:
        bid = _resolve_by_name(bn)
        ok = (bid in mounted_ids) or (bn in mounted_names)
        checks.append((f"基础技能 {bn} 已挂载", ok,
                       "" if ok else "缺失 → 该 Agent 无法调用钉钉,会出问题", True))

    # 知识库指针「双保险」:① 环境变量指向知识库根;② AGENTS.md 里声明了这个根节点。
    # 两处各报一行(软提示),硬性要求是「至少其一可达」——否则 Agent 找不到自己的知识库。
    root = cfg["dingtalk"]["source_node"]
    env_out, _ = multica(["agent", "env", "get", agent_id])
    _ce = env_out.get("custom_env") if isinstance(env_out, dict) else None
    env_ok = isinstance(_ce, dict) and _ce.get(KB_ROOT_ENV) == root
    doc_ok = root in instructions
    checks.append((f"环境变量 {KB_ROOT_ENV} 指向知识库", env_ok,
                   "" if env_ok else "未设(sync --yes 会自动写;写不进多因非 workspace owner/admin)", False))
    checks.append(("AGENTS.md 声明了知识库根节点", doc_ok,
                   "" if doc_ok else f"入口文档里没有根节点 {root};建议加一行「知识库根: {root}」", False))
    checks.append(("知识库指针可达(env 或 AGENTS.md 至少其一)", env_ok or doc_ok,
                   "" if (env_ok or doc_ok) else "两处都没有 → Agent 找不到自己的知识库", True))

    # 每个同步技能:name / description / content 非空,且已挂载
    skill_dirs = _skill_dirs(work_dir)
    no_name, no_desc, empty_content, not_mounted = [], [], [], []
    for sd in skill_dirs:
        name, _ = _skill_identity(sd, work_dir, prefix)
        sid, _ = _resolve_skill_id(name, cfg, state, own)
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
    checks.append((f"{n} 个技能 name 非空", not no_name, "，".join(no_name), True))
    checks.append((f"{n} 个技能 description 非空", not no_desc, "，".join(no_desc), True))
    checks.append((f"{n} 个技能 content 非空", not empty_content, "，".join(empty_content), True))
    checks.append((f"{n} 个技能已挂载到 agent", not not_mounted, "，".join(not_mounted), True))

    bad = 0
    print(f"[qc] 质检 agent {agent_id}")
    for label, ok, detail, hard in checks:
        tail = f"  ({detail})" if detail else ""
        mark = "✓" if ok else ("✗" if hard else "⚠")
        print(f"  {label}: {mark}{tail}")
        bad += (not ok) and hard
    print(f"[qc] {'质检通过,Agent 完备' if not bad else f'⚠️ {bad} 项待修'}")
    return bad


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="钉钉文档知识库 ⇄ Multica agent 双向同步")
    ap.add_argument("command", nargs="?", default="sync",
                    choices=["pull", "sync", "push-dingtalk", "verify", "qc"],
                    help="pull=只从钉钉物化; sync=钉钉→Multica(默认干跑,--yes 才写); "
                         "push-dingtalk=Multica→钉钉保真写回(默认干跑,--yes 才写); "
                         "verify=回读比对; qc=完成质检")
    ap.add_argument("--config", default="agents.md", help="同步配置文件(默认 ./agents.md)")
    ap.add_argument("--work-dir", default="./.agent-workspace", help="定义物化目录")
    ap.add_argument("--yes", action="store_true", help="sync / push-dingtalk 时真正写入")
    ap.add_argument("--force", action="store_true", help="忽略已物化目录,清空重拉")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        if args.command == "verify":
            sys.exit(1 if verify(cfg, args.work_dir, args.config) else 0)
        if args.command == "qc":
            sys.exit(1 if quality_check(cfg, args.work_dir, args.config) else 0)
        if args.command == "push-dingtalk":
            # 反方向:Multica → 钉钉。先 pull 刷新工作区结构与 rel→node 映射,再保真写回。
            pull(cfg, args.work_dir, True, args.config)
            sys.exit(1 if push_dingtalk(cfg, args.work_dir, args.config,
                                        dry_run=not args.yes) else 0)
        # 正方向:钉钉 → Multica
        pull(cfg, args.work_dir, args.force or args.command == "sync", args.config)
        if args.command == "pull":
            return
        push(cfg, args.work_dir, args.config, dry_run=not args.yes)
        if args.yes:
            # 双保险之一:把知识库指针写进 agent 环境变量(另一保险=AGENTS.md 声明,由 qc 查)
            set_kb_env(cfg)
            bad_v = verify(cfg, args.work_dir, args.config)
            bad_q = quality_check(cfg, args.work_dir, args.config)
            sys.exit(1 if (bad_v or bad_q) else 0)
    except ConfigError as exc:
        print(f"配置/前置错误: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

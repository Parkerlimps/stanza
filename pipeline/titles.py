# -*- coding: utf-8 -*-
"""
章节标题：把一段台词压成一个短标题。

两种模式：
- heuristic：无依赖，挑第一个像样的句子再截断。够用，但生硬。
- llm：调本地大模型的 OpenAI 兼容接口（LM Studio / Ollama / vLLM / llama.cpp server），
  真正做摘要。连不上或解析失败时自动退回 heuristic。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

# 本机服务必须绕过系统代理，否则 localhost 的请求会被代理转发并返回 502
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout):
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return _NO_PROXY.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)

SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…\n])")
TAG_RE = re.compile(r"<\|[^|]*\|>")
FILLER = re.compile(r"^(嗯|啊|呃|哦|那个|然后|就是|这个|你看|我说|好吧)[,，、\s]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ENG_RE = re.compile(r"[A-Za-z]{2,}")
MAX_LEN = 14          # 中文标题上限（按字算，14 字够放一行）
MAX_LEN_LATIN = 38    # 英文标题上限（按字符算）。给到 30 会把完整分句截成半句：
                      # "Determined to follow his orders" 正好 32 字符


def _core(s: str) -> str:
    """去掉标点后的有效字符，用来判断这句到底有没有内容。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s)


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _script_of(s: str) -> str:
    """粗略判断一段文本的主导文字：cjk / latin / other。

    只用来挡住「英文台词被译成中文标题」这类错误，不做精细的语种识别。
    中文里夹几个英文单词仍算 cjk，英文里夹个把人名仍算 latin。
    """
    s = s or ""
    cjk = len(_CJK_RE.findall(s))
    lat = len(_LATIN_RE.findall(s))
    if not cjk and not lat:
        return "other"
    return "cjk" if cjk >= lat else "latin"


def limit_for(s: str) -> int:
    """按文字系统给长度上限。中文按字算，英文按字符算，两者差一倍多。"""
    return MAX_LEN_LATIN if _script_of(s) == "latin" else MAX_LEN


def sentences(text: str):
    text = TAG_RE.sub("", text or "").strip()
    if not text:
        return []
    out = []
    for part in SENT_SPLIT.split(text):
        part = FILLER.sub("", part.strip(" ，,、")).strip()
        if len(_core(part)) < 4:
            continue
        # 单个英文单词当不了标题（Yeah / The / Okay）
        if part.isascii() and len(part.split()) < 2:
            continue
        out.append(part)
    return out


def _shorten(s: str, max_len: int) -> str:
    """按字符截断；英文尽量断在词边界，不要切断单词。"""
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    if s.isascii() and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,，、") + "…"


def heuristic(text: str, max_len: int | None = None) -> str:
    """挑一个能完整放下的句子，实在没有才截断。

    以前是「取第一个 6~40 字的句子，再截到 10 字」，结果几乎每条标题都以省略号
    结尾（「说一些说一下说下在泰…」），读起来像半句话。改成优先挑长度本身
    就在上限内的句子，只有全都超长时才截断。
    """
    max_len = max_len or limit_for(text)
    sents = [s.strip(" 。．.!！?？,，、") for s in sentences(text)]
    sents = [s for s in sents if s]
    if not sents:
        return ""
    pick = next((s for s in sents if 6 <= len(_core(s)) <= max_len), None)
    if pick is not None:
        return pick.strip()
    # 全都太短或全都太长：取最长的一句，太长才截
    return _shorten(max(sents, key=lambda x: len(_core(x))), max_len).strip()


def _endpoint(url: str) -> str:
    """
    兼容各家写法：
      http://127.0.0.1:1234        -> /v1/chat/completions   (LM Studio / Ollama / vLLM)
      http://127.0.0.1:8000/v3     -> /v3/chat/completions   (OVMS)
      http://.../v1/chat/completions -> 原样
    """
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if re.search(r"/v\d+$", url):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


THINK_RE = re.compile(r"[\s\S]*?<｜end▁of▁thinking｜>")
THINK_TAG_RE = re.compile(r"<think[^>]*>[\s\S]*?</think[^>]*>")


def _strip_think(text: str) -> str:
    """Qwen3 系列会先吐思考过程，只取最后的结果部分。"""
    text = THINK_TAG_RE.sub("", text or "")
    text = THINK_RE.sub("", text)
    return text.strip()


PROMPT = """你在给一部影片写章节小标题。

下面每一行是一段视频开头的台词或字幕，编号从 1 开始。
内容可能是中文，也可能是英文或其他语言，还可能带语音识别的错字，请按大意理解。

【第一条，最重要：绝不翻译】
- 台词是什么语言，标题就用什么语言。英文台词必须写英文标题，
  中文台词写中文标题，日文台词写日文标题。
- 看到英文歌词或英文对白却写出中文标题，是错误答案。
- 下面每段台词的语言可能不同，逐段单独判断，不要统一成同一种语言。

其它要求：
- 中文标题 4 到 8 个字；英文标题 2 到 4 个单词的完整短语
- 英文标题不许以 to / of / for / and / the 收尾，也不要用问号把两句话拼在一起
- 必须概括这一段在讲什么，不要直接摘抄台词原句
- 要有区分度：不要只写「对决」「危机」这种两个字的泛词，也不要和别段撞名
- 台词里如果只是台标、公司名、字幕组、版权信息，说明这一段没有剧情，
  必须输出空字符串，不要描述它是什么（不要写「XX标识」「XX公司」这类）
- 内容为空、或是纯音乐/音效、或完全无法判断时，输出空字符串
- 只输出 JSON 数组，元素是字符串，长度必须和输入行数一致
- 不要解释，不要 Markdown 代码块，不要任何前后缀

示例：
输入 1. 我们将需要所有的救援这里一团糟
输出 ["全城陷入混乱"]

输入 1. We're no strangers to love, you know the rules and so do I
输出 ["Love's Rules"]

输入 1. 如果是我就不会玩火，朋友 你是纸做的
输出 ["楼顶正面对峙"]

输入 1. 索尼公司
输出 [""]

台词：
"""


LAST_ERROR = ""
_UNREACHABLE = False


def _call(texts, base_url: str, model: str, timeout: int = 180, prompt: str = None, context_lines=None):
    """单次请求，成功返回与 texts 等长的列表，失败返回 None 并记下原因。"""
    global LAST_ERROR, _UNREACHABLE
    lines = []
    for i, t in enumerate(texts):
        clean = re.sub(r"\s+", " ", TAG_RE.sub("", t or "")).strip()
        lines.append(f"{i + 1}. {clean[:220] if clean else '（无台词）'}")

    # 带上文：让模型知道这一段接在什么后面，标题才可能连贯
    ctx = ""
    if context_lines:
        cleaned = [re.sub(r"\s+", " ", c).strip()[:120] for c in context_lines if (c or "").strip()]
        if cleaned:
            ctx = (
                "【上文，仅供参考，不要为它们写标题】\n"
                + "\n".join(f"- {c}" for c in cleaned)
                + "\n\n【需要写标题的部分】\n"
            )

    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 512,
        "stream": False,
        "messages": [
            {"role": "system", "content": "你只输出 JSON 数组，不输出任何其他内容。"},
            {"role": "user", "content": (prompt or PROMPT) + ctx + "\n".join(lines)},
        ],
    }

    # Qwen3 系列默认先思考再回答，这里关掉，省时间也省得解析
    if "qwen" in model.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}

    req = urllib.request.Request(
        _endpoint(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _open(req, timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        msg = payload["choices"][0]["message"]
        content = msg.get("content") or ""
        # 有些运行时会单独给 reasoning_content，正文可能是空的
        if not content.strip():
            content = msg.get("reasoning_content") or ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        LAST_ERROR = f"HTTP {e.code} {detail}".strip()
        return None
    except urllib.error.URLError as e:
        LAST_ERROR = f"连不上 {e.reason}"
        _UNREACHABLE = True   # 服务没起来，重试多少次都没用，直接放弃
        return None
    except (KeyError, ValueError, TimeoutError) as e:
        LAST_ERROR = f"返回格式异常 {type(e).__name__}"
        return None

    parsed = _parse(_strip_think(content), len(texts))
    if parsed is None:
        LAST_ERROR = "模型返回的不是合法 JSON 数组"
    return parsed


def _fill(texts, base_url: str, model: str, timeout: int, prompt: str = None, ctx=None):
    """失败就二分重试：8 段对不上就拆成 4+4，最差退到单条。"""
    got = _call(texts, base_url, model, timeout, prompt, ctx)
    if got:
        return got
    if _UNREACHABLE or len(texts) <= 1:
        return [""] * len(texts)
    mid = len(texts) // 2
    left = _fill(texts[:mid], base_url, model, timeout, prompt, ctx)
    tail = (ctx or []) + list(texts[:mid][-2:])
    right = _fill(texts[mid:], base_url, model, timeout, prompt, tail)
    return left + right


def llm_titles(texts, base_url: str, model: str, timeout: int = 180, batch: int = 8,
               prompt: str = None, context: int = 2, on_progress=None):
    """
    分批请求，每批带上前面几段作为上文——模型知道这一段接在哪，
    标题才有前后逻辑，而不是对着一句话瞎猜。
    """
    global _UNREACHABLE
    if not base_url or not model or not texts:
        return None

    _UNREACHABLE = False
    total = (len(texts) + batch - 1) // batch
    out = []
    ok = 0
    for bi, i in enumerate(range(0, len(texts), batch)):
        chunk = texts[i:i + batch]
        ctx = [t for t in texts[max(0, i - context):i] if (t or "").strip()]
        got = _fill(chunk, base_url, model, timeout, prompt, ctx or None)
        if any(got):
            ok += 1
        out.extend(got)
        if on_progress:
            on_progress(bi + 1, total)
        if _UNREACHABLE:
            out.extend([""] * (len(texts) - len(out)))
            break
    return out if ok else None


def _parse(content: str, n: int):
    text = content.strip()

    # 有些模型会返回 {"1": "...", "2": "..."} 这种带编号的对象，而不是数组
    if "{" in text and "}" in text:
        try:
            obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
            if isinstance(obj, dict):
                vals = [obj.get(str(i)) for i in range(1, n + 1)]
                if all(v is not None for v in vals):
                    return [str(v) for v in vals]
        except ValueError:
            pass

    start = text.find("[")
    end = text.rfind("]")

    if start >= 0 and end > start:
        inner = text[start + 1:end]
        try:
            arr = json.loads("[" + inner + "]")
            if isinstance(arr, list) and len(arr) == n:
                return [str(x) for x in arr]
        except ValueError:
            pass

        # 小模型经常漏写引号，比如 ["a", "b", 【c】, "d"]，按逗号切也能救回来。
        # 注意空标题也要占位，否则整批会错位。
        parts = [p.strip().strip("\\/\"'“”「」『』【】[] ") for p in inner.split(",")]
        if len(parts) == n:
            return parts
        non_empty = [p for p in parts if p]
        if len(non_empty) == n:
            return non_empty

    # 退一步：按行号解析
    out = [""] * n
    hit = 0
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)[.、:：)）]\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < n:
            out[idx] = m.group(2).strip(" \"'“”")[:MAX_LEN]
            hit += 1
    return out if hit >= max(1, n // 2) else None


def _root(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not re.search(r"/v\d+$", url):
        url += "/v1"
    return url


def ping(base_url: str, model: str, timeout: int = 30) -> bool:
    """自检：列模型 + 发一个小请求，确认地址和模型名都对。"""
    import time

    root = _root(base_url)
    print(f"服务地址  {root}")
    print(f"对话端点  {_endpoint(base_url)}")

    ids = []
    try:
        req = urllib.request.Request(root + "/models")
        with _open(req, timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", [])]
        print(f"可用模型  {ids}")
        if model and ids and model not in ids:
            print(f"注意：{model} 不在列表里，OVMS 的 servable 名字通常等于模型目录名")
    except Exception as exc:
        print(f"列模型失败（不影响下面的对话测试）: {exc}")

    t0 = time.perf_counter()
    got = _call(["这是一段测试台词，用来确认模型能不能返回 JSON 数组。"], base_url, model, timeout=timeout)
    dt = time.perf_counter() - t0

    if got:
        print(f"对话测试  成功，往返 {dt:.2f}s，返回 {got}")
        return True
    print(f"对话测试  失败：{LAST_ERROR}")
    return False


BAD_TITLES = {
    "空白", "空白片段", "未识别", "无法识别", "无内容", "未知", "未知内容", "空", "无",
    "无法判断", "略", "同上", "标题", "小标题", "片段", "n/a", "none", "null",
}


# 「X与Y」「X，Y」「X引发Y」这类双段式，超长时取其中一段
SPLIT_RE = re.compile(r"[与，,、；;和]|以及|引发|导致|让|使|被|遭|却|但|之后")


def _compress(s: str, max_len: int) -> str:
    """超过上限的标题按连接词切，取最长的能塞进去的那段。"""
    if len(s) <= max_len:
        return s
    parts = [p for p in re.split(SPLIT_RE, s) if p]
    good = [p for p in parts if 4 <= len(p) <= max_len]
    if good:
        return max(good, key=len)
    cut = s[:max_len]
    if s.isascii() and " " in cut:
        cut = cut[: cut.rfind(" ")]      # 英文别切在单词中间
    return cut.rstrip(" 的与和,，、")


# 英文标题常被模型写成半句：以介词/冠词收尾，或把两句用问号拼在一起
_TAIL_STOP = {
    "to", "of", "for", "and", "the", "a", "an", "in", "on", "at", "with", "by",
    "from", "that", "is", "are", "was", "were", "but", "or", "as", "into",
    "about", "his", "her", "their", "its", "this", "these", "those", "than",
}


def _tidy_latin(s: str) -> str:
    """把英文标题收成一个完整短语。

    小模型给的英文标题经常是半句话：
        "Spider-Man Grounded for"   —— 以介词收尾
        "No, What Did You Do? Wanting" —— 两句拼在一起
    直接上屏就是「Spider-Man Grounded for」这种断头标题。
    """
    m = re.match(r"^(.{8,}?[?!])\s+\S", s)
    if m:
        s = m.group(1)
    words = s.split()
    while len(words) > 2:
        last = words[-1].strip(".,;:!?—-…").lower()
        # 介词/冠词结尾、或者所有格结尾（"a fugitive's"），都是没说完
        if last in _TAIL_STOP or last.endswith("'s") or last.endswith("’s"):
            words.pop()
        else:
            break
    return " ".join(words).rstrip(" ,;:-—…")


def clean_title(s: str, max_len: int | None = None) -> str:
    """统一标题格式：去括号引号、去首尾标点和转义残留、挡掉模型偷懒的占位词。"""
    s = (s or "").strip()
    max_len = max_len or limit_for(s)
    s = s.strip("\\/【】[]《》〈〉「」『』()（）\"'“”‘’ ")
    s = s.strip("，,。.、：:；;！!？?…—- ")
    if not s or s.lower() in BAD_TITLES:
        return ""
    if _script_of(s) == "latin":
        s = _tidy_latin(s)
        if not s:
            return ""
    # 「公司标识」「索尼品牌亮相」这类是模型在描述台标，不是剧情
    if len(s) <= 10 and re.search(r"(标识|标志|logo|品牌|片头|字幕组)", s, re.I):
        return ""
    if len(s) <= 6 and re.search(r"(影业|公司|工作室|制片|出品)", s):
        return ""
    if max_len and len(s) > max_len:
        s = _compress(s, max_len)
    else:
        s = _shorten(s, max_len)
    # 截断可能正好切在介词上（"...deadlines and"），收尾再理一遍
    if _script_of(s) == "latin":
        s = _tidy_latin(s)
    return s


SUMMARY_PROMPT = """你在给一部影片做逐段剧情提要。

下面每一行是一段视频开头的台词或字幕，编号从 1 开始。
内容可能是中文或英文，也可能带语音识别的错字，按大意理解。

为每一段写一句 15 到 25 字的剧情摘要：
- 说清楚「谁 在做什么 发生了什么」，要有信息量
- 不要照抄台词，不要写「这一段里」「他说」这类空话
- 台词只是台标、公司名、字幕组、纯音效时，写「无剧情」
- 语言跟原文一致，不要翻译
- 只输出 JSON 数组，长度必须和输入行数一致，不要解释

台词：
"""

TITLE_PROMPT = """下面每一行是一段影片的剧情摘要，编号从 1 开始。

为每一段拟一个章节标题：
- 中文摘要写中文标题，4 到 8 个字；英文摘要写英文标题，2 到 4 个词的完整短语
- 要概括，不要照抄摘要里的句子
- 要有区分度，不要和相邻段落撞名
- 摘要为「无剧情」或看不出内容时，输出空字符串
- 只输出 JSON 数组，长度必须和输入行数一致，不要解释

摘要：
"""

# 两阶段跑完后，这里会留下每段的摘要，方便写进 chapters.json 备查
LAST_SUMMARIES = []


def clean_summary(s: str) -> str:
    s = (s or "").strip().strip("\"'“”「」『』【】[] ")
    s = s.strip("，,。.、：:；; ")
    if not s or "无剧情" in s or s.lower() in BAD_TITLES:
        return ""
    return s[:60]


def _single_stage(texts, idx, base, base_url, model, on_progress):
    got = llm_titles([texts[i] for i in idx], base_url, model, on_progress=on_progress)
    if not got:
        return None
    return got


def _lang_name(script: str) -> str:
    return "英文" if script == "latin" else "中文"


def _retry_by_language(texts, base_url, model):
    """模型把某些段翻成别的语言了，单独再问一次，并把语言要求说到最前面。

    批量请求里模型容易被上一段的语言带跑（同一批 8 条全被译成中文），
    单独拎出来强调一次，命中率明显更高。
    """
    if not texts:
        return None
    script = _script_of(texts[0])
    lang = _lang_name(script)
    spec = "英文标题 2 到 4 个单词的完整短语" if script == "latin" else "中文标题 4 到 8 个字"
    prompt = (
        f"下面每一行是一段视频的台词，原文是{lang}。\n\n"
        f"为每一段写一个章节标题，必须用{lang}，绝对不要翻译成其它语言。\n"
        f"- {spec}\n"
        "- 概括这一段在讲什么，不要照抄原句\n"
        "- 台词只是台标、公司名、字幕组、版权信息，或根本看不出内容时，输出空字符串，\n"
        "  不要描述它是什么（不要写「XX标识」「XX公司」这类）\n"
        "- 只输出 JSON 数组，长度和输入行数一致，不要解释，不要 Markdown\n\n"
        "台词：\n"
    )
    return llm_titles(texts, base_url, model, prompt=prompt, on_progress=None)


def build(texts, base_url=None, model=None, on_progress=None, two_stage=False):
    """
    两阶段（默认关闭）：先把每段台词压成剧情摘要，再根据摘要起标题。
    实测在 4B 上收益不明显且慢 3 倍，摘要环节自己会编，反而引入新错误。
    留给更大模型试：--two-stage 打开。
    任一阶段失败就退回单阶段。
    """
    global LAST_SUMMARIES
    LAST_SUMMARIES = []

    base = [heuristic(t) for t in texts]
    if not base_url or not model:
        return base, "heuristic"

    # 只把有实质台词的段发过去：空行和「。」这类残片会让小模型合并输出，导致整批错位
    idx = [i for i, t in enumerate(texts) if len(_core(t or "")) >= 4]
    if not idx:
        return base, "heuristic"

    subs = [texts[i] for i in idx]
    batches = (len(subs) + 7) // 8
    tick = {"n": 0}

    def step(_done, _total):
        tick["n"] += 1
        if on_progress:
            on_progress(min(tick["n"], batches * 2), batches * 2)

    got = None
    if two_stage:
        summaries = llm_titles(subs, base_url, model, prompt=SUMMARY_PROMPT, on_progress=step)
        cleaned = [clean_summary(s) for s in (summaries or [""] * len(subs))]

        LAST_SUMMARIES = [""] * len(texts)
        for k, i in enumerate(idx):
            LAST_SUMMARIES[i] = cleaned[k]

        keep = [k for k, s in enumerate(cleaned) if s]
        if keep:
            second = llm_titles(
                [cleaned[k] for k in keep], base_url, model, prompt=TITLE_PROMPT, context=3, on_progress=step
            )
            if second:
                got = [""] * len(subs)
                for k, t in zip(keep, second):
                    got[k] = t

    if not got:
        got = _single_stage(texts, idx, base, base_url, model, on_progress)
    if not got:
        return base, ("heuristic（LLM 失败：" + LAST_ERROR + "）") if LAST_ERROR else "heuristic"

    # 模型说这段没内容就尊重它——之前一律回退启发式，会把台标读成「a Sony Company」
    merged = [""] * len(texts)
    filled = 0
    rejected = 0
    bad = []            # 语言不符的段，等下单独补问
    for k, i in enumerate(idx):
        t = clean_title(got[k] if k < len(got) else "")
        # 语言必须跟原文一致。小模型经常把英文台词意译成中文
        # （"We're no strangers to love" → 「初次相遇」），prompt 里写死也拦不住，
        # 所以这里再校验一次：文字不符就丢掉，退回同语言的启发式。
        src = _script_of(texts[i])
        if t and src != "other" and _script_of(t) != src:
            t = base[i]
            rejected += 1
            bad.append((k, i))
        # 台词很长却被判成空，多半是模型失误，这时才用启发式兜一下
        if not t and len(_core(texts[i])) >= 20:
            t = base[i]
        merged[i] = t
        if t:
            filled += 1

    # 被翻译的那些段单独再问一次，按语言分组，避免再次被带跑
    if bad:
        groups = {}
        for k, i in bad:
            groups.setdefault(_script_of(texts[i]), []).append((k, i))
        for script, items in groups.items():
            if script == "other":
                continue
            fixed = _retry_by_language([texts[i] for _, i in items], base_url, model)
            if not fixed:
                continue
            for (_k, i), t2 in zip(items, fixed):
                t2 = clean_title(t2)
                if t2 and _script_of(t2) == script:
                    if not merged[i]:
                        filled += 1
                    merged[i] = t2

    if not filled:
        return base, "heuristic"
    how = f"llm:{model} {filled}/{len(idx)}"
    if rejected:
        how += f"（{rejected} 条曾译错，已纠正）"
    return merged, how


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="检查本地大模型接口是否可用")
    ap.add_argument("--url", required=True, help="如 http://127.0.0.1:8000/v3")
    ap.add_argument("--model", required=True, help="OVMS 里的 servable 名字")
    a = ap.parse_args()
    raise SystemExit(0 if ping(a.url, a.model) else 1)

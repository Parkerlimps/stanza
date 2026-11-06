# -*- coding: utf-8 -*-
"""
语义分段：让大模型判断「这里是不是一个新段落的开始」。

设计上刻意不让模型直接输出章节数或时间点：
- 它算不准时间（让它算，错得离谱且无法校验）
- 它做不好全局规划（上下文放不下整片台词，强行放会漏掉中段）

所以它只回答局部问题：「这 ±1 分钟里，有没有新的剧情段落开始，在几分几秒」。
全局的长度约束、边界吸附交给 analyze.build_chapters 的动态规划。
模型给的分数只是「切在这里有多值」的权重，切不切由 DP 决定。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.error
import urllib.request

from titles import _endpoint, _open, _strip_think

sys.stdout.reconfigure(encoding="utf-8")

MIN_SCORE = 0.5          # 低于这个分数当作"没有转折"
SNAP = 8.0               # 模型给的时间点最多吸附到 8 秒内最近的一句台词
MAX_LINES = 90           # 单个窗口最多喂多少句，防止字幕特密的片子炸上下文
MAX_AT = 1800.0          # 相对秒的合理上限，超过必然是模型把相对/绝对时间搞混了

PROMPT = """你在给一部影片划分剧情段落。

下面是某一段视频的台词，每行是「相对秒数 + 台词」。时间从 0 开始，只覆盖这段窗口。

判断：窗口**中间**有没有一个新的剧情段落开始？

算作新段落的：
- 地点、时间、场景明显切换
- 主线事件告一段落，新的事件开始
- 出现新的关键人物，或者冲突方向变了

不算新段落的：
- 同一场戏内部的话题转移、闲聊、寒暄
- 短暂闪回、回忆插叙
- 只是情绪起伏

只输出一个 JSON 对象，不要解释、不要代码块：
{"at": 新段落起始的相对秒数, "score": 0 到 1 的把握}

没有明显新段落就输出：
{"at": -1, "score": 0}

台词：
"""


def candidates(duration: float, step: float, head: float = 45.0, tail: float = 45.0):
    """沿时间轴等距撒候选点。避开片头片尾——那里常常是 logo 和字幕表。"""
    if duration <= head + tail + step * 0.5:
        return []
    out = []
    t = head
    while t < duration - tail:
        out.append(round(t, 2))
        t += step
    return out


def _lines(cues, lo: float, hi: float):
    """窗口内的台词，时间换成相对秒。"""
    rows = []
    for t, txt in cues:
        if lo <= t < hi:
            rows.append(f"{t - lo:.1f}\t{txt}")
            if len(rows) >= MAX_LINES:
                break
    return rows


def _ask(lines, base_url: str, model: str, timeout: int = 60):
    """返回 (相对秒, 分数)。失败返回 (-1, 0.0)。"""
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": "你只输出 JSON 对象，不输出任何其他内容。"},
            {"role": "user", "content": PROMPT + "\n".join(lines)},
        ],
    }
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
        content = _strip_think(msg.get("content") or "")
        if not content.strip():
            content = _strip_think(msg.get("reasoning_content") or "")
    except Exception:
        return -1.0, 0.0

    return _parse_point(content)


def _parse_point(content: str):
    if not content:
        return -1.0, 0.0
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return -1.0, 0.0
    try:
        obj = json.loads(content[start:end + 1])
    except ValueError:
        # 小模型偶尔写 {"at": 12.5, "score": 0.8,} 这种尾逗号
        raw = re.sub(r",\s*([}\]])", r"\1", content[start:end + 1])
        try:
            obj = json.loads(raw)
        except ValueError:
            return -1.0, 0.0

    try:
        at = float(obj.get("at", -1))
        score = float(obj.get("score", 0))
    except (TypeError, ValueError):
        return -1.0, 0.0
    if at < 0 or score <= 0 or at > MAX_AT:
        return -1.0, 0.0
    return at, max(0.0, min(1.0, score))


def _snap(t: float, cue_times, lo: float, hi: float) -> float:
    """把模型给的时间吸附到最近一句台词的起点，别让章节边界落在半句话上。"""
    if not cue_times:
        return t
    best = min(cue_times, key=lambda x: abs(x - t))
    if abs(best - t) <= SNAP and lo - 1.0 <= best <= hi + 1.0:
        return best
    return t


def score(cues, cands, base_url: str, model: str, half: float = 60.0, workers: int = 2,
          timeout: int = 60, on_progress=None):
    """
    给每个候选点问一次模型，返回 {绝对时间: 分数}（只含分数 >= MIN_SCORE 的点）。

    单个候选失败不影响其他候选——失败就是没有语义切点，DP 会退回按镜头/长度切。
    """
    if not cues or not cands:
        return {}

    cue_times = [t for t, _ in cues]
    result: dict[float, float] = {}
    lock = threading.Lock()
    done = 0
    asked = 0

    def one(t: float):
        lo, hi = max(0.0, t - half), t + half
        lines = _lines(cues, lo, hi)
        if len(lines) < 4:      # 台词太少，问了也是瞎猜
            return None
        at, s = _ask(lines, base_url, model, timeout)
        if at < 0 or s < MIN_SCORE:
            return None
        abs_t = _snap(lo + at, cue_times, lo, hi)
        # 吸附失败又落在全片台词范围外的点直接丢。这种点会进 semantic，
        # 而 _fuse_semantic 再宽也只能挡一次，别让它污染 DP 的终点。
        if not (cue_times[0] - 1.0 <= abs_t <= cue_times[-1] + 1.0):
            return None
        return abs_t, s

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, t): t for t in cands}
        for fut in as_completed(futures):
            with lock:
                done += 1
                asked += 1
            try:
                got = fut.result()
            except Exception:
                got = None
            if got:
                with lock:
                    # 多个候选可能吸附到同一句台词，取较高分
                    result[got[0]] = max(result.get(got[0], 0.0), got[1])
            if on_progress:
                on_progress(done, len(cands))

    return dict(sorted(result.items()))

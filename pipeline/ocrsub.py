# -*- coding: utf-8 -*-
"""
硬字幕 OCR：从画面底部读出烧进画面的字幕。

比语音转写靠谱得多——字幕是官方译文，转写是对声音的猜测。
但画面里还有路牌、书名、转场字卡、片尾名单，必须过滤掉，否则会读出
「如何说孩子才会听」这种书名当章节标题。

注意：一章要读多行，不是只留最长的一条。只留一句的话，
后面无论怎么调 prompt，模型都只能拿一句话编标题。
"""

from __future__ import annotations

import os
import re
import sys

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ENG_RE = re.compile(r"[A-Za-z]{2,}")
# 片尾名单、转场字卡通常是全大写英文（SAMUEL ARTURO / MEANWHILE-ON EARTH）
CREDIT_RE = re.compile(r"^[A-Z0-9 .,'\-—:&]{8,}$")
# 整行被括号包住的长文本，多半是书名、招牌
PAREN_RE = re.compile(r"^[（(].{8,}[)）]$")

Y0, Y1 = 0.68, 0.99
X0, X1 = 0.05, 0.95

# 一章最多读多少字——够模型写一句摘要，又不至于把 prompt 撑爆
MAX_CHARS = 140
# 读到多少字就不再继续采样（够用就走）
ENOUGH = 60


def available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        os.environ.setdefault("OMP_NUM_THREADS", "4")
        _engine = RapidOCR()
    return _engine


def _read_frame(cap, t: float):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def _ocr_band(engine, frame):
    """返回 (文本行列表, 文本在整帧里的纵向中心比例)。"""
    h, w = frame.shape[:2]
    top = int(h * Y0)
    crop = frame[top:int(h * Y1), int(w * X0):int(w * X1)]
    if crop.size == 0:
        return [], 0.0

    result = engine(crop)
    txts = getattr(result, "txts", None)
    if not txts:
        return [], 0.0

    boxes = getattr(result, "boxes", None)
    ch, cw = crop.shape[:2]
    parts = []
    centres = []
    for i, raw in enumerate(txts):
        text = (raw or "").strip()
        if not text:
            continue
        if boxes is not None and i < len(boxes):
            b = np.asarray(boxes[i]).reshape(-1, 2)
            x0, y0 = b.min(axis=0)
            x1, y1 = b.max(axis=0)
            width = x1 - x0
            cx = (x0 + x1) / 2.0
            if width < cw * 0.18:                 # 太短，像画面里的杂字
                continue
            if not (cw * 0.10 < cx < cw * 0.90):  # 不居中
                continue
            centres.append((top + (y0 + y1) / 2.0) / h)
        parts.append(text)

    y = float(np.mean(centres)) if centres else 0.0
    return parts, y


def _useful(text: str) -> bool:
    if len(CJK_RE.findall(text)) >= 2:
        return True
    return len(ENG_RE.findall(text)) >= 2


def _is_credit(text: str) -> bool:
    return bool(CREDIT_RE.match(text.strip())) and not CJK_RE.search(text)


def _is_noise(text: str) -> bool:
    return bool(PAREN_RE.match(text.strip()))


def _scan(engine, cap, start: float, end: float, fractions, enough: int = ENOUGH):
    """
    在一个区间里多点采样，把读到的不同字幕行累积起来。
    字幕会停留几秒，同一句会重复读到，按内容去重。
    """
    lines = []
    seen = set()
    ys = []
    total = 0

    for frac in fractions:
        t = start + (end - start) * frac
        frame = _read_frame(cap, t)
        if frame is None:
            continue
        parts, y = _ocr_band(engine, frame)
        if parts:
            ys.append(y)
        for text in parts:
            if _is_credit(text) or _is_noise(text) or not _useful(text):
                continue
            key = re.sub(r"\s+", "", text)
            if key in seen:
                continue
            seen.add(key)
            lines.append(text)
            total += len(text)
        if total >= enough:
            break

    return lines, (float(np.mean(ys)) if ys else 0.0)


def detect(video_path, duration: float, samples: int = 8) -> float:
    """抽样几帧看底部有没有字幕，返回命中率 0-1。"""
    if not available() or duration <= 0:
        return 0.0
    engine = _get_engine()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0

    hits = 0
    tried = 0
    for i in range(samples):
        t = duration * (i + 0.5) / samples
        frame = _read_frame(cap, t)
        if frame is None:
            continue
        tried += 1
        parts, _ = _ocr_band(engine, frame)
        text = " ".join(parts)
        if _useful(text) and not _is_credit(text) and not _is_noise(text):
            hits += 1
    cap.release()
    return hits / tried if tried else 0.0


def read_chapters(video_path, ranges, on_progress=None):
    """
    ranges: [(开始秒, 结束秒)]。
    第一轮在前 90 秒内多点采样；还是空的段，第二轮扫全章。
    最后按纵向位置过滤：一部片子的字幕高度固定，偏离中位数的多半是书名、路牌。
    """
    engine = _get_engine()
    cap = cv2.VideoCapture(str(video_path))
    raw = []

    for i, (start, end) in enumerate(ranges):
        head = min(end, start + 90.0)
        lines, y = _scan(engine, cap, start, head, (0.04, 0.12, 0.22, 0.34, 0.48, 0.62))
        raw.append((lines, y))
        if on_progress:
            on_progress(i + 1, len(ranges))

    misses = [i for i, (lines, _) in enumerate(raw) if not lines]
    for k, i in enumerate(misses):
        start, end = ranges[i]
        if end - start <= 91.0:
            continue
        lines, y = _scan(engine, cap, start, end, (0.2, 0.4, 0.6, 0.8), enough=40)
        raw[i] = (lines, y)
        if on_progress:
            on_progress(len(ranges) - len(misses) + k + 1, len(ranges))

    cap.release()
    if on_progress:
        on_progress(len(ranges), len(ranges))

    ys = [y for lines, y in raw if lines and y > 0]
    median = float(np.median(ys)) if len(ys) >= 5 else None

    out = []
    for lines, y in raw:
        if not lines:
            out.append("")
            continue
        if median is not None and y > 0 and abs(y - median) > 0.06:
            out.append("")
            continue
        out.append(" ".join(lines)[:MAX_CHARS])
    return out

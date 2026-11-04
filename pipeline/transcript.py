# -*- coding: utf-8 -*-
"""
全片台词提取：外挂字幕优先，没有就走语音转写。

和以前「每章开头转 30 秒」不同，语义分段需要整片台词——大模型得看见
前后文才知道哪里是剧情转折。所以这里的产物是完整的 [(起秒, 文本)]。

三种来源的代价差得很远：
    外挂字幕  <1 秒
    语音转写  140 分钟片约 10 分钟（SenseVoice 十几倍实时）
    硬字幕 OCR 全片要几小时（1.8 秒/帧），只在显式指定时用
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 单条台词上限。超长的多半是整段拼接或识别串行，留着只会污染给模型的输入
MAX_CUE = 160

_ASS_STYLE = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def clean_text(s: str) -> str:
    s = _ASS_STYLE.sub("", s or "")
    s = _HTML_TAG.sub("", s)
    s = s.replace("\\N", " ").replace("\\n", " ")
    s = _WS.sub(" ", s).strip()
    return s


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CUE else text[:MAX_CUE].rstrip() + "…"


# ---------------------------------------------------------------- 字幕文件

def parse_srt(path: Path):
    """SRT / VTT。返回 [(起秒, 文本)]。"""
    cues = []
    raw = path.read_text(encoding="utf-8", errors="ignore").replace("\r", "")
    for block in raw.split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        m = re.search(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block
        )
        if not m:
            continue
        g = list(map(int, m.groups()))
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        body = " ".join(lines[lines.index(next(l for l in lines if "-->" in l)) + 1:])
        text = clean_text(body)
        if text:
            cues.append((start, _clip(text)))
    return cues


def parse_ass(path: Path):
    """ASS / SSA。取 Events 段里的 Dialogue 行。"""
    cues = []
    raw = path.read_text(encoding="utf-8", errors="ignore").replace("\r", "")
    in_events = False
    for line in raw.split("\n"):
        s = line.strip()
        if s.startswith("[") and s.lower().startswith("[events"):
            in_events = True
            continue
        if s.startswith("[") and in_events:
            in_events = False
        if not in_events or not s.lower().startswith("dialogue:"):
            continue
        parts = s.split(",", 9)
        if len(parts) < 10:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)\.(\d+)", parts[1].strip())
        if not m:
            continue
        h, mi, se, cs = map(int, m.groups())
        text = clean_text(parts[9])
        if text:
            cues.append((h * 3600 + mi * 60 + se + cs / 100, _clip(text)))
    return cues


SUBTITLE_EXT = ((".srt", parse_srt), (".ass", parse_ass), (".ssa", parse_ass), (".vtt", parse_srt))


def from_subtitle(video: Path):
    """找同名外挂字幕。返回 (cues, 文件名) 或 ([], None)。"""
    for ext, fn in SUBTITLE_EXT:
        p = video.with_suffix(ext)
        if not p.is_file():
            continue
        try:
            cues = fn(p)
        except Exception as exc:
            print(f"      字幕解析失败 {p.name}：{exc}")
            continue
        if cues:
            cues.sort(key=lambda x: x[0])
            return cues, p.name
    return [], None


# ---------------------------------------------------------------- 语音转写

def from_asr(video, duration: float, backend="auto", model="small", window: float = 30.0,
             on_progress=None):
    """整片转写成句子级时间戳。返回 (cues, 后端名)。

    backend=auto 时先探语言：
        中文片 → SenseVoice（中文 CER 低，十几倍实时）
        英语片 → whisper（SenseVoice 的英文错字太多，一句话能错三处，
                 而章节分段全靠台词，错字直接吃掉判断力）
    """
    import asr as _asr

    try:
        audio = _asr.decode_audio(video)
    except Exception as exc:
        print(f"      音频解码失败，可能没有音轨：{exc}")
        return [], None

    name = backend
    if name == "auto":
        lang = _asr.detect_language(audio=audio, backend="sensevoice", model=model)
        if lang == "en":
            if _asr.backend_available("whisper"):
                name = "whisper"
                print("      探测到英语对白，改用 whisper")
            else:
                name = "sensevoice"
                print("      探测到英语对白，但没有 whisper 可用，仍走 SenseVoice")
        else:
            name = "sensevoice"

    if not _asr.backend_available(name):
        alt = "whisper" if name == "sensevoice" else "sensevoice"
        if _asr.backend_available(alt):
            print(f"      {name} 不可用，改用 {alt}")
            name = alt
        else:
            print("      没有可用的转写后端")
            return [], None

    def on_step(done, total):
        if on_progress:
            on_progress(done, total)

    if name == "whisper":
        cues = _asr.transcribe_whisper(audio=audio, model=model, on_progress=on_step)
    else:
        cues = _asr.transcribe_sentences(
            audio=audio, backend=name, model=model, on_progress=on_step
        )
    return cues, name


# ---------------------------------------------------------------- 对外

def extract(video, duration: float, how: str = "auto", backend="auto", model="small",
            window: float = 30.0, on_progress=None):
    """
    统一入口，返回 (cues, source)。

    how:
        auto  外挂字幕优先，没有就转写
        sub   只用外挂字幕
        asr   只转写
        off   不提取
    backend:
        auto       先探语言，中文走 SenseVoice、英语走 whisper
        sensevoice 强制 SenseVoice
        whisper    强制 faster-whisper
    """
    video = Path(video)
    if how in ("auto", "sub"):
        cues, name = from_subtitle(video)
        if cues:
            return cues, f"subtitle:{name}"
        if how == "sub":
            return [], None

    if how in ("auto", "asr"):
        cues, name = from_asr(video, duration, backend=backend, model=model,
                              window=window, on_progress=on_progress)
        if cues:
            return cues, f"asr:{name}"

    return [], None


def save(path: Path, video, duration: float, cues, source: str):
    """落盘成 transcript.json。cues 存成 [[秒, 文本], ...]，比字典省一半体积。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video": {"file": Path(video).name, "path": str(video), "duration": round(duration, 3)},
        "source": source,
        "cues": [[round(t, 2), txt] for t, txt in cues],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Path):
    """读回 transcript.json，返回 (cues, source)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return [], None
    cues = [(float(t), str(txt)) for t, txt in data.get("cues", [])]
    return cues, data.get("source")


def slice_text(cues, start: float, end: float, head: float = 45.0, limit: int = 400):
    """取一段开头的台词，拼成给标题模型看的文本。

    只取开头 head 秒：章节标题要概括"这段开始发生了什么"，
    整段 4 分钟的台词塞进去反而稀释重点。
    """
    stop = min(end, start + head)
    parts = [txt for t, txt in cues if start - 0.5 <= t < stop]
    text = " ".join(parts)
    if len(text) > limit:
        cut = text[:limit]
        for sep in ("。", "！", "？", ". ", "! ", "? "):
            i = cut.rfind(sep)
            if i > limit * 0.6:
                cut = cut[: i + 1]
                break
        text = cut.rstrip() + "…"
    return text

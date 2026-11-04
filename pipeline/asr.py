# -*- coding: utf-8 -*-
"""
语音转写层：把视频按区间转成文本。

两个后端：
- sensevoice：阿里 SenseVoice-Small（234M，非自回归），中文 CER 约 7.8%，
  CPU 上就有十几倍实时，走 sherpa-onnx，不需要 PyTorch。
- whisper：faster-whisper，多语言兜底，中文明显弱于 SenseVoice。

对外只暴露 transcribe_chapters()，返回每个区间一段文本。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

SAMPLE_RATE = 16000
TAG_RE = re.compile(r"<\|[^|]*\|>")


def model_dir() -> Path:
    env = os.environ.get("STANZA_MODEL_DIR")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "Stanza" / "models"


def sense_voice_dir() -> Path:
    return model_dir() / "sense-voice"


def sense_voice_ready() -> bool:
    d = sense_voice_dir()
    return (d / "model.int8.onnx").exists() and (d / "tokens.txt").exists()


def backend_available(name: str) -> bool:
    if name == "sensevoice":
        if not sense_voice_ready():
            return False
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except Exception:
            return False
    if name == "whisper":
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False
    return False


def _patch_av():
    """PyAV 19 去掉了 av.open(metadata_errors=...)，faster-whisper 还在传。"""
    try:
        import av
    except Exception:
        return
    if getattr(av, "_stanza_patched", False):
        return
    original = av.open

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original(*args, **kwargs)

    av.open = open_compat
    av._stanza_patched = True


def decode_audio(path) -> np.ndarray:
    """整条音轨解成 16k 单声道 float32，音频解码比视频便宜得多。"""
    _patch_av()
    from faster_whisper.audio import decode_audio as fw_decode

    return fw_decode(str(path), sampling_rate=SAMPLE_RATE)


def _clean(text: str) -> str:
    return TAG_RE.sub("", text or "").replace("\n", " ").strip()


class _SenseVoice:
    def __init__(self, threads: int = 8):
        import sherpa_onnx

        d = sense_voice_dir()
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=threads,
            use_itn=True,
            debug=False,
        )

    def run(self, samples: np.ndarray) -> str:
        stream = self.rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.rec.decode_stream(stream)
        return _clean(stream.result.text)


class _Whisper:
    def __init__(self, size: str, threads: int = 8):
        from faster_whisper import WhisperModel

        try:
            # 模型已缓存时不要联网，避免代理不通时整个流程卡住
            self.model = WhisperModel(
                size, device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True
            )
        except Exception:
            self.model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads)

    def run(self, samples: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            samples, vad_filter=True, beam_size=1, condition_on_previous_text=False
        )
        return _clean(" ".join(s.text for s in segments))


def open_engine(backend="sensevoice", model="small", threads: int | None = None):
    """构造一个识别器。整片转写只该构造一次——模型加载比识别本身还慢。"""
    threads = threads or min(8, os.cpu_count() or 4)
    return _SenseVoice(threads) if backend == "sensevoice" else _Whisper(model, threads)


def transcribe_windows(video_path, window: float = 30.0, backend="sensevoice", model="small",
                       on_progress=None, max_seconds: float | None = None):
    """
    整片转写，返回 [(起秒, 文本)]。

    窗口不重叠，时间精度 ±window 秒。章节边界后面还要吸附到镜头切点，
    没必要为了更细的时间戳去做什么强制对齐。
    """
    import math

    try:
        audio = decode_audio(video_path)
    except Exception as exc:
        # 没有音轨的文件（纯画面 / 测试素材）会让 PyAV 抛 IndexError，
        # 这种片子本来就转写不出东西，别拖垮整条分析链
        print(f"      音频解码失败，可能没有音轨：{exc}")
        return []
    duration = len(audio) / SAMPLE_RATE
    if max_seconds:
        duration = min(duration, max_seconds)
    engine = open_engine(backend, model)

    total = max(1, int(math.ceil(duration / window)))
    out = []
    for i in range(total):
        a = int(i * window * SAMPLE_RATE)
        b = min(len(audio), int((i + 1) * window * SAMPLE_RATE))
        if b - a < SAMPLE_RATE * 0.8:
            break
        try:
            text = engine.run(audio[a:b]).strip()
        except Exception:
            text = ""
        if text:
            out.append((i * window, text))
        if on_progress:
            on_progress(i + 1, total)
    return out


# ---------------------------------------------------------------- 按句切分

def _normalize(audio, target_db: float = -20.0):
    """
    把整体音量拉到 -20 dBFS 左右。

    实测一个「轻语」vlog 整体只有 -41 dBFS，silero VAD 直接判不出人声，
    27 分钟只切出 60 秒语音，台词全挤在前 8 分钟。轻语音轨必须先补增益。
    已经很响的音频不动，避免削波。
    """
    rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-9)
    cur = 20 * np.log10(rms)
    if cur >= target_db:
        return audio.astype(np.float32, copy=False)
    gain = 10 ** ((target_db - cur) / 20)
    # 必须显式转回 float32：float32 * Python float 会变成 float64，
    # silero 的 onnx 输入只接受 float32，否则直接 INVALID_ARGUMENT
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


def _snap_to_silence(audio, t: float, radius: float = 2.0) -> float:
    """把切点挪到附近能量最低处，别把一句话从中间截断。"""
    a = max(0, int((t - radius) * SAMPLE_RATE))
    b = min(len(audio), int((t + radius) * SAMPLE_RATE))
    hop = 160                       # 10ms 一帧
    n = (b - a) // hop
    if n < 4:
        return t
    seg = audio[a:a + n * hop].reshape(n, hop)
    energy = np.sqrt(np.mean(seg ** 2, axis=1))
    return a / SAMPLE_RATE + int(np.argmin(energy)) * hop / SAMPLE_RATE


def vad_segments(audio, min_silence: float = 0.35, min_speech: float = 0.3,
                 max_speech: float = 12.0):
    """
    用 silero VAD 切出语音段，返回 [(起秒, 止秒)]。

    faster-whisper 包里自带 silero_vad_v6.onnx，直接拿来用，不用另外下载。
    注意 sherpa_onnx 自己的 VAD 接口不认 v6 格式，会报 Unsupported silero vad model。
    """
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception as exc:
        print(f"      VAD 不可用（{exc}）")
        return []

    opts = VadOptions(
        min_silence_duration_ms=int(min_silence * 1000),
        min_speech_duration_ms=int(min_speech * 1000),
        max_speech_duration_s=max_speech,
    )
    try:
        stamps = get_speech_timestamps(audio, opts, sampling_rate=SAMPLE_RATE)
    except Exception as exc:
        print(f"      VAD 切分失败（{exc}）")
        return []
    return [(s["start"] / SAMPLE_RATE, s["end"] / SAMPLE_RATE) for s in stamps]


def transcribe_sentences(video_path=None, audio=None, backend="sensevoice", model="small",
                         on_progress=None, min_speech: float = 0.5):
    """
    先 VAD 切句，再逐句转写，返回 [(起秒, 文本)]。

    固定 30 秒窗口只能把章节边界定在 ±30 秒；按句切之后是 ±1 秒。
    代价是多几千次短推理——SenseVoice 十几倍实时，整片还是十分钟量级。
    """
    if audio is None:
        try:
            audio = decode_audio(video_path)
        except Exception as exc:
            print(f"      音频解码失败，可能没有音轨：{exc}")
            return []

    audio = _normalize(audio)
    dur = len(audio) / SAMPLE_RATE
    segs = vad_segments(audio)

    covered = sum(b - a for a, b in segs)
    if not segs or covered < dur * 0.25:
        # 口播、强 BGM、轻语音轨上 silero 会大面积漏检——27 分钟只认出 60 秒。
        # 与其只转前 1/3，不如退回固定窗口，切点再吸附到最近的静音处。
        print(f"      VAD 只覆盖 {covered:.0f}s / {dur:.0f}s，改用 10 秒窗口")
        cuts = sorted({round(_snap_to_silence(audio, t), 3)
                       for t in np.arange(10.0, dur, 10.0)})
        bounds = [0.0] + [c for c in cuts if 0.5 < c < dur - 0.5] + [dur]
        segs = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    engine = open_engine(backend, model)
    total = len(segs)
    out = []
    for i, (a, b) in enumerate(segs):
        s = max(0, int(a * SAMPLE_RATE))
        e = min(len(audio), int(b * SAMPLE_RATE))
        if (e - s) >= SAMPLE_RATE * min_speech:
            try:
                text = _clean(engine.run(audio[s:e]))
            except Exception:
                text = ""
            if text:
                out.append((round(a, 2), text))
        if on_progress:
            on_progress(i + 1, total)
    return out


def transcribe_whisper(video_path=None, audio=None, model="small", chunk: float = 600.0,
                       on_progress=None):
    """
    whisper 路径：它自己带 VAD 和句级时间戳，分块转写即可。

    分块是为了控制内存和长音频的时间戳漂移；块与块不重叠——
    重叠会让同一句话出现两次，后面还得去重。
    """
    import math

    if audio is None:
        try:
            audio = decode_audio(video_path)
        except Exception as exc:
            print(f"      音频解码失败，可能没有音轨：{exc}")
            return []

    engine = open_engine("whisper", model)
    total = max(1, int(math.ceil(len(audio) / SAMPLE_RATE / chunk)))
    out = []
    for k in range(total):
        a = int(k * chunk * SAMPLE_RATE)
        b = min(len(audio), int((k + 1) * chunk * SAMPLE_RATE))
        try:
            segs, _info = engine.model.transcribe(
                audio[a:b], vad_filter=True, beam_size=1, condition_on_previous_text=False
            )
            segs = list(segs)
        except Exception as exc:
            print(f"      第 {k + 1}/{total} 块转写失败：{exc}")
            segs = []
        for s in segs:
            txt = _clean(s.text)
            if txt:
                out.append((round(a / SAMPLE_RATE + s.start, 2), txt))
        if on_progress:
            on_progress(k + 1, total)
    return out


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LAT_RE = re.compile(r"[A-Za-z]")


def detect_language(video_path=None, audio=None, backend="sensevoice", model="small",
                    samples: int = 4, window: float = 30.0):
    """
    抽几段听一下，判断片源主要说什么语言。返回 "zh" / "en" / None。

    只需要回答「要不要换 whisper」，不做精细语种识别：
    中文片 CJK 字符占多数，英语片基本没有 CJK。
    """
    if audio is None:
        try:
            audio = decode_audio(video_path)
        except Exception:
            return None
    if len(audio) < SAMPLE_RATE * 5:
        return None

    audio = _normalize(audio)
    engine = open_engine(backend, model)
    dur = len(audio) / SAMPLE_RATE
    cjk = lat = 0
    for i in range(samples):
        start = int(dur * (i + 0.5) / samples * SAMPLE_RATE)
        seg = audio[start:start + int(window * SAMPLE_RATE)]
        if len(seg) < SAMPLE_RATE:
            continue
        try:
            text = engine.run(seg)
        except Exception:
            continue
        cjk += len(CJK_RE.findall(text))
        lat += len(LAT_RE.findall(text))

    if cjk + lat < 20:      # 采样几乎没识别出字，别硬判
        return None
    return "zh" if cjk >= lat else "en"


def transcribe_chapters(video_path, ranges, backend="sensevoice", model="small", on_progress=None):
    """
    ranges: [(开始秒, 结束秒)]，通常就是每章开头 30 秒。
    返回与 ranges 等长的文本列表（识别不到内容的位置为空串）。
    """
    try:
        audio = decode_audio(video_path)
    except Exception as exc:
        print(f"      音频解码失败，可能没有音轨：{exc}")
        return ["" for _ in ranges]
    engine = open_engine(backend, model)

    texts = []
    for i, (start, end) in enumerate(ranges):
        a = max(0, int(start * SAMPLE_RATE))
        b = min(len(audio), a + int(max(0.5, end - start) * SAMPLE_RATE))
        if b - a < SAMPLE_RATE:
            texts.append("")
        else:
            try:
                texts.append(engine.run(audio[a:b]))
            except Exception:
                texts.append("")
        if on_progress:
            on_progress(i + 1, len(ranges))

    if on_progress:
        on_progress(len(ranges), len(ranges))
    return texts

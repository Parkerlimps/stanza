#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
章节分析管线：镜头检测 -> 动态规划聚合章节 -> 关键帧缩略图 -> chapters.json

只依赖 opencv-python + numpy，不调用 ffmpeg 命令行。

用法:
    python analyze.py ../samples/bbb.mp4 --out ../data --target 40
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

try:
    import asr as _asr
    import titles as _titles
except Exception:
    _asr = None
    _titles = None

try:
    import ocrsub as _ocr
except Exception:
    _ocr = None

try:
    import transcript as _transcript
except Exception:
    _transcript = None

try:
    import segment as _segment
except Exception:
    _segment = None

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 文本

# 字幕解析、全片转写都在 transcript.py；这里只负责「把文本变成章节标题」。


# ---------------------------------------------------------------- 镜头检测

def downscale(frame, target_w=96):
    """
    缩到 ~96px 宽，保持原始宽高比。

    插值方式按倍率选：缩小 3.5 倍以内用 INTER_AREA（质量好且不慢），
    超过就用 INTER_LINEAR——实测 9 倍降采样下 INTER_AREA 是 0.79ms/帧，
    INTER_LINEAR 只要 0.011ms，差 70 倍，而直方图统计根本看不出这点抗锯齿差异。
    """
    h, w = frame.shape[:2]
    ratio = w / float(target_w)
    tw = target_w
    th = max(2, int(round(h * tw / w)))
    interp = cv2.INTER_AREA if ratio < 3.5 else cv2.INTER_LINEAR
    return cv2.resize(frame, (tw, th), interpolation=interp)


def emit(payload: dict):
    """给上层（Electron）读的机器可读进度，前面加 PROGRESS 前缀便于过滤。"""
    print("PROGRESS " + json.dumps(payload, ensure_ascii=False), flush=True)


def scan_segment(video, start: float, end: float, sample_fps: float, counter=None):
    """
    扫描 [start, end) 区间，返回 [(时间, 相邻帧差异)]。
    只回传分数不回传帧，避免多进程传输几十 MB 的图像数据。
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if start > 0.05:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    first = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
    step = max(1, int(round(fps / sample_fps)))

    out = []
    prev = None
    i = 0
    while True:
        if not cap.grab():
            break
        t = (first + i) / fps
        if t >= end:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = downscale(frame)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            cur = (t, hist, gray)
            if prev is not None:
                out.append((t, frame_distance(prev, cur)))
            prev = cur
        i += 1
        if counter is not None and i % 120 == 0:
            try:
                counter.value = counter.value + 120
            except Exception:
                pass
    cap.release()
    return out


def _scan_worker(chunk_id, video, start, end, sample_fps, counter, results):
    try:
        results[chunk_id] = scan_segment(video, start, end, sample_fps, counter)
    except Exception as exc:  # 单个分片失败不该拖垮整体
        results[chunk_id] = []
        print(f"  分片 {chunk_id} 失败: {exc}", file=sys.stderr)


def scan_video(video, duration: float, total_frames: int, sample_fps: float, jobs: int, on_progress=None):
    """按 jobs 把时间轴切片并行扫描，返回按时间排序的 [(时间, 差异)]。"""
    if jobs <= 1 or duration < 120:
        t0 = time.perf_counter()
        res = scan_segment(video, 0.0, duration + 1.0, sample_fps)
        dt = time.perf_counter() - t0
        print(f"      单进程扫描，耗时 {dt:.2f}s")
        if on_progress:
            on_progress(1.0)
        return res

    import multiprocessing as mp

    mgr = mp.Manager()
    counter = mgr.Value('l', 0)
    results = mgr.dict()

    span = duration / jobs
    chunks = []
    for k in range(jobs):
        s = k * span
        e = duration + 1.0 if k == jobs - 1 else (k + 1) * span
        chunks.append((k, str(video), s, e, sample_fps, counter, results))

    procs = [mp.Process(target=_scan_worker, args=c) for c in chunks]
    t0 = time.perf_counter()
    for p in procs:
        p.start()

    last = -1
    while any(p.is_alive() for p in procs):
        time.sleep(0.25)
        if on_progress and total_frames:
            pct = min(0.995, counter.value / total_frames)
            if abs(pct - last) > 0.002:
                last = pct
                on_progress(pct)
    for p in procs:
        p.join()

    dt = time.perf_counter() - t0
    merged = []
    for k in range(jobs):
        merged.extend(results.get(k, []) or [])
    merged.sort(key=lambda x: x[0])
    speed = (total_frames / dt) if dt > 0 else 0
    print(f"      {jobs} 进程扫描，耗时 {dt:.2f}s（解码 {speed:.0f} 帧/秒，{len(merged)} 个采样点）")
    if on_progress:
        on_progress(1.0)
    return merged


def frame_distance(prev, cur) -> float:
    """
    0-1 的画面内容变化度。
    主信号用 HSV 直方图相关性——对镜头运动不敏感，对换景敏感；
    再叠一点点高百分位像素差，补偿「同色系换景」这种直方图看不出来的切换。
    """
    hist_a, gray_a = prev[1:]
    hist_b, gray_b = cur[1:]
    corr = cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL)
    d_hist = max(0.0, 1.0 - corr)

    d = np.abs(gray_b - gray_a)
    p95 = float(np.percentile(d, 95))
    return float(d_hist + 0.10 * min(1.0, p95 / 150.0))


def detect_shots(scores, threshold: float, min_gap: float):
    """
    输入 [(时间, 差异分数)]，输出切点 [(时间, 分数)]。
    先取所有超过阈值的采样点，再按强度从高到低做非极大值抑制，
    避免一次切换被相邻多个采样点重复记录，也避免被运动帧误触发。
    """
    if len(scores) < 3:
        return []
    cands = [(t, s) for t, s in scores if s >= threshold]
    cands.sort(key=lambda x: -x[1])

    accepted = []
    for t, s in cands:
        if all(abs(t - at) >= min_gap for at, _ in accepted):
            accepted.append((t, s))

    accepted.sort()
    return accepted


# ---------------------------------------------------------------- 章节聚合

def fill_long_gaps(cands, max_len, step):
    """在过长的空档里补「虚拟切点」（强度 0）。

    静态机位 / 口播类视频的镜头切点很少，相邻切点间隔可能远超 max_len。
    动态规划要求每一步长度都落在 [min_len, max_len] 内，一旦存在超长空档就
    找不到从 0 到片尾的合法路径，直接退化成「整片一个章节」
    （实测 27 分钟口播片 14 个切点 → 只有 1 章）。
    补虚拟点后 DP 始终有解；真实切点强度更高，仍然优先被选中。
    """
    out = [cands[0]]
    for t, d in cands[1:]:
        prev = out[-1][0]
        gap = t - prev
        if gap > max_len:
            n = max(2, math.ceil(gap / step))
            for k in range(1, n):
                out.append((prev + gap * k / n, 0.0))
        out.append((t, d))
    return out


def _fuse_semantic(cands, semantic, shot_scale=0.08):
    """
    把大模型给的分数并进候选切点。

    镜头分数和语义分数不是一回事：前者说「画面变了」，后者说「故事翻篇了」。
    语义模式下镜头点只当低权重参考，否则 3000 多个切点每个都带一点奖励，
    加起来足以把 32 章切成 39 章——每刀都"稍微划算"，但没人想要 39 章。
    """
    pts = [[t, shot_scale * min(1.0, d)] for t, d in cands]
    for t, s in semantic.items():
        for p in pts:
            if abs(p[0] - t) <= 3.0:
                p[1] = max(p[1], s)
                break
        else:
            pts.append([t, s])
    pts.sort(key=lambda x: x[0])
    # 首尾必须是 0——否则起点/终点会冒充强切点把奖励骗走
    if pts:
        pts[0][1] = 0.0
        pts[-1][1] = 0.0
    return [(t, s) for t, s in pts]


def build_chapters(cuts, duration, target, min_len, max_len, semantic=None, semantic_weight=0.6):
    """
    把镜头切点聚合成章节。动态规划：章节长度越接近 target 越好，
    切点强度越高越好，边界就会吸附到剧情转折处。

    semantic: {秒: 0-1}，大模型判断的「这里是新段落起点」的分数。
        它只作为切点奖励参与 DP，不直接决定边界——长度约束仍由 min_len/max_len 兜底。
        这样模型只需要做局部判断，全局均衡交给动态规划。
    """
    cands = [(0.0, 0.0)] + [(t, d) for t, d in cuts if 0.5 < t < duration - 0.5]
    cands.append((duration, 0.0))
    cands.sort()

    # 语义点必须赶在「切点太少就不分章」之前并进来：
    # 静态机位的片子可以一个镜头切点都没有，但剧情照样翻篇
    if semantic:
        cands = _fuse_semantic(cands, semantic)

    if len(cands) <= 2 or duration < min_len * 1.5:
        return [(0.0, duration, 0.0)]

    cands = fill_long_gaps(cands, max_len, max(target, min_len))
    n = len(cands)

    # 语义分数满值时够抵消 0.6 的长度代价，
    # 这样边界会真的跑到转折点上去，而不是被"均匀"拉回来
    bonus_scale = semantic_weight if semantic else 0.30

    # 有语义信号时放松长度约束：让"故事在哪里翻篇"主导段数，
    # 均匀只作为软偏好，硬边界仍由 min_len/max_len 兜。没有语义时保持原行为。
    length_scale = 0.35 if semantic else 1.0

    max_d = max((d for _, d in cands), default=1.0) or 1.0
    INF = float("inf")
    dp = [INF] * n
    back = [-1] * n
    dp[0] = 0.0

    for j in range(1, n):
        tj, sj = cands[j]
        for i in range(j - 1, -1, -1):
            ti = cands[i][0]
            length = tj - ti
            if length < min_len:
                continue
            if length > max_len:
                break
            if dp[i] == INF:
                continue
            length_cost = length_scale * ((length - target) / target) ** 2
            cut_bonus = bonus_scale * (sj / max_d)
            cost = dp[i] + length_cost - cut_bonus
            if cost < dp[j]:
                dp[j] = cost
                back[j] = i

    if dp[n - 1] == INF:
        return [(0.0, duration, 0.0)]

    chain = [n - 1]
    while chain[-1] != 0:
        chain.append(back[chain[-1]])
    chain.reverse()

    chapters = []
    for k in range(len(chain) - 1):
        s = cands[chain[k]][0]
        e = cands[chain[k + 1]][0]
        strength = cands[chain[k + 1]][1] / max_d
        chapters.append((s, e, strength))
    return chapters


# ---------------------------------------------------------------- 缩略图

def grab_thumb(cap, start: float, end: float, out_path: Path, width=480):
    """
    在一段内部采样多帧，挑「清晰度最高且曝光正常」的一帧当封面。
    单点取帧容易落在淡出黑场或转场上，那样章节列表会一片黑。
    """
    best_frame, best_score = None, -1.0
    span = max(0.0, end - start)
    for r in (0.18, 0.3, 0.42, 0.55, 0.7, 0.82):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + span * r) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90), interpolation=cv2.INTER_AREA)
        sharp = float(cv2.Laplacian(small, cv2.CV_64F).var())
        bright = float(small.mean())
        if bright < 26 or bright > 232:
            sharp *= 0.15
        if sharp > best_score:
            best_score, best_frame = sharp, frame
    if best_frame is None:
        return False

    h, w = best_frame.shape[:2]
    if w > width:
        best_frame = cv2.resize(best_frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 注意：cv2.imwrite 在非 ASCII 路径下会静默失败，必须自己编码后写字节
    ok, buf = cv2.imencode(".jpg", best_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return False
    out_path.write_bytes(buf.tobytes())
    return True


# ---------------------------------------------------------------- 主流程

def retitle(out_dir: Path, args):
    """
    只重做标题，不重新扫画面。
    台词文本存在 chapters.json 里；缺失的话才补转写。
    """
    jf = out_dir / "chapters.json"
    if not jf.exists():
        sys.exit(f"找不到 {jf}")

    data = json.loads(jf.read_text(encoding="utf-8"))
    chs = data.get("chapters", [])
    if not chs:
        sys.exit("chapters.json 里没有章节")

    texts = [c.get("text", "") for c in chs]
    missing = [i for i, t in enumerate(texts) if not t.strip()]
    if missing:
        # 这里不补转写：转写要解码整条音轨，重做标题会卡好几分钟。
        # 缺文本的段就让它回退成序号，想补文本请重新分析。
        print(f"      {len(missing)} 段没有台词文本，这些段保留序号")

    emit({"stage": "titles", "label": "生成小标题", "pct": 8.0})

    def on_title(done, total):
        emit({
            "stage": "titles",
            "label": f"生成小标题 {done}/{total} 批",
            "pct": round(8.0 + (done / max(1, total)) * 88.0, 1),
        })

    titles, how = _titles.build(texts, args.llm, args.llm_model, on_progress=on_title, two_stage=args.two_stage)

    # 用户明确要求用大模型，但没连上：保留原标题，别把已有结果降级成台词原句
    if args.llm and how.startswith("heuristic"):
        print(f"大模型不可用（{how}），保留原有标题")
        emit({"stage": "warn", "label": "大模型不可用，已保留原标题", "pct": 100.0})
        return

    named = 0
    for i, c in enumerate(chs):
        t = titles[i] if i < len(titles) else ""
        if t:
            named += 1
        c["title"] = t or f"第 {c.get('index', i) + 1} 段"

    data["chapters"] = chs
    jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"标题已更新：{named}/{len(chs)} 段有语义标题（{how}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", help="视频路径；用 --retitle 时可以省略")
    ap.add_argument("--retitle", default=None, metavar="DIR", help="只重做标题，DIR 为已分析的输出目录")
    ap.add_argument("--out", default="../data")
    ap.add_argument("--target", type=float, default=40.0, help="目标章节长度（秒）")
    ap.add_argument("--min", type=float, default=16.0, dest="min_len")
    ap.add_argument("--max", type=float, default=200.0, dest="max_len")
    ap.add_argument("--threshold", type=float, default=0.20, help="镜头切分阈值 0-1，越大越保守")
    ap.add_argument("--sample-fps", type=float, default=5.0)
    ap.add_argument("--thumb-width", type=int, default=480)
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数，0 表示按 CPU 自动")
    ap.add_argument("--count", type=int, default=0, help="期望章节数，给了就按它推算目标长度")
    ap.add_argument("--asr", choices=["auto", "off"], default="auto", help="没有字幕时是否转写生成段名")
    ap.add_argument("--two-stage", action="store_true", dest="two_stage",
                    help="标题分两步生成（先摘要再起名），慢 3 倍，小模型收益不明显")
    ap.add_argument("--segment", choices=["auto", "llm", "equal"], default="auto",
                    help="切分方式：llm 用大模型找剧情转折，equal 只按镜头和时长")
    ap.add_argument("--transcript", choices=["auto", "sub", "asr", "off"], default="auto",
                    help="台词来源：auto 外挂字幕优先、没有就转写；off 不提取")
    ap.add_argument("--seg-step", type=float, default=0.0, dest="seg_step",
                    help="语义候选点间隔（秒），0 表示按目标章节长度自动")
    ap.add_argument("--seg-window", type=float, default=60.0, dest="seg_window",
                    help="判断候选点时，前后各取多少秒台词")
    ap.add_argument("--semantic-weight", type=float, default=0.6, dest="semantic_weight",
                    help="语义切点在动态规划里的权重，越大越倾向在转折处切")
    ap.add_argument("--llm-workers", type=int, default=2, dest="llm_workers",
                    help="并发请求数；NPU 上别超过 2，GPU 可以 3-4")
    ap.add_argument("--reuse", default=None, metavar="DIR",
                    help="复用另一个输出目录里的镜头切点，跳过逐帧扫描")
    ap.add_argument("--force-text", action="store_true", dest="force_text",
                    help="忽略已缓存的 transcript.json，重新提取台词")
    ap.add_argument("--source", choices=["auto", "ocr", "asr"], default="auto",
                    help="文本来源：auto 先探硬字幕，探不到再转写")
    ap.add_argument("--ocr-min", type=float, default=0.45, dest="ocr_min", help="判定有硬字幕的命中率阈值")
    ap.add_argument("--ocr-samples", type=int, default=8, dest="ocr_samples", help="探测字幕时抽样帧数")
    ap.add_argument("--backend", choices=["auto", "sensevoice", "whisper"], default="auto",
                    help="转写后端：auto 按片源语言选（中文 SenseVoice / 英语 whisper）")
    ap.add_argument("--model", default="small", help="whisper 用的模型：tiny / base / small / medium / large-v3")
    ap.add_argument("--language", default=None, help="强制语言 zh / en，默认自动识别")
    ap.add_argument("--llm", default=None, help="本地大模型 OpenAI 兼容地址，如 http://127.0.0.1:1234")
    ap.add_argument("--llm-model", default=None, dest="llm_model", help="模型名，如 qwen3.5-4b")
    args = ap.parse_args()
    # 用环境变量也能配，这样不用重新打包 exe
    if not args.llm:
        args.llm = os.environ.get("STANZA_LLM_URL") or None
    if not args.llm_model:
        args.llm_model = os.environ.get("STANZA_LLM_MODEL") or None

    if args.retitle:
        retitle(Path(args.retitle).resolve(), args)
        return
    if not args.video:
        sys.exit("需要视频路径，或用 --retitle 指定已分析的目录")

    video = Path(args.video).resolve()
    if not video.exists():
        sys.exit(f"找不到视频: {video}")
    out_dir = Path(args.out).resolve()
    thumb_dir = out_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        sys.exit("无法打开视频，可能是编码不被 OpenCV 支持")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = total / fps if total else 0.0

    jobs = args.jobs if args.jobs > 0 else max(2, min(6, (os.cpu_count() or 4) // 2 - 1))
    if duration < 120:
        jobs = 1
    if args.count > 0:
        args.target = max(12.0, duration / args.count)
        # 目标长度变了，最短/最长段也得跟着走，否则短片上约束互相打架
        args.min_len = max(5.0, args.target * 0.45)
        args.max_len = max(args.min_len * 2.5, args.target * 3.5)

    # 复用已算好的镜头切点：逐帧扫描是整条链路最贵的一步，
    # 只调分段参数时没必要重来
    cuts = []
    reused = None
    if args.reuse:
        prev = Path(args.reuse).resolve() / "chapters.json"
        if prev.is_file():
            try:
                data = json.loads(prev.read_text(encoding="utf-8"))
                cuts = [(float(s["t"]), float(s.get("score", 0))) for s in data.get("shots", [])]
                reused = prev
            except Exception as exc:
                print(f"      复用 {prev} 失败（{exc}），改为重新扫描")
                cuts = []

    if cuts:
        print(f"[1/5] 扫描  复用 {reused.parent.name} 的 {len(cuts)} 个镜头切点")
        emit({"stage": "sample", "label": "复用镜头切点", "pct": 62.0})
        print(f"[2/5] 镜头检测  跳过")
    else:
        print(f"[1/5] 扫描  {video.name}  {width}x{height}  {fps:.2f}fps  {duration/60:.1f}min  并发 {jobs}")

        def on_scan(frac):
            emit({"stage": "sample", "label": "逐帧扫描", "pct": round(frac * 62.0, 1)})

        scores = scan_video(video, duration, total, args.sample_fps, jobs, on_scan)
        if len(scores) < 3:
            sys.exit("视频太短，无法分析")

        print(f"[2/5] 镜头检测  采样点 {len(scores)}")
        emit({"stage": "detect", "label": "识别镜头切换", "pct": 63.0})
        cuts = detect_shots(scores, args.threshold, min_gap=0.8)
        print(f"      检出镜头切点 {len(cuts)} 个")

    # 台词必须在分章之前拿到：语义分段就是靠它判断哪里翻篇
    print(f"[3/5] 提取台词  {args.transcript}")
    emit({"stage": "text", "label": "提取全片台词", "pct": 65.0})

    def on_text(done, total):
        emit({
            "stage": "text",
            "label": f"转写台词 {done}/{total} 段",
            "pct": round(65.0 + (done / max(1, total)) * 20.0, 1),
        })

    cues, text_source = [], None
    cache_tp = out_dir / "transcript.json"
    if args.transcript != "off" and not args.force_text and cache_tp.is_file():
        got, src = _transcript.load(cache_tp)
        if got:
            cues, text_source = got, (src or "cache")
            print(f"      复用台词缓存 {len(cues)} 条（--force-text 可重转）")

    if not cues and args.transcript != "off" and _transcript:
        cues, text_source = _transcript.extract(
            video, duration, how=args.transcript, backend=args.backend,
            model=args.model, on_progress=on_text,
        )
    if cues:
        _transcript.save(out_dir / "transcript.json", video, duration, cues, text_source)
        last_m, last_s = divmod(int(cues[-1][0]), 60)
        print(f"      台词 {len(cues)} 条，来源 {text_source}，最后一条 {last_m:02d}:{last_s:02d}")
    else:
        print("      没有提取到台词，段名只能退化")

    # 语义分段：大模型只给候选点打分，切不切由 DP 决定
    semantic = {}
    seg_mode = "equal"
    if cues and args.segment in ("auto", "llm") and args.llm and args.llm_model and _segment:
        step = args.seg_step if args.seg_step > 0 else max(90.0, args.target * 0.7)
        cands_seg = _segment.candidates(duration, step)
        print(f"[4/5] 语义分段  候选 {len(cands_seg)} 个，间隔 {step:.0f}s")
        emit({"stage": "segment", "label": "大模型判断段落边界", "pct": 86.0})

        def on_seg(done, total):
            emit({
                "stage": "segment",
                "label": f"判断段落 {done}/{total}",
                "pct": round(86.0 + (done / max(1, total)) * 9.0, 1),
            })

        semantic = _segment.score(
            cues, cands_seg, args.llm, args.llm_model, half=args.seg_window,
            workers=args.llm_workers, on_progress=on_seg,
        )
        if semantic:
            seg_mode = "llm"
            print(f"      语义切点 {len(semantic)} 个（分数 >= 0.5）")
        else:
            print("      模型没找到明确转折，退回按镜头/时长切")
    elif cues:
        why = f"segment={args.segment}" if (args.llm and args.llm_model) else "没有配置大模型"
        print(f"[4/5] 语义分段  跳过（{why}）")

    print(f"      聚合章节  target={args.target:.0f}s  模式 {seg_mode}")
    emit({"stage": "merge", "label": "归并剧情段落", "pct": 95.0})
    chapters = build_chapters(cuts, duration, args.target, args.min_len, args.max_len,
                              semantic=semantic or None,
                              semantic_weight=args.semantic_weight)

    titles = [""] * len(chapters)
    texts = [""] * len(chapters)
    source = text_source or "none"

    if cues:
        texts = [_transcript.slice_text(cues, s, e) for s, e, _ in chapters]
        filled = sum(1 for t in texts if t.strip())
        print(f"      台词覆盖 {filled}/{len(texts)} 段")
    else:
        # 没有全片台词（transcript=off 或没配大模型）：只为起标题找文本，
        # 不需要整片，读每段开头就够
        if args.source in ("auto", "ocr") and _ocr and _ocr.available():
            # 画面里的硬字幕是官方译文，比听声音准，优先
            ranges = [(s, e) for s, e, _ in chapters]
            emit({"stage": "ocr", "label": "探测画面字幕", "pct": 95.5})
            rate = _ocr.detect(video, duration, samples=args.ocr_samples)
            print(f"      字幕命中率 {rate:.0%}（阈值 {args.ocr_min:.0%}）")
            if rate >= args.ocr_min:
                def on_ocr(done, total):
                    emit({
                        "stage": "ocr",
                        "label": f"读字幕 {done}/{total} 段",
                        "pct": round(95.5 + (done / max(1, total)) * 1.5, 1),
                    })

                texts = _ocr.read_chapters(video, ranges, on_ocr)
                source = "ocr"
                print(f"      读到 {sum(1 for t in texts if t.strip())}/{len(texts)} 段字幕")

        if not any(t.strip() for t in texts) and args.source in ("auto", "asr") \
                and _asr and _asr.backend_available(args.backend):
            ranges = [(s, min(e, s + 45.0)) for s, e, _ in chapters]
            emit({"stage": "asr", "label": "转写每段开头", "pct": 95.5})

            def on_head(done, total):
                emit({
                    "stage": "asr",
                    "label": f"转写段首 {done}/{total}",
                    "pct": round(95.5 + (done / max(1, total)) * 1.5, 1),
                })

            try:
                texts = _asr.transcribe_chapters(
                    video, ranges, backend=args.backend, model=args.model, on_progress=on_head
                )
                source = args.backend
                print(f"      段首转写 {sum(1 for t in texts if t.strip())}/{len(texts)} 段")
            except Exception as exc:
                print(f"      段首转写失败：{exc}")

    if any(t.strip() for t in texts):
        emit({"stage": "titles", "label": "生成小标题", "pct": 97.0})
        titles, how = _titles.build(texts, args.llm, args.llm_model, two_stage=args.two_stage)
        print(f"      文本来源：{source}；标题来源：{how}")
    else:
        print("      没有可用文本，段名用序号")

    print("[5/5] 生成缩略图")
    # 重跑时章节数会变，上一轮多出来的编号会一直留在目录里，先清干净
    for old in thumb_dir.glob("*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass

    items = []
    for i, (s, e, strength) in enumerate(chapters):
        emit({
            "stage": "thumb",
            "label": "生成预览图",
            "pct": round(98.0 + (i / max(1, len(chapters))) * 2.0, 1),
            "done": i + 1,
            "total": len(chapters),
        })
        name = f"{i:03d}.jpg"
        grab_thumb(cap, s, e, thumb_dir / name, args.thumb_width)
        title = (titles[i] if i < len(titles) else "") or f"第 {i + 1} 段"
        items.append({
            "index": i,
            "start": round(s, 3),
            "end": round(e, 3),
            "duration": round(e - s, 3),
            "title": title,
            "text": texts[i] if i < len(texts) else "",
            "strength": round(strength, 3),
            "thumb": f"thumbs/{name}",
        })

    cap.release()

    payload = {
        "video": {
            "file": video.name,
            "path": str(video),
            "duration": round(duration, 3),
            "width": width,
            "height": height,
            "fps": round(fps, 3),
        },
        "generator": {
            "target": args.target,
            "threshold": args.threshold,
            "segment": seg_mode,
            "text_source": text_source,
            "semantic_points": len(semantic),
            "cues": len(cues),
        },
        "chapters": items,
        "shots": [{"t": round(t, 3), "score": round(d, 2)} for t, d in cuts],
    }

    out_json = out_dir / "chapters.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({"stage": "done", "label": "完成", "pct": 100.0})

    print(f"\n完成 -> {out_json}  共 {len(items)} 章")
    for it in items:
        m, s = divmod(int(it["start"]), 60)
        print(f"  {m:02d}:{s:02d}  {it['duration']:5.1f}s  {it['title']}")


if __name__ == "__main__":
    main()

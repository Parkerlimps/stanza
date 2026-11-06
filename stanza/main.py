"""Stanza — libmpv 版主窗口。

布局：顶栏 /（视频区 | 章节抽屉）/ 底部控制条。
键盘全部在主窗口处理；mpv 只留鼠标默认绑定（视频区单击暂停）。
"""
import os
import sys
from pathlib import Path

# PyInstaller --windowed 下 sys.stdout / sys.stderr 是 None，任何 print 都会炸
if sys.stdout is None or sys.stderr is None:
    class _Sink:
        encoding = "utf-8"

        def write(self, *_a):
            pass

        def flush(self):
            pass

        def isatty(self):
            return False

    if sys.stdout is None:
        sys.stdout = _Sink()
    if sys.stderr is None:
        sys.stderr = _Sink()

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import app_dir, dll_dirs, resource  # noqa: E402

ROOT = app_dir()
VENDOR = ROOT / "vendor"

# libmpv-2.dll 必须在 import mpv 之前可见。
# 开发态在 vendor/，打包后被 PyInstaller 放进 _internal/，两个都要试。
for _d in dll_dirs():
    os.environ["PATH"] = str(_d) + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(str(_d))
    except Exception:
        pass

import locale
locale.setlocale(locale.LC_NUMERIC, "C")

import json
import time as _time
import traceback

from PySide6.QtCore import Qt, QTimer, Signal, QUrl, QEvent
from PySide6.QtGui import (QAction, QKeySequence, QShortcut, QPixmap, QFont, QIcon,
                           QDragEnterEvent, QDropEvent)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout,
                               QHBoxLayout, QFrame, QScrollArea, QFileDialog, QDialog,
                               QLineEdit, QPushButton, QMessageBox, QSizePolicy, QMenu,
                               QToolButton)

from config import (LIB_DIR, load_config, save_config, chapters_for, out_dir_for,
                    cache_key)
from player import (MpvVideo, ChapterRail, Dock, IconButton, SPEEDS, fmt,
                    BG, FG, FG_DIM, STROKE)
from analysis import AnalysisRunner
from llm import LlmStarter

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".mp3", ".flac", ".wav"}


class ChapterDrawer(QFrame):
    """右侧章节列表：缩略图 + 标题（两行截断）+ 时间。"""
    jumpRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drawer")
        self.setFixedWidth(316)
        self.setStyleSheet(
            f"#drawer{{background:#fff;border-left:1px solid {STROKE};}}")

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QWidget()
        head.setFixedHeight(46)
        h = QHBoxLayout(head)
        h.setContentsMargins(16, 0, 12, 0)
        title = QLabel("章节")
        title.setStyleSheet(f"font-size:13px;font-weight:700;color:{FG};")
        self.countLbl = QLabel("0")
        self.countLbl.setStyleSheet(
            f"color:{FG_DIM};font-size:11px;border:1px solid {STROKE};"
            "border-radius:10px;padding:2px 8px;")
        h.addWidget(title)
        h.addStretch(1)
        h.addWidget(self.countLbl)
        v.addWidget(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;}"
                                  "QScrollBar{width:0;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(8, 0, 8, 12)
        self.list.setSpacing(2)
        self.list.addStretch(1)
        self.scroll.setWidget(inner)
        v.addWidget(self.scroll, 1)
        self._items: list[tuple[QWidget, QLabel]] = []

    def load(self, chapters: list, thumbs_dir: Path):
        while self.list.count() > 1:
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._items.clear()
        self.countLbl.setText(str(len(chapters)))
        for c in chapters:
            btn = QChapterItem(c, thumbs_dir)
            btn.clicked.connect(lambda _=False, s=c["start"]: self.jumpRequested.emit(s + 0.05))
            self.list.insertWidget(self.list.count() - 1, btn)
            self._items.append((btn, btn.titleLabel))

    def set_active(self, idx: int):
        for i, (w, _lbl) in enumerate(self._items):
            w.set_active(i == idx)
        if 0 <= idx < len(self._items) and self.isVisible():
            w = self._items[idx][0]
            self.scroll.ensureWidgetVisible(w, 0, 120)


class QChapterItem(QFrame):
    """章节条目。用 QFrame 而不是 QPushButton——后者会把内嵌 layout 挤扁，
    文字被压成一条线。"""

    clicked = Signal()

    def __init__(self, chapter: dict, thumbs_dir: Path):
        super().__init__()
        self.setObjectName("chap")
        self.setProperty("active", False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(70)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "QFrame#chap{border:0;border-radius:12px;background:transparent;}"
            "QFrame#chap:hover{background:rgba(0,0,0,0.05);}"
            "QFrame#chap[active=\"true\"]{background:rgba(0,0,0,0.09);}")

        self.titleLabel = QLabel(str(chapter.get("title", "")))
        # 允许折成两行：标题长的时候不硬切，最多占两行（约 30 个字）
        self.titleLabel.setWordWrap(True)
        self.titleLabel.setMaximumHeight(36)
        self.titleLabel.setStyleSheet(f"color:{FG};font-size:12px;font-weight:600;background:transparent;")
        meta = QLabel(f"{fmt(chapter.get('start', 0))} · {int(chapter.get('duration', 0))}s")
        meta.setStyleSheet(f"color:{FG_DIM};font-size:11px;background:transparent;")

        thumb = QLabel()
        thumb.setFixedSize(88, 50)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:#f0f1f3;border-radius:8px;")
        tp = thumbs_dir / str(chapter.get("thumb", "")).replace("thumbs/", "")
        if tp.is_file():
            pm = QPixmap(str(tp))
            if not pm.isNull():
                thumb.setPixmap(pm.scaled(thumb.size(), Qt.KeepAspectRatioByExpanding,
                                          Qt.SmoothTransformation))

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 10, 6)
        row.setSpacing(10)
        row.addWidget(thumb)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addStretch(1)
        col.addWidget(self.titleLabel)
        col.addWidget(meta)
        col.addStretch(1)
        row.addLayout(col, 1)

    def set_active(self, on: bool):
        if self.property("active") != on:
            self.setProperty("active", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class LlmDialog(QDialog):
    """本地大模型配置（重做标题用）。与 Electron 版字段一致。"""

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.setWindowTitle("本地大模型")
        self.setFixedWidth(420)
        v = QVBoxLayout(self)
        v.setSpacing(10)
        hint = QLabel("用于把台词压成章节标题。留空则用规则截取台词。")
        hint.setStyleSheet(f"color:{FG_DIM};font-size:12px;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.url = QLineEdit(cfg.get("llmUrl", ""))
        self.url.setPlaceholderText("http://127.0.0.1:8080/v1")
        self.model = QLineEdit(cfg.get("llmModel", ""))
        self.model.setPlaceholderText("qwen3.5-4b-int4")
        self.launch = QLineEdit(cfg.get("llmLaunch", ""))
        self.launch.setPlaceholderText(r"D:\WorkSpace\npu-llm\start.bat（可选）")
        for label, w in (("接口地址", self.url), ("模型名", self.model), ("启动命令", self.launch)):
            v.addWidget(QLabel(label))
            v.addWidget(w)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        ok = QPushButton("保存")
        ok.setDefault(True)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        self.setStyleSheet(
            f"QDialog{{background:#fff;}} QLabel{{color:{FG};font-size:12px;}}"
            "QLineEdit{border:1px solid rgba(0,0,0,0.18);border-radius:8px;"
            "padding:6px 10px;font-size:13px;}"
            "QPushButton{border:1px solid rgba(0,0,0,0.18);border-radius:8px;"
            "padding:6px 16px;}")


class MainWindow(QMainWindow):
    # 分析任务状态
    _job_start = 0.0
    _job_first = 0.0

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stanza")
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        self.cfg = load_config()
        self.speed_idx = SPEEDS.index(1)
        self.chapters: list = []
        self.shots: list = []
        self.duration = 0.0
        self.current_path: str | None = None
        self.muted = False
        self._last_volume = 100
        self._ext_sub: Path | None = None
        self._ext_sub_id = None
        self._sub_candidates: list[Path] = []
        self.llm = None                 # LlmStarter，正在拉模型时非空
        self._job_kind = "analyze"      # analyze / retitle

        central = QWidget()
        central.setStyleSheet(f"background:{BG};")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)

        # ---- 顶栏
        top = QWidget()
        top.setFixedHeight(64)
        th = QHBoxLayout(top)
        th.setContentsMargins(24, 12, 24, 0)
        tcol = QVBoxLayout()
        tcol.setSpacing(1)
        self.fileName = QLabel("未加载视频")
        self.fileName.setStyleSheet(f"color:{FG};font-size:15px;font-weight:700;")
        self.fileMeta = QLabel("选择「文件 → 打开视频」开始，或把视频拖进来")
        self.fileMeta.setStyleSheet(f"color:{FG_DIM};font-size:11px;")
        tcol.addWidget(self.fileName)
        tcol.addWidget(self.fileMeta)
        th.addLayout(tcol)
        th.addStretch(1)
        self.nowPlaying = QLabel("")
        self.nowPlaying.setStyleSheet(
            f"color:{FG_DIM};font-size:12px;font-weight:600;"
            "border:1px solid rgba(0,0,0,0.08);border-radius:10px;padding:5px 12px;")
        self.nowPlaying.hide()
        th.addWidget(self.nowPlaying)
        lv.addWidget(top)

        # ---- 视频区（外层留白，mpv 原生窗口只占中间，跟原网页版同构）
        self.stage = QFrame()
        self.stage.setStyleSheet("background:#fff;")
        sv = QVBoxLayout(self.stage)
        sv.setContentsMargins(56, 6, 56, 22)
        sv.setSpacing(0)
        self.video = MpvVideo()
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        sv.addWidget(self.video)
        lv.addWidget(self.stage, 1)

        # ---- Dock
        dockWrap = QWidget()
        dv = QVBoxLayout(dockWrap)
        dv.setContentsMargins(36, 8, 36, 22)
        self.dock = Dock()
        dv.addWidget(self.dock)
        lv.addWidget(dockWrap)

        root.addWidget(left, 1)

        # ---- 抽屉
        self.drawer = ChapterDrawer()
        self.drawer.hide()
        root.addWidget(self.drawer)

        # ---- 菜单
        m = self.menuBar()
        mfile = m.addMenu("文件")
        mfile.addAction("打开视频…", QKeySequence("Ctrl+O"), self.open_file)
        self.act_analyze = mfile.addAction("分析当前视频章节", QKeySequence("Ctrl+R"), self.start_analyze)
        self.act_retitle = mfile.addAction("重做章节标题…", QKeySequence("Ctrl+T"), self.start_retitle)
        mfile.addSeparator()
        mfile.addAction("退出", QKeySequence("Ctrl+Q"), self.close)
        mhelp = m.addMenu("帮助")
        mhelp.addAction("打开缓存目录", self._open_cache)

        # ---- 分析任务
        self.runner = AnalysisRunner(self)
        self.runner.progress.connect(self._on_progress)
        self.runner.log_line.connect(self._on_log)
        self.runner.finished.connect(self._on_analyze_done)

        # ---- 轮询播放状态
        self.poll = QTimer(self)
        self.poll.setInterval(120)
        self.poll.timeout.connect(self._tick)
        self.poll.start()

        # ---- 空闲自动隐藏
        self.idle = QTimer(self)
        self.idle.setInterval(2600)
        self.idle.setSingleShot(True)
        self.idle.timeout.connect(self._go_idle)
        QApplication.instance().installEventFilter(self)

        # ---- dock 信号
        d = self.dock
        d.playClicked.connect(self.video.toggle_play)
        d.prevClicked.connect(lambda: self.jump_chapter(-1))
        d.nextClicked.connect(lambda: self.jump_chapter(1))
        d.speedClicked.connect(self.cycle_speed)
        d.muteClicked.connect(self.toggle_mute)
        d.drawerClicked.connect(self.toggle_drawer)
        d.fullClicked.connect(self.toggle_fullscreen)
        d.analyzeClicked.connect(self.start_analyze)
        d.retitleClicked.connect(self.start_retitle)
        d.countChanged.connect(self._on_count_changed)
        d.volumeChanged.connect(self.set_volume)
        d.seekRequested.connect(self._seek)
        d.set_count(self.cfg.get("chapterCount", "auto"))
        self.rail = d.rail
        self.rail.activeChanged.connect(self._on_chapter_change)
        self.rail.hoverChanged.connect(self._on_hover)
        self.video.fileLoaded.connect(self._on_file_loaded, Qt.QueuedConnection)
        self.drawer.jumpRequested.connect(lambda t: (self._seek(t), self.toggle_drawer(False)))

        # 命令行视频
        argv = [a for a in sys.argv[1:] if Path(a).suffix.lower() in VIDEO_EXT and Path(a).is_file()]
        if argv:
            QTimer.singleShot(200, lambda: self.load_video(argv[0]))

    # ================================================== 打开 / 加载
    def open_file(self):
        exts = " ".join("*" + e for e in VIDEO_EXT)
        path, _ = QFileDialog.getOpenFileName(self, "选择视频", "", f"视频 ({exts})")
        if path:
            self.load_video(path)

    def load_video(self, path: str):
        p = Path(path).resolve()
        if not p.is_file():
            return
        self.current_path = str(p)
        self.setWindowTitle(f"{p.name} — Stanza")
        self.fileName.setText(p.name)
        self.fileMeta.setText("正在加载…")
        self.chapters, self.shots, self.duration = [], [], 0.0
        self.drawer.load([], LIB_DIR)
        self.nowPlaying.hide()
        self.dock.set_time(0, 0)
        # 先找字幕再 loadfile：file-loaded 可能来得很快，避免竞态
        self._auto_load_subs(self.current_path)
        self.video.load(self.current_path)
        self._update_chapter_buttons()
        key, chapters_json = chapters_for(self.current_path)
        if chapters_json:
            self._apply_chapters(chapters_json)
        else:
            self.fileMeta.setText("尚无章节数据，可「文件 → 分析当前视频章节」")

    def _apply_chapters(self, json_path: Path):
        try:
            data = json.loads(json_path.read_text("utf-8"))
        except Exception as e:
            self.fileMeta.setText(f"章节数据读取失败：{e}")
            return
        self.chapters = data.get("chapters", [])
        self.shots = data.get("shots", [])
        vd = data.get("video", {})
        self.duration = float(vd.get("duration") or (self.chapters[-1]["end"] if self.chapters else 0))
        meta = " · ".join(x for x in [
            f"{vd.get('width')}×{vd.get('height')}" if vd.get("width") else "",
            f"{round(vd['fps'])} fps" if vd.get("fps") else "",
            fmt(vd.get("duration", 0)) if vd.get("duration") else "",
        ] if x)
        self.fileMeta.setText(meta or f"{len(self.chapters)} 个章节")
        thumbs = json_path.parent / "thumbs"
        self.rail.set_chapters(self.chapters, self.shots, self.duration)
        self.drawer.load(self.chapters, thumbs)

    def _update_chapter_buttons(self):
        has = bool(self.chapters)
        self.dock.btnAnalyze.setVisible(True)
        self.dock.btnRetitle.setVisible(has)
        self.act_retitle.setEnabled(has)

    # ================================================== 字幕
    SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".smi")

    def _auto_load_subs(self, video_path: str):
        """找同名外挂字幕（movie.srt / movie.zh.srt / movie.en.forced.srt…）。
        真正 sub-add 推迟到 file-loaded 之后——在 loadfile 之前加会被新文件覆盖掉。
        mpv 自己也会按 sub-auto=exact 自动挂 movie.srt，所以后面要按文件名去重。"""
        self._ext_sub = None
        self._ext_sub_id = None
        self._sub_candidates = []
        base = Path(video_path)
        stem = base.stem
        try:
            entries = list(base.parent.iterdir())
        except OSError:
            entries = []
        for f in entries:
            if not f.is_file() or f.suffix.lower() not in self.SUB_EXT:
                continue
            if f.stem == stem or f.stem.startswith(stem + "."):
                self._sub_candidates.append(f)
        # 精确同名优先（movie.srt 排在 movie.zh.srt 前面）
        self._sub_candidates.sort(key=lambda f: (f.stem != stem, f.name))

    def _attach_pending_subs(self):
        """文件就绪后把还没被 mpv 自动挂上的外挂字幕补上。"""
        for cand in getattr(self, "_sub_candidates", []):
            try:
                self.video.add_sub(str(cand))
            except Exception as e:
                print(f"[sub] 挂载 {cand.name} 失败: {e}", flush=True)
        self._refresh_sub_id()
        if self._ext_sub is not None:
            print(f"[sub] 外挂字幕 {self._ext_sub.name} → track {self._ext_sub_id}",
                  flush=True)

    def _refresh_sub_id(self):
        """从 track-list 认领第一条外挂字幕（mpv 分配轨道号，内嵌在前、外挂在后）。"""
        exts = [t for t in self.video.sub_tracks() if t.get("external")]
        if not exts:
            self._ext_sub = None
            self._ext_sub_id = None
            return
        # 优先认 _sub_candidates 里排最前的那条
        names = [c.name.lower() for c in getattr(self, "_sub_candidates", [])]
        pick = None
        for n in names:
            pick = next((t for t in exts if Path(t.get("filename") or "").name.lower() == n), None)
            if pick:
                break
        pick = pick or exts[0]
        self._ext_sub = Path(pick["filename"]) if pick.get("filename") else None
        self._ext_sub_id = pick["id"]

    def _rebuild_sub_menu(self):
        menu = QMenu(self.dock.btnSubs)
        cur = str(self.video.current_sid())
        tracks = self.video.sub_tracks()

        act = menu.addAction("关闭字幕")
        act.setCheckable(True)
        act.setChecked(cur == "no")
        act.triggered.connect(lambda: self._set_sid("no", "字幕已关闭"))

        if tracks:
            menu.addSeparator()
        for t in tracks:
            if t.get("external"):
                label = f"外挂：{Path(t.get('filename') or t['title']).name}"
            else:
                label = f"内嵌 {t['id']}：{t['title']}"
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(cur == str(t["id"]))
            act.triggered.connect(
                lambda _=False, i=t["id"], n=label: self._set_sid(i, f"字幕 {n}"))

        self.dock.btnSubs.setMenu(menu)
        # 单击直接弹出（DelayedPopup 要长按，没人猜得到）
        self.dock.btnSubs.setPopupMode(QToolButton.InstantPopup)
        self.dock.btnSubs.setToolTip(
            f"字幕轨道 {len(tracks)} 条 (V)" if tracks else "这个文件没有字幕")
        # 有字幕时按钮显示当前状态 + 下拉箭头，没有就灰着
        self.dock.btnSubs.setEnabled(bool(tracks))
        if tracks:
            cur_t = next((t for t in tracks if str(t["id"]) == cur), None)
            self.dock.btnSubs.setText(("字幕" if cur_t else "字幕关") + " ▾")
        else:
            self.dock.btnSubs.setText("字幕")

    def _set_sid(self, sid, label: str = ""):
        if sid is None:
            return
        self.video.set_sid(sid)
        self._rebuild_sub_menu()
        if label:
            self.video.osd(label)

    def cycle_subs(self):
        opts = [("no", "关闭")]
        for t in self.video.sub_tracks():
            tag = "外挂" if t.get("external") else "内嵌"
            opts.append((str(t["id"]), f"{tag} {t['title']}"))
        if len(opts) <= 1:
            self.video.osd("没有字幕")
            return
        cur = str(self.video.current_sid())
        idx = next((i for i, (sid, _) in enumerate(opts) if sid == cur), 0)
        sid, label = opts[(idx + 1) % len(opts)]
        self._set_sid(sid, label)

    # ================================================== 播放控制
    def _seek(self, t: float):
        self.video.seek(max(0.0, min(self.duration, t)))
        self._tick()

    def jump_chapter(self, delta: int):
        if not self.chapters:
            return
        t = self._mpv_time()
        idx = self.rail.chapter_at(t)
        if delta < 0:
            at_start = t - self.chapters[idx]["start"] < 1.2
            idx = max(0, idx - 1) if at_start else idx
        else:
            idx = min(len(self.chapters) - 1, idx + 1)
        self._seek(self.chapters[idx]["start"] + 0.04)
        self.video.osd("下一段" if delta > 0 else "上一段")

    def cycle_speed(self):
        self.speed_idx = (self.speed_idx + 1) % len(SPEEDS)
        v = SPEEDS[self.speed_idx]
        self.video.set_speed(v)
        self.dock.set_speed(v)
        self.video.osd(f"{v:g}×")

    def _bump_speed(self, d: int):
        self.speed_idx = max(0, min(len(SPEEDS) - 1, self.speed_idx + d))
        v = SPEEDS[self.speed_idx]
        self.video.set_speed(v)
        self.dock.set_speed(v)
        self.video.osd(f"{v:g}×")

    def toggle_mute(self):
        self.muted = not self.muted
        self.video.mpv.mute = self.muted
        if not self.muted:
            self.set_volume(self._last_volume)
        self.video.osd("静音" if self.muted else f"音量 {self._last_volume}")

    def set_volume(self, v: int):
        if v > 0:
            self._last_volume = v
            self.muted = False
            self.video.mpv.mute = False
        self.video.set_volume(v)
        self.dock.set_volume(0 if self.muted else v)
        if self._volume_flash:
            self.video.osd(f"音量 {v}")

    _volume_flash = True

    def toggle_drawer(self, force=None):
        show = self.drawer.isHidden() if force is None else force
        self.drawer.setVisible(show)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.toggle_drawer(False)
            self.showFullScreen()

    # ================================================== 状态轮询
    def _mpv_time(self) -> float:
        try:
            return float(self.video.mpv.time_pos or 0)
        except Exception:
            return 0.0

    def _tick(self):
        t = self._mpv_time()
        dur = self.duration
        try:
            d = self.video.mpv.duration
            if d:
                dur = float(d)
                if abs(dur - self.duration) > 0.5:
                    self.duration = dur
        except Exception:
            pass
        paused = True
        try:
            paused = bool(self.video.mpv.pause)
        except Exception:
            pass
        self.dock.set_time(t, dur)
        self.dock.set_playing(not paused)
        self.rail.set_time(t)

    # ================================================== 章节
    def _on_file_loaded(self):
        """文件加载完成：内嵌字幕轨这时才可枚举。"""
        self._attach_pending_subs()
        self._rebuild_sub_menu()

    def _on_chapter_change(self, idx: int):
        self.drawer.set_active(idx)
        if 0 <= idx < len(self.chapters):
            c = self.chapters[idx]
            self.nowPlaying.setText(str(c.get("title", "")))
            self.nowPlaying.show()

    def _on_hover(self, chapter, t):
        if chapter is None:
            self.dock.show_peek(None, 0, None)
            return
        pm = None
        thumbs = LIB_DIR / cache_key(self.current_path or "") / "thumbs"
        tp = thumbs / str(chapter.get("thumb", "")).replace("thumbs/", "")
        if tp.is_file():
            pm = QPixmap(str(tp))
        self.dock.show_peek(chapter, t, pm)

    # ================================================== 分析任务
    def _llm_configured(self) -> bool:
        return bool(self.cfg.get("llmUrl") and self.cfg.get("llmModel"))

    def _llm_busy(self) -> bool:
        return self.llm is not None and self.llm.isRunning()

    def _kick(self, kind: str):
        """先确保模型服务就绪，再派活。kind: 'analyze' / 'retitle'"""
        self._job_kind = kind
        self._job_start = _time.time()
        self._job_first = 0.0
        self.dock.show_job(True)
        self.dock.set_job(0, "检查模型服务…" if self._llm_configured() else "准备中…")
        self.dock.btnAnalyze.setEnabled(False)
        self.dock.btnRetitle.setEnabled(False)
        if kind == "analyze":
            self.dock.btnAnalyze.setText("分析中")

        if not self._llm_configured():
            self._dispatch(False)
            return

        self.llm = LlmStarter(self.cfg, self)
        self.llm.progress.connect(lambda msg: self.dock.set_job(2, msg))
        self.llm.finished_ok.connect(self._on_llm_ready)
        self.llm.start()

    def _on_llm_ready(self, ok: bool, why: str):
        # 这里不清 self.llm：信号是在 QThread.run() 里发出的，此刻线程还没真正结束，
        # 立刻丢掉引用有可能在运行中被 GC 掉（QThread: Destroyed while running）。
        if ok:
            self._dispatch(True)
            return
        if self._job_kind == "retitle":
            # 「重做标题」的全部意义就是让大模型起名，服务起不来跑也没意义
            self.dock.show_job(False)
            self.dock.btnAnalyze.setEnabled(True)
            self.dock.btnAnalyze.setText("分析章节")
            self.dock.btnRetitle.setEnabled(bool(self.chapters))
            QMessageBox.warning(
                self, "Stanza",
                f"{why}\n\n可在「文件 → 重做章节标题」里填启动命令，或手动启动模型服务。")
            return
        # 分析：模型起不来也照跑，标题退化成台词截断
        self.dock.set_job(2, "模型未就绪，用台词截断起标题")
        self._dispatch(False)

    def _on_count_changed(self, v):
        """章节数只影响下一次分析；当前结果不重跑（重跑要几分钟，太打断）。"""
        self.cfg = save_config({"chapterCount": v})
        self.dock.set_count(v)

    def _dispatch(self, use_llm: bool):
        if self._job_kind == "retitle":
            self.runner.retitle(out_dir_for(self.current_path), self.cfg, use_llm=use_llm)
        else:
            # count 不在这里传：analysis.py 统一从 cfg 的 chapterCount 读，单一真相
            self.runner.analyze_video(self.current_path, out_dir_for(self.current_path),
                                      self.cfg, use_llm=use_llm)

    def start_analyze(self):
        if not self.current_path or self.runner.running() or self._llm_busy():
            return
        self._kick("analyze")

    def start_retitle(self):
        if not self.current_path or self.runner.running() or self._llm_busy():
            return
        if not self._llm_configured():
            dlg = LlmDialog(self, self.cfg)
            if dlg.exec() != QDialog.Accepted:
                return
            self.cfg = save_config({
                "llmUrl": dlg.url.text().strip(),
                "llmModel": dlg.model.text().strip(),
                "llmLaunch": dlg.launch.text().strip(),
            })
        self._kick("retitle")

    def _on_progress(self, payload: dict):
        pct = payload.get("pct", 0)
        label = payload.get("label", "")
        if pct > 0 and not self._job_first:
            self._job_first = _time.time()
        base = self._job_first or self._job_start
        elapsed = _time.time() - base
        if 2 < pct < 99.5 and base:
            remain = elapsed * (100 - pct) / pct
            eta = f"已用 {fmt(elapsed)} · 剩余约 {fmt(remain)}"
        else:
            eta = f"已用 {fmt(elapsed)}"
        self.dock.set_job(pct, label, eta)
        self.dock.btnAnalyze.setText(f"分析中 {round(pct)}%")

    def _on_log(self, line: str):
        self.dock.jobLabel.setToolTip(line[-400:])

    def _on_analyze_done(self, ok: bool, err: str):
        self.dock.btnAnalyze.setEnabled(True)
        self.dock.btnAnalyze.setText("分析章节")
        self.dock.btnRetitle.setEnabled(True)
        if ok:
            self.dock.set_job(100, "完成")
            QTimer.singleShot(900, lambda: self.dock.show_job(False))
            key, cj = chapters_for(self.current_path)
            if cj:
                self._apply_chapters(cj)
            self._update_chapter_buttons()
        else:
            self.dock.show_job(False)
            QMessageBox.warning(self, "Stanza", err or "分析失败")

    # ================================================== 空闲隐藏 / 事件
    def eventFilter(self, obj, ev: QEvent):
        if ev.type() in (QEvent.MouseMove, QEvent.MouseButtonPress, QEvent.KeyPress,
                         QEvent.Wheel):
            self._wake()
        return super().eventFilter(obj, ev)

    def _wake(self):
        hidden = getattr(self, "_hidden_ui", None)
        if hidden:
            for w in hidden:
                if w:
                    w.setVisible(True)
            self._hidden_ui = None
            self.centralWidget().setCursor(Qt.ArrowCursor)
        self.idle.start()

    def _go_idle(self):
        try:
            if self.video.mpv.pause:
                return
        except Exception:
            pass
        if getattr(self, "_hidden_ui", None) is not None:
            return
        central = self.centralWidget()
        self._hidden_ui = []
        for w in (self.menuBar(), self.dock.parentWidget()):
            if w and w.isVisible():
                w.setVisible(False)
                self._hidden_ui.append(w)
        central.setCursor(Qt.BlankCursor)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if Path(p).suffix.lower() in VIDEO_EXT:
                self.load_video(p)
                break

    def keyPressEvent(self, ev):
        k, mods = ev.key(), ev.modifiers()
        step = 1 if (mods & Qt.ShiftModifier) else 5
        t = self._mpv_time()
        if k == Qt.Key_Space:
            self.video.toggle_play()
        elif k == Qt.Key_Left:
            self._seek(t - step); self.video.osd(f"−{step}s")
        elif k == Qt.Key_Right:
            self._seek(t + step); self.video.osd(f"+{step}s")
        elif k == Qt.Key_Up:
            self.set_volume(min(100, self._last_volume + 5))
        elif k == Qt.Key_Down:
            self.set_volume(max(0, self._last_volume - 5))
        elif k in (Qt.Key_M,):
            self.toggle_mute()
        elif k in (Qt.Key_F,):
            self.toggle_fullscreen()
        elif k in (Qt.Key_C,):
            self.toggle_drawer()
        elif k in (Qt.Key_N,):
            self.jump_chapter(1)
        elif k in (Qt.Key_P,):
            self.jump_chapter(-1)
        elif k in (Qt.Key_J,):
            self._seek(t - 10); self.video.osd("−10s")
        elif k in (Qt.Key_L,):
            self._seek(t + 10); self.video.osd("+10s")
        elif k in (Qt.Key_K,):
            self.video.toggle_play()
        elif k == Qt.Key_BracketLeft:
            self._bump_speed(-1)
        elif k == Qt.Key_BracketRight:
            self._bump_speed(1)
        elif Qt.Key_0 <= k <= Qt.Key_9 and self.duration:
            frac = (k - Qt.Key_0) / 10
            self._seek(self.duration * frac)
            self.video.osd(f"{int(frac * 100)}%")
        elif k == Qt.Key_Escape:
            self.toggle_drawer(False)
        elif k in (Qt.Key_V,):
            self.cycle_subs()
        else:
            super().keyPressEvent(ev)

    def _open_cache(self):
        import subprocess
        subprocess.Popen(["explorer", str(LIB_DIR)])

    def closeEvent(self, ev):
        if self.llm is not None and self.llm.isRunning():
            self.llm.requestInterruption()
            self.llm.wait(3000)
        self.runner.stop()
        self.video.shutdown()
        super().closeEvent(ev)


def _install_crash_log():
    """打包成 --windowed 后没有控制台，崩溃信息只能落盘。"""
    def hook(exc_type, exc, tb):
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            p = Path(os.environ.get("APPDATA", str(Path.home()))) / "Stanza" / "stanza.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt, "utf-8")
        except Exception:
            pass
        try:
            print(txt, file=sys.__stderr__ or sys.stderr, flush=True)
        except Exception:
            pass

    sys.excepthook = hook


def main():
    _install_crash_log()
    app = QApplication(sys.argv)
    app.setApplicationName("Stanza")
    app.setApplicationDisplayName("Stanza")
    _ico = resource("stanza.ico")
    if _ico:
        app.setWindowIcon(QIcon(str(_ico)))
    app.setStyleSheet("""
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei UI'; }
        QMenuBar { background: #fff; border-bottom: 1px solid rgba(0,0,0,0.06); }
        QMenuBar::item { padding: 6px 10px; color: #16171b; }
        QMenuBar::item:selected { background: rgba(0,0,0,0.06); border-radius: 6px; }
        QMenu { background: #fff; border: 1px solid rgba(0,0,0,0.1); border-radius: 10px; }
        QMenu::item { padding: 7px 24px; color: #16171b; }
        QMenu::item:selected { background: rgba(0,0,0,0.06); }
        QToolTip { background: #fff; color: #16171b; border: 1px solid rgba(0,0,0,0.15); }
    """)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

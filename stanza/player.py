"""播放器 UI：mpv 视频区 + 章节时间轴 + 底部控制条 + 章节抽屉。

视频用 libmpv wid 嵌入（原生子窗口），所以所有 Qt 控件都不与视频区重叠——
瞬时提示（倍速/音量/跳段）走 mpv 自己的 OSD。
"""
import locale
from pathlib import Path

locale.setlocale(locale.LC_NUMERIC, "C")  # Qt 会改 locale，必须在 mpv 初始化前设回

from PySide6.QtCore import Qt, QTimer, Signal, QRectF, QPointF, QPoint, QEvent
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap,
                           QPen, QBrush, QFontMetrics, QGuiApplication)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QWidget, QLabel, QToolButton, QSlider, QHBoxLayout,
                               QVBoxLayout, QFrame, QSizePolicy, QScrollArea,
                               QStackedWidget)

import mpv

SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]

# ---------------------------------------------------------------- 纯白配色
BG = "#ffffff"
FG = "#16171b"
FG_DIM = "rgba(22,23,27,0.55)"
RAIL_REST = "#e9eaee"
RAIL_FILL = "#1a1b1f"
RAIL_ACTIVE = "#000000"
STROKE = "rgba(0,0,0,0.09)"
PANEL = "#ffffff"

# 现有 HTML 里的图标 path，直接复用
_SVG = {
    "prev":   '<path d="M18 5.2a.6.6 0 0 1 .92.51v12.58a.6.6 0 0 1-.92.51L9 12.51V18a1 1 0 0 1-2 0V6a1 1 0 0 1 2 0v5.49l9-6.29z"/>',
    "play":   '<path d="M7.5 4.6a.6.6 0 0 1 .92-.5l11 7.4a.6.6 0 0 1 0 .99l-11 7.4a.6.6 0 0 1-.92-.5V4.6z"/>',
    "next":   '<path d="M6 5.2a.6.6 0 0 0-.92.51v12.58a.6.6 0 0 0 .92.51L15 12.51V18a1 1 0 0 0 2 0V6a1 1 0 0 0-2 0v5.49L6 5.2z"/>',
    "pause":  '<rect x="6.5" y="4.5" width="3.6" height="15" rx="1.2"/><rect x="13.9" y="4.5" width="3.6" height="15" rx="1.2"/>',
    "vol":    '<path d="M11 5 6.5 9H3v6h3.5L11 19V5z"/><path d="M15.2 9.2a4 4 0 0 1 0 5.6" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round"/><path d="M17.8 6.6a7.6 7.6 0 0 1 0 10.8" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round"/>',
    "list":   '<path d="M4 7h16M4 12h11M4 17h14" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>',
    "full":   '<path d="M4 9.5V4h5.5M20 14.5V20h-5.5M20 9.5V20h-5.5M4 14.5V20h5.5" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>',
    "vol0":   '<path d="M11 5 6.5 9H3v6h3.5L11 19V5z"/><path d="M16 9.5l4.5 5M20.5 9.5l-4.5 5" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round"/>',
}


def svg_icon(kind: str, color: str, size: int = 22) -> QIcon:
    body = _SVG[kind].replace("{c}", color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="{color}">{body}</svg>')
    r = QSvgRenderer(svg.encode())
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    r.render(p)
    p.end()
    return QIcon(pm)


def fmt(t: float) -> str:
    if t is None or t < 0 or t != t:
        t = 0
    s = int(t) % 60
    m = int(t // 60) % 60
    h = int(t // 3600)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ================================================================ mpv 视频区

class MpvVideo(QWidget):
    """libmpv 嵌入区。原生子窗口，鼠标默认绑定保留（单击暂停）。

    视频区保持直角：mpv 是独立的原生子窗口，Qt 画不到它上面，QSS 的
    border-radius 对它无效。曾经试过给 HWND 设 Win32 圆角 region（能生效），
    但用户明确说不要圆角，已撤掉。
    """

    clicked = Signal()
    fileLoaded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setStyleSheet("background:#000;")
        self.mpv = mpv.MPV(
            wid=str(int(self.winId())),
            vo="gpu",
            hwdec="auto-safe",
            keep_open="no",
            osc=False,
            osd_level=1,
            osd_duration=900,
            # 键盘归 Qt（见主窗口 keyPressEvent），鼠标默认绑定给 mpv（单击暂停）
            input_default_bindings=True,
            input_vo_keyboard=False,
            log_handler=self._log,
        )
        self.mpv["vo"] = "gpu"
        self.mpv.event_callback("file-loaded")(lambda _ev: self.fileLoaded.emit())

    @staticmethod
    def _log(level, prefix, text):
        if level in ("error", "fatal"):
            print(f"[mpv {level}] {prefix}{text}", flush=True)

    # mpv 命令薄封装 -----------------------------------------------
    def load(self, path: str):
        # 用 mpv 原生 loadfile 命令而不是 python-mpv 的 play()：
        # 后者在无音轨文件上会静默失败（文件加载后立刻回到 idle）
        self.mpv.command("loadfile", path, "replace")

    def toggle_play(self):
        self.mpv.command("cycle", "pause")

    def seek(self, t: float, exact=False):
        self.mpv.command("seek", max(0.0, t), "absolute+exact" if exact else "absolute")

    def set_speed(self, v: float):
        self.mpv.speed = v

    def set_volume(self, v: int):
        self.mpv.volume = max(0, min(100, v))

    # 字幕 -------------------------------------------------
    # python-mpv 三种访问语义别搞混：
    #   mpv["x"]      → 读写 **option**（内部自动加 "options/" 前缀）
    #   mpv.x         → 读 property，但会和类方法名撞车
    #   _get_property → 直接读 property（最可靠，track-list / sub-text 只能这么读）
    # 踩过：mpv["track-list"] 报 "property does not exist"，因为 track-list 不是 option。
    def prop(self, name, default=None):
        try:
            v = self.mpv._get_property(name)
        except Exception:
            return default
        return default if v is None else v

    def set_prop(self, name, value):
        try:
            self.mpv._set_property(name, value)
            return True
        except Exception as e:
            print(f"[mpv] set {name}={value!r} 失败: {e}", flush=True)
            return False

    def sub_tracks(self) -> list:
        """全部字幕轨（内嵌 + 外挂）：
        [{'id','title','lang','selected','external','filename'}, ...]"""
        out = []
        for t in self.prop("track-list", []) or []:
            if t.get("type") != "sub":
                continue
            fn = str(t.get("external-filename") or "")
            ext = bool(t.get("external"))
            name = t.get("title") or t.get("lang") or ""
            if not name and fn:
                name = Path(fn).name
            out.append({
                "id": t.get("id"),
                "title": name or f"字幕 {t.get('id')}",
                "lang": t.get("lang", ""),
                "selected": bool(t.get("selected")),
                "external": ext,
                "filename": fn,
            })
        return out

    def current_sid(self):
        """当前选中字幕轨的 id；关闭时返回 'no'。
        不用 sid 属性——它把 "no" 返回成 False，"auto" 返回成 True，没法区分。"""
        t = self.prop("current-tracks/sub", None)
        return t["id"] if isinstance(t, dict) else "no"

    def set_sid(self, sid):
        """sid: 'no' 关闭 / 'auto' 自动 / 整数轨道号"""
        if isinstance(sid, str) and sid.isdigit():
            sid = int(sid)
        return self.set_prop("sid", sid)

    def add_sub(self, path: str, select=True):
        """挂外挂字幕。返回新轨道 id（拿不到返回 None）。已存在同文件则不重复加。"""
        p = Path(path)
        for t in self.sub_tracks():
            if t.get("filename") and Path(t["filename"]).name.lower() == p.name.lower():
                return t["id"]
        before = {t["id"] for t in self.sub_tracks()}
        self.mpv.command("sub-add", str(p), "select" if select else "auto")
        for t in self.sub_tracks():
            if t["id"] not in before:
                return t["id"]
        return None

    def sub_visible(self) -> bool:
        return bool(self.prop("sub-visibility", True))

    def set_sub_visible(self, on: bool):
        return self.set_prop("sub-visibility", bool(on))

    def sub_text(self) -> str:
        """当前正在显示的字幕文本（调试/自检用）。"""
        return self.prop("sub-text", "") or ""

    def osd(self, text: str, ms=900):
        try:
            self.mpv.command("show-text", text, ms)
        except Exception:
            pass

    def shutdown(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass


# ================================================================ 章节时间轴

class ChapterRail(QWidget):
    """分段进度条：悬停变粗、点击跳转、镜头 tick、当前段高亮。"""

    seekRequested = Signal(float)
    hoverChanged = Signal(object, float)   # (章节 dict 或 None, hover 时刻)
    activeChanged = Signal(int)

    PAD = 0  # 左右不留白，段间自然有 gap

    def __init__(self, parent=None):
        super().__init__(parent)
        self.chapters: list[dict] = []
        self.shots: list[float] = []
        self.duration = 0.0
        self.time_pos = 0.0
        self.active = -1
        self._hover_x = None
        self._drag = False
        self.setMouseTracking(True)
        self.setMinimumHeight(26)
        self.setMaximumHeight(26)
        self.setCursor(Qt.PointingHandCursor)

    # ------------------------------------------------------------ 数据
    def set_chapters(self, chapters: list, shots: list, duration: float):
        self.chapters = chapters or []
        self.shots = shots or []
        self.duration = duration or (self.chapters[-1]["end"] if self.chapters else 0.0)
        self.active = -1
        self.update()

    def set_time(self, t: float):
        self.time_pos = t
        idx = self.chapter_at(t)
        if idx != self.active:
            self.active = idx
            self.activeChanged.emit(idx)
        self.update()

    def chapter_at(self, t: float) -> int:
        for i in range(len(self.chapters) - 1, -1, -1):
            if t >= self.chapters[i]["start"] - 0.001:
                return i
        return 0

    # ------------------------------------------------------------ 绘制
    def _rail_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        rh = 15 if (self._hover_x is not None or self._drag) else 9
        return QRectF(self.PAD, (h - rh) / 2, w - 2 * self.PAD, rh)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.chapters:
            r = self._rail_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(RAIL_REST))
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            if self.duration:
                p.setBrush(QColor(RAIL_FILL))
                frac = min(1.0, self.time_pos / self.duration)
                p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * frac, r.height()),
                                  r.height() / 2, r.height() / 2)
            p.end()
            return

        total = sum(c["end"] - c["start"] for c in self.chapters) or 1.0
        r = self._rail_rect()
        gap = 2.0
        x = r.left()
        avail = r.width() - gap * (len(self.chapters) - 1)
        for i, c in enumerate(self.chapters):
            w = avail * (c["end"] - c["start"]) / total
            seg = QRectF(x, r.top(), max(3.0, w), r.height())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(RAIL_REST))
            p.drawRoundedRect(seg, seg.height() / 2, seg.height() / 2)
            frac = (self.time_pos - c["start"]) / max(0.001, c["end"] - c["start"])
            frac = max(0.0, min(1.0, frac))
            if frac > 0:
                fill = QRectF(seg.left(), seg.top(), seg.width() * frac, seg.height())
                p.setBrush(QColor(RAIL_ACTIVE if i == self.active else RAIL_FILL))
                p.drawRoundedRect(fill, fill.height() / 2, fill.height() / 2)
            x += w + gap

        # 镜头切换 tick
        if r.height() >= 13:
            p.setBrush(QColor("#ffffff"))
            p.setPen(Qt.NoPen)
            for s in self.shots:
                t = s.get("t", 0) if isinstance(s, dict) else float(s)
                tx = r.left() + r.width() * (t / total)
                p.drawRect(QRectF(tx - 0.75, r.top() + 2, 1.5, r.height() - 4))
        p.end()

    # ------------------------------------------------------------ 交互
    def _time_at(self, x: float) -> float:
        if not self.chapters or not self.duration:
            return 0.0
        total = sum(c["end"] - c["start"] for c in self.chapters) or 1.0
        avail = self.width() - 2 * self.PAD - 2.0 * (len(self.chapters) - 1)
        if avail <= 0:
            return 0.0
        # 把像素映射回时长（考虑 gap）
        seg_x = self.PAD
        for c in self.chapters:
            w = avail * (c["end"] - c["start"]) / total
            if x <= seg_x + w or c is self.chapters[-1]:
                frac = max(0.0, min(1.0, (x - seg_x) / max(1.0, w)))
                return c["start"] + frac * (c["end"] - c["start"])
            seg_x += w + 2.0
        return self.duration

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self.seekRequested.emit(self._time_at(e.position().x()))
            self.update()

    def mouseMoveEvent(self, e):
        self._hover_x = e.position().x()
        t = self._time_at(self._hover_x)
        idx = self.chapter_at(t) if self.chapters else -1
        self.hoverChanged.emit(self.chapters[idx] if 0 <= idx < len(self.chapters) else None, t)
        if self._drag:
            self.seekRequested.emit(t)
        self.update()

    def leaveEvent(self, _):
        self._hover_x = None
        self.hoverChanged.emit(None, 0.0)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = False
            self.update()


# ================================================================ 图标按钮

class IconButton(QToolButton):
    def __init__(self, kind: str, tip: str, size=34, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._size = size
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self._apply(False)
        self.setStyleSheet(
            "QToolButton{border:0;border-radius:10px;background:transparent;}"
            "QToolButton:hover{background:rgba(0,0,0,0.06);}"
            "QToolButton:pressed{background:rgba(0,0,0,0.1);}")

    def _apply(self, hover: bool):
        c = "#0a0a0c" if not hover else "#000000"
        self.setIcon(svg_icon(self.kind, c, 22))
        self.setIconSize(self.size() * 0 + self.iconSize())

    def enterEvent(self, e):
        self._apply(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._apply(False)
        super().leaveEvent(e)


# ================================================================ 音量条

class VolumeBar(QWidget):
    """细长音量条：填充比例表示音量，点击/拖动调节。"""
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 100
        self._drag = False
        self.setFixedSize(56, 26)
        self.setCursor(Qt.PointingHandCursor)

    def set_value(self, v: int):
        self.value = max(0, min(100, int(v)))
        self.update()

    def _v_at(self, x):
        return int(max(0.0, min(1.0, x / self.width())) * 100)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y = self.height() / 2 - 2
        track = QRectF(0, y, self.width(), 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(RAIL_REST))
        p.drawRoundedRect(track, 2, 2)
        p.setBrush(QColor(RAIL_FILL))
        p.drawRoundedRect(QRectF(0, y, self.width() * self.value / 100.0, 4), 2, 2)
        p.end()

    def mousePressEvent(self, e):
        self._drag = True
        self.changed.emit(self._v_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if self._drag:
            self.changed.emit(self._v_at(e.position().x()))

    def mouseReleaseEvent(self, _):
        self._drag = False


# ================================================================ 底部控制条

class Dock(QFrame):
    """悬停预览 + 章节时间轴 + 播放控制。分析任务时切成进度面板。"""

    playClicked = Signal()
    prevClicked = Signal()
    nextClicked = Signal()
    speedClicked = Signal()
    muteClicked = Signal()
    drawerClicked = Signal()
    fullClicked = Signal()
    analyzeClicked = Signal()
    retitleClicked = Signal()
    volumeChanged = Signal(int)
    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dock")
        self.setStyleSheet(
            f"#dock{{background:{PANEL};border:1px solid {STROKE};border-radius:20px;}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 8)
        root.setSpacing(4)

        # 悬停预览（缩略图 + 标题 + 时间），默认隐藏
        self.peek = QFrame(self)
        self.peek.setObjectName("peek")
        self.peek.setStyleSheet(
            "#peek{background:#fff;border:1px solid rgba(0,0,0,0.1);border-radius:12px;}")
        pv = QHBoxLayout(self.peek)
        pv.setContentsMargins(8, 8, 10, 8)
        pv.setSpacing(10)
        self.peekImg = QLabel()
        self.peekImg.setFixedSize(86, 48)
        self.peekImg.setAlignment(Qt.AlignCenter)
        self.peekImg.setStyleSheet("background:#f0f1f3;border-radius:6px;")
        self.peekImg.hide()
        pv.addWidget(self.peekImg)
        pt = QVBoxLayout()
        pt.setSpacing(2)
        self.peekTitle = QLabel()
        self.peekTitle.setStyleSheet(f"color:{FG};font-size:12px;font-weight:600;")
        self.peekTime = QLabel()
        self.peekTime.setStyleSheet(f"color:{FG_DIM};font-size:11px;")
        pt.addWidget(self.peekTitle)
        pt.addWidget(self.peekTime)
        pv.addLayout(pt)
        self.peek.hide()
        self.peek.adjustSize()

        # 时间轴 / 任务进度 两块切换
        self.stack = QStackedWidget()
        self.stack.setFixedHeight(30)
        self.rail = ChapterRail()
        self.stack.addWidget(self.rail)

        self.job = QWidget()
        jv = QVBoxLayout(self.job)
        jv.setContentsMargins(2, 2, 2, 2)
        jv.setSpacing(2)
        self.jobBar = QSlider(Qt.Horizontal)
        self.jobBar.setRange(0, 100)
        self.jobBar.setStyleSheet(
            "QSlider::groove:horizontal{height:4px;border-radius:2px;background:%s;}"
            "QSlider::sub-page:horizontal{height:4px;border-radius:2px;background:%s;}"
            "QSlider::handle:horizontal{width:0;}" % (RAIL_REST, RAIL_FILL))
        self.jobBar.setFixedHeight(12)
        self.jobLabel = QLabel("准备中…")
        self.jobLabel.setStyleSheet(f"color:{FG_DIM};font-size:11px;")
        jv.addWidget(self.jobBar)
        jv.addWidget(self.jobLabel)
        self.stack.addWidget(self.job)
        root.addWidget(self.peek, 0, Qt.AlignHCenter)
        root.addWidget(self.stack)

        # 控件行
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btnPrev = IconButton("prev", "上一段 (P)")
        self.btnPlay = IconButton("play", "播放 / 暂停 (空格)", 40)
        self.btnPlay.setIconSize(self.btnPlay.iconSize())
        self.btnNext = IconButton("next", "下一段 (N)")
        row.addWidget(self.btnPrev)
        row.addWidget(self.btnPlay)
        row.addWidget(self.btnNext)
        row.addSpacing(8)

        self.timeLbl = QLabel("00:00 / 00:00")
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        self.timeLbl.setFont(f)
        self.timeLbl.setStyleSheet(f"color:{FG};font-size:12px;")
        row.addWidget(self.timeLbl)
        row.addStretch(1)

        self.btnAnalyze = QToolButton()
        self.btnAnalyze.setText("分析章节")
        self.btnAnalyze.setCursor(Qt.PointingHandCursor)
        self.btnAnalyze.setStyleSheet(
            "QToolButton{border:1px solid rgba(0,0,0,0.28);border-radius:10px;"
            "padding:5px 14px;font-size:12px;font-weight:600;color:%s;}"
            "QToolButton:hover{background:rgba(0,0,0,0.05);}" % FG)
        self.btnRetitle = QToolButton()
        self.btnRetitle.setText("重做标题")
        self.btnRetitle.setCursor(Qt.PointingHandCursor)
        self.btnRetitle.setStyleSheet(self.btnAnalyze.styleSheet())
        self.btnRetitle.hide()
        row.addWidget(self.btnAnalyze)
        row.addWidget(self.btnRetitle)

        self.btnSpeed = QToolButton()
        self.btnSpeed.setText("1.0×")
        self.btnSpeed.setCursor(Qt.PointingHandCursor)
        self.btnSpeed.setToolTip("倍速 ( [ / ] )")
        self.btnSpeed.setStyleSheet(
            "QToolButton{border:1px solid %s;border-radius:10px;padding:5px 10px;"
            "font-size:12px;font-weight:600;color:%s;background:transparent;}"
            "QToolButton:hover{background:rgba(0,0,0,0.05);}" % (STROKE, FG))
        row.addWidget(self.btnSpeed)

        self.btnSubs = QToolButton()
        self.btnSubs.setText("字幕")
        self.btnSubs.setCursor(Qt.PointingHandCursor)
        self.btnSubs.setToolTip("字幕轨道 (V)")
        self.btnSubs.setStyleSheet(
            "QToolButton{border:1px solid %s;border-radius:10px;padding:5px 10px;"
            "font-size:12px;font-weight:600;color:%s;background:transparent;}"
            "QToolButton:hover{background:rgba(0,0,0,0.05);}"
            "QToolButton:disabled{color:rgba(22,23,27,0.32);border-color:rgba(0,0,0,0.08);}"
            "QToolButton::menu-indicator{image:none;width:0;}" % (STROKE, FG))
        row.addWidget(self.btnSubs)
        row.addSpacing(4)

        self.btnMute = IconButton("vol", "静音 (M)", 30)
        self.vol = VolumeBar()
        row.addWidget(self.btnMute)
        row.addWidget(self.vol)
        row.addSpacing(4)

        self.btnDrawer = IconButton("list", "章节列表 (C)", 30)
        self.btnFull = IconButton("full", "全屏 (F)", 30)
        row.addWidget(self.btnDrawer)
        row.addWidget(self.btnFull)

        root.addLayout(row)

        # 信号
        self.rail.seekRequested.connect(self.seekRequested)
        self.btnPlay.clicked.connect(self.playClicked)
        self.btnPrev.clicked.connect(self.prevClicked)
        self.btnNext.clicked.connect(self.nextClicked)
        self.btnSpeed.clicked.connect(self.speedClicked)
        self.btnMute.clicked.connect(self.muteClicked)
        self.btnDrawer.clicked.connect(self.drawerClicked)
        self.btnFull.clicked.connect(self.fullClicked)
        self.btnAnalyze.clicked.connect(self.analyzeClicked)
        self.btnRetitle.clicked.connect(self.retitleClicked)
        self.vol.changed.connect(self.volumeChanged)

        self._vol_last = 100

    # ------------------------------------------------------------ 状态
    def set_playing(self, playing: bool):
        self.btnPlay.kind = "pause" if playing else "play"
        self.btnPlay.setToolTip("暂停 (空格)" if playing else "播放 (空格)")
        self.btnPlay.setIcon(svg_icon(self.btnPlay.kind, "#0a0a0c", 22))

    def set_time(self, t, dur):
        self.timeLbl.setText(f"{fmt(t)} / {fmt(dur)}")

    def set_speed(self, v):
        self.btnSpeed.setText(f"{v:g}×")

    def set_volume(self, v):
        self.vol.set_value(v)
        self.btnMute.setIcon(svg_icon("vol0" if v == 0 else "vol", "#0a0a0c", 20))

    def show_job(self, visible: bool):
        self.stack.setCurrentWidget(self.job if visible else self.rail)

    def set_job(self, pct, label, eta=""):
        self.jobBar.setValue(int(pct))
        self.jobLabel.setText(f"{label}   {eta}".strip())

    def show_peek(self, chapter, t, thumb_pix: QPixmap | None):
        if chapter is None:
            self.peek.hide()
            return
        self.peekTitle.setText(str(chapter.get("title", "")))
        self.peekTime.setText(f"{fmt(t)} / {fmt(chapter.get('end', 0))}")
        if thumb_pix and not thumb_pix.isNull():
            self.peekImg.setPixmap(thumb_pix.scaled(
                self.peekImg.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.peekImg.show()
        else:
            self.peekImg.hide()
        self.peek.adjustSize()
        # 位置：跟随鼠标，clamp 在 dock 内
        hx = int(self.rail._hover_x or 0)
        mx = self.rail.mapTo(self, QPoint(hx, 0)).x()
        x = max(8, min(self.width() - self.peek.width() - 8, int(mx - self.peek.width() / 2)))
        self.peek.move(x, 2)
        self.peek.show()

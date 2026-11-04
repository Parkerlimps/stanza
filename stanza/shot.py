"""UI 自检：起窗口 → 播视频 → 截整屏 → 退出。

用法：python stanza/shot.py [输出名] [视频路径] [drawer] [seek秒]

注意：mpv 是原生子窗口，grabWindow(winId) 抓不到它（视频区会是黑的），
必须抓全屏 grabWindow(0) 再按窗口 frameGeometry 裁剪。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["PATH"] = str(ROOT / "vendor") + os.pathsep + os.environ.get("PATH", "")
try:
    os.add_dll_directory(str(ROOT / "vendor"))
except Exception:
    pass

import locale
locale.setlocale(locale.LC_NUMERIC, "C")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import main as app_main

SHOTS = ROOT / "screens"


def run():
    # 参数里有视频路径，别让 MainWindow 再从 argv 自动加载一遍
    args = sys.argv[1:]
    sys.argv = sys.argv[:1]

    app = QApplication(sys.argv)
    w = app_main.MainWindow()
    w.setWindowFlag(Qt.WindowStaysOnTopHint, True)   # 免得被别的窗口挡住截胡
    w.resize(1280, 820)
    w.move(80, 60)          # 固定位置，保证截图可复现且不被屏幕边缘裁掉
    w.show()
    w.raise_()
    w.activateWindow()

    name = args[0] if len(args) > 0 else "py_player"
    video = args[1] if len(args) > 1 else str(ROOT / "samples" / "sintel.mp4")
    drawer = len(args) > 2 and args[2] == "drawer"
    seek_to = float(args[3]) if len(args) > 3 else 21.5
    SHOTS.mkdir(exist_ok=True)
    out = SHOTS / f"{name}.png"

    def keep_ui():
        # 避免 idle 把控件藏掉
        w._wake()

    def t1():
        w.load_video(video)

    def t2():
        try:
            w.video.seek(seek_to)
        except Exception:
            pass
        if drawer:
            w.toggle_drawer(True)

    def shot():
        keep_ui()
        w.raise_()
        w.activateWindow()
        app.processEvents()
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() or 1.0
        pm = screen.grabWindow(0)          # 全屏抓，才能带出 mpv 原生窗口
        geo = w.frameGeometry()
        pm = pm.copy(int(geo.x() * dpr), int(geo.y() * dpr),
                     int(geo.width() * dpr), int(geo.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.save(str(out))
        try:
            print(f"[shot] 已保存 {out} ({pm.width()}x{pm.height()}) "
                  f"sub-text={w.video.sub_text()!r} sid={w.video.current_sid()!r}",
                  flush=True)
        except Exception:
            print(f"[shot] 已保存 {out}", flush=True)
        app.exit(0)

    keeper = QTimer()
    keeper.timeout.connect(keep_ui)
    keeper.start(800)
    QTimer.singleShot(1200, t1)
    QTimer.singleShot(2800, t2)
    QTimer.singleShot(5200, shot)
    sys.exit(app.exec())


if __name__ == "__main__":
    run()

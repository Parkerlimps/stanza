"""冒烟测试：起窗口 → 加载样片 → 检查解码推进 → 自动退出。

用法：python stanza/smoke_test.py [视频路径]
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
os.environ["PATH"] = str(VENDOR) + os.pathsep + os.environ.get("PATH", "")
try:
    os.add_dll_directory(str(VENDOR))
except Exception:
    pass

import locale
locale.setlocale(locale.LC_NUMERIC, "C")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

import main as app_main


def run():
    # 参数里有视频路径，别让 MainWindow 再从 argv 自动加载一遍
    args = sys.argv[1:]
    sys.argv = sys.argv[:1]

    app = QApplication(sys.argv)
    w = app_main.MainWindow()
    w.show()

    video = args[0] if args else str(ROOT / "samples" / "sintel.mp4")
    log = []

    def step1():
        print(f"[smoke] 加载 {video}", flush=True)
        w.load_video(video)

    def step2():
        # seek 到片子中段（片子可能比 21.5s 短）
        try:
            dur = float(w.video.mpv.duration or 0)
        except Exception:
            dur = 0.0
        w.video.seek(min(21.5, max(0.5, dur * 0.5)))

    def report():
        m = w.video.mpv
        try:
            st = {
                "duration": round(float(m.duration or 0), 2),
                "time_pos": round(float(m.time_pos or 0), 2),
                "pause": bool(m.pause),
                "width": m.width,
                "height": m.height,
                "video_format": m.video_format,
                "hwdec_current": m.hwdec_current,
                "filename": bool(m.path),
            }
        except Exception as e:
            st = {"error": str(e)}
        # 字幕自检：轨道枚举 + 当前选中 + 当前显示的文本
        subs = w.video.sub_tracks()
        st["subs"] = [(t["id"], t["title"], "ext" if t["external"] else "emb") for t in subs]
        st["sid"] = w.video.current_sid()
        st["sub_text"] = w.video.sub_text()
        print("[smoke] mpv 状态 " + str(st), flush=True)
        log.append(st)
        ok = st.get("time_pos", 0) > 0 and st.get("duration", 0) > 0
        print(f"[smoke] {'PASS' if ok else 'FAIL'}", flush=True)
        app.exit(0 if ok else 1)

    QTimer.singleShot(1500, step1)
    QTimer.singleShot(4000, step2)
    QTimer.singleShot(6000, report)
    code = app.exec()
    sys.exit(code)


if __name__ == "__main__":
    run()

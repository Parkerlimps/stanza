"""性能对照：同一视频，测本进程（PySide6+libmpv）的内存与 CPU。

用法：python stanza/bench.py [视频路径]
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes as wt
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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import main as app_main


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                 ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                 ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                 ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                 ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class FT(ctypes.Structure):
    _fields_ = [("LowDateTime", wt.DWORD), ("HighDateTime", wt.DWORD)]


def mem_mb(pid):
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
    ctypes.windll.kernel32.CloseHandle(h)
    return c.WorkingSetSize / 1048576.0


def cpu_s(pid):
    ct = FT(); et = FT(); kt = FT(); ut = FT()
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                           ctypes.byref(kt), ctypes.byref(ut))
    ctypes.windll.kernel32.CloseHandle(h)
    f = lambda x: (x.HighDateTime << 32) | x.LowDateTime
    return (f(kt) + f(ut)) / 1e7


def main():
    app = QApplication(sys.argv)
    w = app_main.MainWindow()
    w.show()
    pid = os.getpid()
    video = sys.argv[1] if len(sys.argv) > 1 else r"D:\BaiduNetdiskDownload\1080P官方中字.mp4"

    def t1():
        print(f"[bench] 加载 {video}", flush=True)
        w.load_video(video)

    def t2():
        m = w.video.mpv
        try:
            print(f"[bench] 解码: {m.video_format} {m.width}x{m.height} "
                  f"hwdec={m.hwdec_current} dur={float(m.duration or 0):.1f}s", flush=True)
        except Exception as e:
            print("[bench] 状态读取失败", e, flush=True)
        c0, m0 = cpu_s(pid), mem_mb(pid)
        dt = 10.0

        def sample():
            c1, m1 = cpu_s(pid), mem_mb(pid)
            print(f"[bench] 播放 10 秒：CPU {((c1 - c0) / dt) * 100:.1f}% 单核 | "
                  f"工作集 {m1:.1f} MB (起 {m0:.1f} MB)", flush=True)
            app.exit(0)

        QTimer.singleShot(int(dt * 1000), sample)

    QTimer.singleShot(1500, t1)
    QTimer.singleShot(4500, t2)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

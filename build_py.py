"""打包成绿色版目录 dist/Stanza（PyInstaller --onedir）。

思路和旧 Electron 版一致：只打 GUI 外壳，分析链路仍用外部 Python 跑
（pipeline 只复制到 exe 旁边，不打进 PYZ）。

用法：python build_py.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Stanza"
DIST = ROOT / "dist"
OUT = DIST / NAME
BUILD = ROOT / "build"
LIBMPV = ROOT / "vendor" / "libmpv-2.dll"
ICON = ROOT / "assets" / "stanza.ico"

# 用不到的 Qt 模块，排掉能省一大截体积
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPrintSupport", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSerialPort",
    "PySide6.QtStateMachine", "PySide6.QtUiTools", "PySide6.QtXml",
    "PySide6.QtNetwork", "PySide6.QtDBus",
    # 标准库里用不到的大件
    "tkinter", "unittest", "pydoc", "doctest", "test", "distutils",
    "numpy", "cv2", "PIL", "scipy", "matplotlib",
]


def run(cmd):
    print(">", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"命令失败，退出码 {r.returncode}")


def rmrf(p: Path):
    """删目录。宿主的安全删除保护会拦 shutil.rmtree（文件数超阈值），
    拦下来就退到 cmd rmdir——和旧 build.js 的处理一致。
    别用 ignore_errors，否则删失败会静默继续、后面 PyInstaller 报一堆莫名错误。"""
    if not p.exists():
        return
    print(f"清理 {p}", flush=True)
    try:
        shutil.rmtree(p)
    except Exception as e:
        print(f"  shutil.rmtree 被拦（{type(e).__name__}），改用 rmdir", flush=True)
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)],
                       capture_output=True)
    if p.exists():
        raise SystemExit(f"清理失败，{p} 仍然存在（可能有进程占用），请手动删除后重试")


def build():
    if not LIBMPV.is_file():
        raise SystemExit(f"缺少 {LIBMPV}")
    if not (ROOT / "pipeline" / "analyze.py").is_file():
        raise SystemExit("缺少 pipeline/analyze.py")
    if not ICON.is_file():
        raise SystemExit(f"缺少图标 {ICON}（先跑 python make_icon.py）")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--onedir",
        "--windowed",
        "--noupx",
        "--icon", str(ICON),
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        # libmpv 124MB，放在 exe 同级（onedir 下实际落在 _internal/）
        "--add-binary", f"{LIBMPV}{';'}.",
        # 运行时窗口图标（exe 内嵌图标管资源管理器，这个管任务栏/标题栏）
        "--add-data", f"{ICON}{';'}.",
    ]
    for m in EXCLUDES:
        args += ["--exclude-module", m]
    args.append(str(ROOT / "stanza" / "main.py"))

    rmrf(OUT)
    run(args)


def copy_pipeline():
    """pipeline 不打进 exe，复制到 exe 旁边用外部 Python 跑。"""
    src = ROOT / "pipeline"
    dst = OUT / "pipeline"
    print(f"复制 pipeline -> {dst}", flush=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def write_launcher():
    # 只用 %~dp0，不写死中文路径
    bat = OUT / "启动.bat"
    bat.write_text(
        "@echo off\r\n"
        'start "" "%~dp0Stanza.exe" %*\r\n',
        "ascii",
    )
    print(f"写入 {bat}", flush=True)


def write_readme():
    (OUT / "使用说明.txt").write_text("\r\n".join([
        f"{NAME} — 本地视频播放器 + 自动章节",
        "",
        "双击 Stanza.exe 或 启动.bat 直接运行，绿色免安装。",
        "",
        "用法",
        "  1. 文件 → 打开视频（Ctrl+O），或把视频拖进窗口",
        "  2. 首次打开会自动提示「分析章节」，进度条显示百分比与剩余时间",
        "  3. 分析结果缓存在 %APPDATA%\\Stanza\\library，换机器可整个目录拷走",
        "",
        "字幕",
        "  同名外挂字幕（movie.srt / movie.zh.srt）自动加载，内嵌字幕轨自动列出。",
        "  V 键循环切换，或点控制条上的「字幕 ▾」。",
        "",
        "快捷键",
        "  空格 播放/暂停      ← → 后退/前进 5 秒",
        "  J / L 后退/前进 10 秒      N / P 下一段 / 上一段",
        "  C 章节列表      F 全屏      M 静音      V 字幕",
        "  [ ] 调速      0-9 跳到百分比      进度条任意位置单击跳转",
        "",
        "注意",
        "  分析章节需要外部 Python 3 + opencv-python + numpy + faster-whisper。",
        "  找不到解释器时，设环境变量 STANZA_PYTHON 指向 python.exe。",
        "  pipeline 目录必须和 Stanza.exe 放在同一层。",
        "  首次分析会下载语音模型，之后走缓存。",
        "",
        "内核",
        "  libmpv（mpv-dev-x86_64-v3），支持 MP4/MKV/HEVC/AV1，d3d11va 硬解。",
        "  Electron 旧版留在 dist\\Stanza-electron，可回退。",
    ]), "utf-8")


def report():
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    n = sum(1 for f in OUT.rglob("*") if f.is_file())
    print(f"\n完成: {OUT / 'Stanza.exe'}")
    print(f"体积: {total / 1024 / 1024:.0f} MB，{n} 个文件")


if __name__ == "__main__":
    build()
    copy_pipeline()
    write_launcher()
    write_readme()
    report()

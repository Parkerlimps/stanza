"""跑 pipeline/analyze.py 的子进程封装（QProcess）。

进度协议沿用现有约定：Python 往 stdout 打 `PROGRESS {json}`，这里解析后发信号。
"""
import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

from paths import app_dir

ROOT = app_dir()
PIPELINE = ROOT / "pipeline"


def find_python() -> str:
    """找一个能跑 pipeline 的外部解释器。

    打包后 `sys.executable` 是 Stanza.exe，不是 python——拿它当解释器会
    变成「用播放器 exe 去执行 analyze.py」，直接起不来。所以 frozen 时必须跳过。
    """
    if not getattr(sys, "frozen", False):
        if sys.executable and "python" in Path(sys.executable).name.lower():
            return sys.executable

    env = os.environ.get("STANZA_PYTHON")
    if env and Path(env).is_file():
        return env

    candidates = [
        Path.home() / ".workbuddy/binaries/python/envs/default/Scripts/python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python313/python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python312/python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python311/python.exe",
        Path("C:/Python313/python.exe"),
        Path("C:/Python312/python.exe"),
        Path("C:/Python311/python.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("python") or "python"


def python_hint() -> str:
    """找不到解释器时给用户看的提示。"""
    return ("分析章节需要外部 Python 3（带 opencv-python / numpy / faster-whisper）。\n"
            "可以设环境变量 STANZA_PYTHON 指向 python.exe，\n"
            f"或把 pipeline 目录放在 {ROOT} 下。")


class AnalysisRunner(QObject):
    progress = Signal(dict)        # {"label":..., "pct":...}
    log_line = Signal(str)
    finished = Signal(bool, str)   # ok, err_message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self._buf = ""
        self._tail = ""

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self, args: list[str]):
        """通用入口：args 已含全部参数。"""
        if self.running():
            self.finished.emit(False, "已有任务在跑")
            return
        script = PIPELINE / "analyze.py"
        if not script.is_file():
            self.finished.emit(False, f"找不到分析脚本：\n{script}")
            return
        self._buf = ""
        self._tail = ""
        py = find_python()
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(str(ROOT))
        self.proc.readyReadStandardOutput.connect(self._pump)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._spawn_error)
        self.log_line.emit(f"$ {py} analyze.py " + " ".join(args))
        self.proc.start(py, [str(script), *args])

    def analyze_video(self, video: str, out_dir: Path, cfg: dict, count: int = 32,
                      use_llm: bool = True):
        args = [video, "--out", str(out_dir), "--reuse", str(out_dir),
                "--count", str(count), "--jobs", "0", "--model", "small"]
        if use_llm and cfg.get("llmUrl") and cfg.get("llmModel"):
            # 语义分段要读全片台词，整片转写比只转段首慢一个数量级，
            # 所以只在模型确实可用时才打开
            args += ["--llm", cfg["llmUrl"], "--llm-model", cfg["llmModel"],
                     "--segment", "auto", "--transcript", "auto"]
        else:
            args += ["--segment", "equal", "--transcript", "sub"]
        self.start(args)

    def retitle(self, out_dir: Path, cfg: dict, use_llm: bool = True):
        args = ["--retitle", str(out_dir)]
        if use_llm and cfg.get("llmUrl") and cfg.get("llmModel"):
            args += ["--llm", cfg["llmUrl"], "--llm-model", cfg["llmModel"]]
        self.start(args)

    def stop(self):
        if self.running():
            self.proc.kill()

    # ------------------------------------------------------------ 内部

    def _pump(self):
        if not self.proc:
            return
        self._buf += bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                try:
                    self.progress.emit(json.loads(line[9:]))
                except Exception:
                    pass
            else:
                self._tail = (self._tail + line + "\n")[-600:]
                self.log_line.emit(line)

    def _spawn_error(self, err):
        self.finished.emit(False, f"启动分析失败：{err}\n{find_python()}\n\n{python_hint()}")

    def _done(self, code, _status):
        ok = code == 0
        self.finished.emit(ok, "" if ok else f"分析失败（退出码 {code}）\n{self._tail.strip()}")
        self.proc = None

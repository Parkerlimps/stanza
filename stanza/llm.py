"""本地大模型服务的探测与拉起（对齐旧 Electron 版 main.js 的 ensureLLM）。

为什么需要：config.json 里配了 llmUrl/llmModel，但服务没起来时 `titles.build()`
会静默退回 heuristic，标题就变成「说一些说一下说下在泰…」这种台词碎片。
用户以为在起标题，其实大模型根本没参与。
"""
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from PySide6.QtCore import QThread, Signal

PROBE_TIMEOUT = 1.5
POLL_INTERVAL = 2.0


def probe(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """探一下 OpenAI 兼容服务在不在。绕开系统代理——本机服务走代理必失败。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        opener = build_opener(ProxyHandler({}))
        req = Request(f"http://{host}:{port}/v1/models", method="GET")
        with opener.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


class LlmStarter(QThread):
    """探测 → 没在跑就按 llmLaunch 拉起来 → 轮询等 llmWait 秒。

    整个过程可能几十秒（模型首次加载要编译），必须放线程里，不能卡 UI。
    """

    progress = Signal(str)             # 进度面板上的一行字
    finished_ok = Signal(bool, str)    # ok, 失败原因

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg or {})

    def run(self):
        url = (self.cfg.get("llmUrl") or "").strip()
        if not url:
            self.finished_ok.emit(False, "没有配置模型地址")
            return
        if probe(url):
            self.finished_ok.emit(True, "")
            return

        launch = (self.cfg.get("llmLaunch") or "").strip()
        if not launch:
            self.finished_ok.emit(False, "大模型没在运行，也没配启动命令")
            return

        self.progress.emit("正在启动本地模型…")
        try:
            cwd = str(Path(launch).parent) if Path(launch).exists() else os.getcwd()
            subprocess.Popen(
                launch, cwd=cwd, shell=True, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.finished_ok.emit(False, f"启动命令执行失败：{e}")
            return

        wait = float(self.cfg.get("llmWait") or 180)
        t0 = time.time()
        while time.time() - t0 < wait:
            self.msleep(int(POLL_INTERVAL * 1000))
            if self.isInterruptionRequested():
                self.finished_ok.emit(False, "已取消")
                return
            if probe(url):
                self.finished_ok.emit(True, "")
                return
            self.progress.emit(f"等待模型加载 {int(time.time() - t0)}s")
        self.finished_ok.emit(False, f"等了 {int(wait)} 秒模型还没起来")

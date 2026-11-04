"""路径解析：开发态用项目根目录，打包态（PyInstaller）用 exe 所在目录。

打包后 `__file__` 指向 `_internal` 里的解包位置，`parent.parent` 完全不是项目根，
所以凡是「找 pipeline / vendor / 资源」的地方都必须走这里。
"""
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录。打包后是 Stanza.exe 的同级目录。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def dll_dirs() -> list[Path]:
    """可能放着 libmpv-2.dll 的目录。

    PyInstaller 6 的 onedir 会把 --add-binary 塞进 `_internal/`，
    开发态在项目根的 `vendor/`，所以两个都得试。
    """
    base = app_dir()
    cands = [base, base / "_internal", base / "vendor"]
    return [d for d in cands if d.is_dir()]


def resource(name: str):
    """找随程序分发的资源（图标等）。打包后在 _internal，开发态在 assets/。"""
    base = app_dir()
    for c in (base / name, base / "_internal" / name, base / "assets" / name):
        if c.is_file():
            return c
    return None

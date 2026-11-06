"""配置与缓存路径。兼容 Electron 版的 %APPDATA%\\Stanza 目录结构。"""
import json
import hashlib
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Stanza"
LIB_DIR = APP_DIR / "library"
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "llmUrl": "",
    "llmModel": "",
    "llmLaunch": "",
    "llmWait": 180,
    # "auto" 表示由模型的段落判断决定章节数；也可存 16 / 24 / 32 / 48
    "chapterCount": "auto",
}


def cache_key(video_path) -> str:
    """缓存键。与 Electron 版规则一致：md5(绝对路径小写)[:12]。"""
    p = str(Path(video_path).resolve())
    return hashlib.md5(p.lower().encode("utf-8")).hexdigest()[:12]


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    except Exception:
        pass
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    cfg.update(patch or {})
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    return cfg


def chapters_for(video_path):
    """返回 (key, chapters_json_path 或 None)。先查缓存目录，再查同名 sidecar。"""
    key = cache_key(video_path)
    cached = LIB_DIR / key / "chapters.json"
    if cached.is_file():
        return key, cached
    sidecar = Path(video_path).with_suffix(".chapters.json")
    if sidecar.is_file():
        return key, sidecar
    return key, None


def out_dir_for(video_path) -> Path:
    d = LIB_DIR / cache_key(video_path)
    d.mkdir(parents=True, exist_ok=True)
    return d

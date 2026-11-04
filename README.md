# Stanza · 会自动分章的本地视频播放器

> 一个给「看长视频总在拖进度条」的人用的本地播放器：拖进去，自动切章节、配缩略图、起标题
> 版本 v0.1.0 · 桌面客户端 · by PALI · Windows 优先
> MIT License · 源码完整开放

---

## 1. 为什么做这个

课程录像、演唱会、纪录片、动画常常没有任何章节信息。想回到「刚才那段」只能凭记忆拖进度条，
拖过头再来一次。手工打点又太麻烦，长片根本没人愿意做。

Stanza 把这件事放在本地自动完成：检测镜头边界 → 聚合成合理长度的章节 → 每章抽一张关键帧 →
从台词里压出一个短标题，结果显示在侧边章节抽屉里，点一下就跳过去。

两条设计原则：

- **全程本地**：视频不出本机，分析链路用外部 Python 跑，大模型也是本机 OpenAI 兼容服务。
- **可降级**：没有字幕、没有 ASR、连不上大模型，都不会让功能消失，最差退回无依赖的启发式标题。

## 2. 功能

| | |
|---|---|
| 播放内核 | libmpv（MP4 / MKV / HEVC / AV1，d3d11va 硬解），外挂字幕自动加载、内嵌字幕轨切换 |
| 自动章节 | 镜头检测 → 动态规划聚合，控制章节数量、最短间隔，避免切得碎或长短悬殊 |
| 章节抽屉 | 每章一张关键帧缩略图 + 时间点 + 标题，点击跳转 |
| 台词来源 | 外挂字幕（< 1 秒）→ 语音转写（SenseVoice / faster-whisper）→ 硬字幕 OCR（最慢，显式指定才用） |
| 语义分段 | 本地大模型只判断「这 ±1 分钟里有没有新段落开始」，不让它直接算时间点或章节数 |
| 标题 | 本地 LLM 压成短标题；连不上时自动退回启发式 |
| **标题不翻译** | 英文台词出英文标题，日文出日文。模型容易顺手把英文歌词意译成中文，代码里做了文字系统校验，不符就丢弃并退回同语言标题 |
| 缓存 | `%APPDATA%\Stanza\library\<md5(绝对路径小写)[:12]>\chapters.json`，换机器可整个目录拷走 |

快捷键：

```
空格   播放 / 暂停         ← →    后退 / 前进 5 秒
J / L  后退 / 前进 10 秒   N / P  下一段 / 上一段
C      章节列表            F      全屏
M      静音                V      循环切换字幕
[ ]    调速                0-9    跳到百分比
```

## 3. 架构

```
stanza/            主程序（PySide6 + python-mpv，单进程）
  main.py          主窗口、键盘、分析任务调度
  player.py        MpvVideo / ChapterRail / 章节抽屉
  analysis.py      QProcess 调 pipeline/analyze.py
  llm.py           探测并拉起本地大模型服务（QThread，不卡 UI）
  config.py        配置与章节缓存路径
  paths.py         打包态 / 开发态统一的资源路径入口
pipeline/          分析链路，由外部 Python 运行，不打进 exe
  analyze.py       镜头检测 → DP 聚合 → 关键帧 → chapters.json
  transcript.py    全片台词（字幕 / ASR / OCR 三选一）
  segment.py       本地 LLM 局部语义分段（分数只作 DP 权重）
  titles.py        LLM 起标题 + 语言校验 + 启发式兜底
  asr.py           SenseVoice（sherpa-onnx）/ faster-whisper 后端
  ocrsub.py        硬字幕 OCR
app/ main.js preload.js serve.js build.js   旧 Electron 版，保留作回退
build_py.py        PyInstaller onedir 打包 → dist/Stanza/
make_icon.py       生成 assets/stanza.ico
```

主窗口只管界面和键盘，mpv 只留鼠标默认绑定；分析放在子进程，避免长任务卡住 UI。

## 4. 跑起来

### 开发态

```bash
pip install -r requirements.txt
# 把 libmpv-2.dll 放到 vendor/（见第 6 节）
python stanza/main.py
```

Windows 下也可双击 `stanza/start_stanza.bat`。解释器按以下顺序选取：
环境变量 `STANZA_PYTHON` → 同目录 `python.local.txt`（本机私有，不提交） → PATH 里的 `python`。

### 打包态

```bash
python make_icon.py     # 可选，重新生成图标
python build_py.py      # → dist/Stanza/，双击 启动.bat 即可绿色运行
```

`pipeline/` 不进 PYZ，打包脚本会复制到 exe 旁边，由外部 Python 执行。
**改过 `pipeline/` 后要手动重新复制一遍**到 `dist/Stanza/pipeline/`，否则 exe 里跑的还是旧逻辑。

### 本地大模型（可选）

`%APPDATA%\Stanza\config.json` 里可配 `llmUrl` / `llmModel` / `llmLaunch` / `llmWait`。
支持 LM Studio、Ollama、vLLM、llama.cpp server 等 OpenAI 兼容接口。
程序启动时会探测 `/v1/models`，没起来就按 `llmLaunch` 拉起并等待；请求本机地址时强制绕过系统代理。

## 5. 分析链路怎么工作

```
视频 → 全片台词（字幕优先）
     → 镜头检测得到候选边界
     → 本地 LLM 对局部窗口打分（哪里像新段落）
     → 动态规划：长度约束 + 边界吸附 + 分数作权重，决定最终切点
     → 每章抽关键帧
     → LLM 起标题 / 语言校验 / 启发式兜底
     → chapters.json + 缩略图
```

刻意不让模型输出章节数或时间点：它算不准时间，也放不下整片台词。
全局约束交给确定性的 DP，模型只提供「切在这里有多值」的权重，因此结果可校验、可复现。

## 6. 二进制与模型不随仓库分发

以下文件超过 GitHub 单文件 100 MB 限制，或属于第三方预编译产物，均已在 `.gitignore` 排除：

| 文件 | 大小 | 获取方式 |
|---|---|---|
| `vendor/libmpv-2.dll` | ~119 MB | [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake) 的 `mpv-dev-x86_64-v3-*.7z`；用 7zr.exe 解压（py7zr 不支持 BCJ2），取 DLL 放到 `vendor/` |
| `vendor/ffmpeg/ffmpeg.exe` | ~100 MB | 仅造测试素材用，非运行必需；[gyan.dev 构建](https://www.gyan.dev/ffmpeg/builds/) |
| `dist/`、`build/`、`node_modules/` | 数百 MB | 由 `build_py.py` / `npm install` 本地生成 |
| ASR / OCR 模型权重 | 数百 MB | 首次分析时由各自后端自动下载 |

`libmpv-2.dll` 必须在 `import mpv` 之前通过 `os.add_dll_directory()` 注入，`paths.dll_dirs()`
会同时试开发态的 `vendor/` 和打包态的 `_internal/`。

## 7. 为什么从 Electron 换成 PySide6

旧版用 Electron 内嵌 libmpv，需要 node-gyp 编译原生模块，而目标机器没有 C++ 编译器。
python-mpv 是纯 ctypes，零编译，于是整体换成 PySide6 外壳。同一段 1920×800 H.264 硬解实测：

| | 进程数 | 内存 | 单核占用 |
|---|---|---|---|
| Electron 版 | 4 | 790 MB | 30.6% |
| PySide6 版 | 1 | 315 MB | 3.9% |

旧 Electron 实现仍在 `main.js` / `app/` / `preload.js` / `serve.js`，仅作回退，不再维护。

## 8. 已知约束

- Windows 优先：DLL 加载、`os.add_dll_directory()`、打包脚本都按 Windows 写。
- mpv 是原生子窗口，控件画不到它上面，浮层提示走 mpv 自己的 OSD；视频区保持直角。
- 硬字幕 OCR 全片很慢（约 1.8 秒/帧），默认只在显式指定时启用。
- 无音轨文件不能用 `python-mpv` 的 `play()`，必须 `command("loadfile", path, "replace")`。
- 章节标题语言必须与台词的文字系统一致，不翻译。

## 9. 第三方

本项目仅通过公开 API 调用下列组件，**未修改任何上游源码**；二进制与模型均不随本仓库分发，
各自遵循上游许可：

| 组件 | 上游仓库 | 许可 |
|---|---|---|
| libmpv / mpv | [mpv-player/mpv](https://github.com/mpv-player/mpv) | GPLv2+ 或 LGPLv2.1+ 双许可（`LICENSE.GPL` / `LICENSE.LGPL`） |
| python-mpv | [jaseg/python-mpv](https://github.com/jaseg/python-mpv) | 见上游 `LICENSE` |
| PySide6 (Qt for Python) | [qtproject/pyside-pyside-setup](https://code.qt.io/cgit/pyside/pyside-setup.git/) | LGPLv3 / GPLv3 |
| OpenCV | [opencv/opencv](https://github.com/opencv/opencv) | Apache-2.0 |
| faster-whisper | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| SenseVoice | [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice) | MIT |
| sherpa-onnx | [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 |
| RapidOCR | [RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0 |
| Electron（旧版） | [electron/electron](https://github.com/electron/electron) | MIT |

## 10. License

MIT © 2026 PALI — 见 [LICENSE](LICENSE)。

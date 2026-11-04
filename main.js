const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const http = require('http');
const { spawn } = require('child_process');
const { startServer } = require('./serve');

// 注意：不能叫 cache —— Windows 上 userData/Cache 是 Chromium 自己的 HTTP 缓存目录
// （大小写不敏感），放进去会被它当成垃圾清掉。
const CACHE_DIR = path.join(app.getPath('userData'), 'library');
const CONFIG_PATH = path.join(app.getPath('userData'), 'config.json');

function loadConfig() {
  try {
    return { llmUrl: '', llmModel: '', llmLaunch: '', llmWait: 180, ...JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')) };
  } catch (_) {
    return { llmUrl: '', llmModel: '' };
  }
}

function saveConfig(patch) {
  const next = { ...loadConfig(), ...(patch || {}) };
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(next, null, 2), 'utf8');
  return next;
}
const VIDEO_EXT = ['.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.ts', '.mp3', '.flac', '.wav'];

let win = null;
let serverInfo = null;
let analyzing = false;
let llmChild = null;

function cacheKeyFor(filePath) {
  return crypto.createHash('md5').update(filePath.toLowerCase()).digest('hex').slice(0, 12);
}

/* --------------------------------------------------- 音轨探测
 * Chromium 的 <video> 只带 AAC/MP3/Opus/FLAC 解码器，AC-3 / E-AC-3 / DTS / TrueHD
 * 一个都不认（画面照放、声音全无）。所以先读 MP4 的 stsd，把音频 fourcc 挖出来。
 */

const ATOM_CONTAINERS = new Set(['moov', 'trak', 'mdia', 'minf', 'stbl', 'edts', 'dinf', 'udta', 'mvex']);
const AUDIO_UNSUPPORTED = new Set(['ac-3', 'ec-3', 'dtsc', 'dtsh', 'dtsl', 'dtse', 'dtsx', 'truehd', 'mlp ']);

function walkAtoms(buf, start, end, ctx, out) {
  let p = start;
  while (p + 8 <= end) {
    let size = buf.readUInt32BE(p);
    const type = buf.toString('latin1', p + 4, p + 8);
    let hdr = 8;
    if (size === 1) {
      size = Number(buf.readBigUInt64BE(p + 8));
      hdr = 16;
    } else if (size === 0) {
      size = end - p;
    }
    if (size < hdr || p + size > end) return;

    const body = p + hdr;
    const bodyEnd = p + size;

    // 注意：ctx 要在同一层里持续生效，hdlr 在 mdia 里排在 minf 前面
    if (type === 'trak') ctx = {};
    if (type === 'hdlr') ctx = { ...ctx, handler: buf.toString('latin1', body + 8, body + 12) };
    if (type === 'stsd' && ctx.handler) {
      // stsd: 4B version/flags + 4B entry_count，然后 4B size + 4B format
      out[ctx.handler] = buf.toString('latin1', body + 12, body + 16);
    }
    if (ATOM_CONTAINERS.has(type)) walkAtoms(buf, body, bodyEnd, ctx, out);
    p = bodyEnd;
  }
}

/** 返回 { video: 'avc1', audio: 'ec-3' }；不是 MP4 或解析失败返回 null */
function probeTracks(filePath) {
  let fd;
  try {
    fd = fs.openSync(filePath, 'r');
    const size = fs.fstatSync(fd).size;
    const head = Buffer.alloc(16);
    let pos = 0;
    while (pos + 8 <= size) {
      fs.readSync(fd, head, 0, 16, pos);
      let sz = head.readUInt32BE(0);
      const type = head.toString('latin1', 4, 8);
      let hdr = 8;
      if (sz === 1) {
        sz = Number(head.readBigUInt64BE(8));
        hdr = 16;
      } else if (sz === 0) {
        sz = size - pos;
      }
      if (sz < hdr || pos + sz > size + 8) break;
      if (type === 'moov') {
        const moov = Buffer.alloc(sz - hdr);
        fs.readSync(fd, moov, 0, moov.length, pos + hdr);
        const out = {};
        walkAtoms(moov, 0, moov.length, {}, out);
        return out;
      }
      pos += sz;
    }
  } catch (_) {
    /* 读不动就当探测失败 */
  } finally {
    if (fd !== undefined) {
      try { fs.closeSync(fd); } catch (_) {}
    }
  }
  return null;
}

function findFfmpeg() {
  const cfg = loadConfig();
  const cands = [
    cfg.ffmpegPath,
    path.join(__dirname, 'vendor', 'ffmpeg', 'ffmpeg.exe'),
    path.join(__dirname, 'vendor', 'ffmpeg.exe'),
  ].filter(Boolean);
  for (const c of cands) {
    if (fs.existsSync(c)) return c;
  }
  return 'ffmpeg'; // 交给 PATH，找不到会走 spawn 的 error
}

let audioJob = null;

/**
 * 把不兼容的音轨单独转成 AAC（.m4a），画面继续用原文件播。
 * 两小时片源大约 5~6 分钟，转一次缓存住，之后秒开。
 */
function prepareAudio(videoPath, duration) {
  if (!videoPath) return;
  const key = cacheKeyFor(videoPath);
  const outDir = path.join(CACHE_DIR, key);
  fs.mkdirSync(outDir, { recursive: true });
  const out = path.join(outDir, 'audio.m4a');
  const notify = (payload) => win && win.webContents.send('audio', payload);

  if (fs.existsSync(out) && fs.statSync(out).size > 4096) {
    notify({ type: 'ready', url: `/cache/${key}/audio.m4a`, cached: true });
    return;
  }

  if (audioJob) {
    try { audioJob.kill(); } catch (_) {}
    audioJob = null;
  }

  const tmp = path.join(outDir, 'audio.part.m4a');
  const args = [
    '-hide_banner', '-nostdin', '-loglevel', 'error',
    '-i', videoPath,
    '-vn', '-map', '0:a:0',
    '-c:a', 'aac', '-b:a', '256k',
    '-movflags', '+faststart',
    '-progress', 'pipe:1',
    '-y', tmp,
  ];

  notify({ type: 'start' });
  const child = spawn(findFfmpeg(), args, { windowsHide: true });
  audioJob = child;

  let buf = '';
  let tail = '';
  child.stdout.on('data', (chunk) => {
    buf += chunk.toString('utf8');
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line.startsWith('out_time_us=')) continue;
      const us = Number(line.slice(12));
      if (duration > 0 && isFinite(us)) {
        notify({ type: 'progress', pct: Math.min(99, (us / 1e6 / duration) * 100) });
      }
    }
  });
  child.stderr.on('data', (chunk) => {
    tail = (tail + chunk.toString('utf8')).slice(-500);
  });
  child.on('error', (err) => {
    audioJob = null;
    notify({ type: 'error', message: `启动 ffmpeg 失败：${err.message}` });
  });
  child.on('close', (code) => {
    audioJob = null;
    if (code === 0 && fs.existsSync(tmp)) {
      try { fs.renameSync(tmp, out); } catch (_) {}
      notify({ type: 'ready', url: `/cache/${key}/audio.m4a` });
    } else {
      notify({ type: 'error', message: `音轨转换失败（退出码 ${code}）${tail ? '\n' + tail : ''}` });
    }
  });
}


function findPython() {
  const candidates = [
    path.join(process.env.USERPROFILE || '', '.workbuddy', 'binaries', 'python', 'envs', 'default', 'Scripts', 'python.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python311', 'python.exe'),
  ];
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c;
  }
  return 'python';
}

function chaptersFor(videoPath) {
  const key = cacheKeyFor(videoPath);
  const cached = path.join(CACHE_DIR, key, 'chapters.json');
  if (fs.existsSync(cached)) {
    return { key, url: `/cache/${key}/chapters.json` };
  }
  const sidecar = videoPath.replace(/\.[^.]+$/, '.chapters.json');
  if (fs.existsSync(sidecar)) {
    return { key, url: `/fs?p=${encodeURIComponent(sidecar)}` };
  }
  return { key, url: null };
}

function buildPayload(videoPath) {
  const name = path.basename(videoPath);
  const { key, url } = chaptersFor(videoPath);
  const tracks = probeTracks(videoPath);
  // 轨道按 hdlr 的 handler_type 归类：'vide' / 'soun'
  const audioCodec = tracks && tracks.soun ? tracks.soun.trim() : null;
  return {
    videoPath,
    videoUrl: `/fs?p=${encodeURIComponent(videoPath)}`,
    chaptersUrl: url,
    thumbBase: url ? url.replace(/[^/]*$/, '') : null,
    title: name,
    key,
    audioCodec,
    needsAudio: !!(audioCodec && AUDIO_UNSUPPORTED.has(audioCodec)),
  };
}

function sendPayload(videoPath) {
  if (!win) return;
  win.setTitle(`${path.basename(videoPath)} — Stanza`);
  win.webContents.send('media', buildPayload(videoPath));
}

async function openVideo() {
  const res = await dialog.showOpenDialog(win, {
    title: '选择视频',
    properties: ['openFile'],
    filters: [
      { name: '视频', extensions: VIDEO_EXT.map((e) => e.slice(1)) },
      { name: '全部文件', extensions: ['*'] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return;
  sendPayload(res.filePaths[0]);
}

function analyze(videoPath) {
  if (!videoPath || analyzing) return;
  const outDir = path.join(CACHE_DIR, cacheKeyFor(videoPath));
  fs.mkdirSync(outDir, { recursive: true });

  const args = [videoPath, '--out', outDir, '--count', '32', '--jobs', '0', '--asr', 'auto', '--model', 'small'];
  const cfg = loadConfig();
  if (cfg.llmUrl && cfg.llmModel) args.push('--llm', cfg.llmUrl, '--llm-model', cfg.llmModel);

  runPipeline(args, videoPath, '正在分析章节', 'analyze');
}

/** 探一下模型服务在不在。用裸 http，避免任何代理干扰。 */
function probeLLM(url, timeout = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (ok) => {
      if (!settled) {
        settled = true;
        resolve(ok);
      }
    };
    try {
      const u = new URL(url);
      const req = http.get(
        { host: u.hostname, port: u.port || 80, path: '/v1/models', timeout },
        (res) => {
          res.resume();
          done(res.statusCode === 200);
        }
      );
      req.on('timeout', () => { req.destroy(); done(false); });
      req.on('error', () => done(false));
    } catch (_) {
      done(false);
    }
  });
}

/**
 * 确保模型服务可用：已在跑就直接返回；
 * 配了启动命令就拉起来，再轮询等它 ready（首次加载要编译，可能几十秒）。
 */
async function ensureLLM(cfg, notify) {
  if (!cfg.llmUrl) return false;
  if (await probeLLM(cfg.llmUrl)) return true;
  if (!cfg.llmLaunch) return false;

  notify({ type: 'progress', pct: 2, label: '正在启动本地模型…' });
  const cwd = fs.existsSync(cfg.llmLaunch) ? path.dirname(cfg.llmLaunch) : process.cwd();

  try {
    const child = spawn(cfg.llmLaunch, { cwd, shell: true, windowsHide: true, stdio: 'ignore' });
    child.on('error', () => { /* 启动失败下面靠轮询判定 */ });
    child.unref();
    llmChild = child;
  } catch (_) {
    return false;
  }

  const wait = (cfg.llmWait || 180) * 1000;
  const t0 = Date.now();
  while (Date.now() - t0 < wait) {
    await new Promise((r) => setTimeout(r, 2000));
    if (await probeLLM(cfg.llmUrl)) return true;
    notify({
      type: 'progress',
      pct: Math.min(6, 2 + ((Date.now() - t0) / 1000) * 0.1),
      label: `等待模型加载 ${Math.round((Date.now() - t0) / 1000)}s`,
    });
  }
  return false;
}

async function retitle(videoPath) {
  if (!videoPath || analyzing) return;
  const outDir = path.join(CACHE_DIR, cacheKeyFor(videoPath));
  const jf = path.join(outDir, 'chapters.json');
  const cfg = loadConfig();

  if (!fs.existsSync(jf)) {
    if (win) win.webContents.send('analyze', { type: 'error', message: '还没有章节数据，先分析一次' });
    return;
  }

  const notify = (payload) => win && win.webContents.send('analyze', payload);

  if (cfg.llmUrl && cfg.llmModel) {
    analyzing = true;
    notify({ type: 'start', title: '正在准备大模型', kind: 'retitle', message: '检查服务…' });
    const ok = await ensureLLM(cfg, notify);
    analyzing = false;

    if (!ok) {
      notify({
        type: 'error',
        message: cfg.llmLaunch
          ? '模型启动超时或失败，检查启动命令'
          : '大模型没在运行。可在设置里填「启动命令」，或手动启动服务',
      });
      return;
    }
  }

  const args = ['--retitle', outDir];
  if (cfg.llmUrl && cfg.llmModel) args.push('--llm', cfg.llmUrl, '--llm-model', cfg.llmModel);

  runPipeline(args, videoPath, '正在重做标题', 'retitle');
}

function runPipeline(extraArgs, videoPath, jobTitle, kind) {
  const script = path.join(__dirname, 'pipeline', 'analyze.py');
  const py = findPython();
  analyzing = true;

  const notify = (payload) => win && win.webContents.send('analyze', payload);
  notify({ type: 'start', title: jobTitle, kind, message: '准备中…' });

  const child = spawn(py, [script, ...extraArgs], {
    cwd: __dirname,
    windowsHide: true,
    // 首次转写会从 HuggingFace 拉模型；若直连慢，可在系统环境变量里设
    // HF_ENDPOINT=https://hf-mirror.com 走镜像
    env: process.env,
  });

  let tail = '';
  const pump = (stream) => {
    let buf = '';
    stream.on('data', (chunk) => {
      buf += chunk.toString('utf8');
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        if (line.startsWith('PROGRESS ')) {
          try {
            notify({ type: 'progress', ...JSON.parse(line.slice(9)) });
          } catch (_) { /* 忽略坏行 */ }
        } else {
          tail = (tail + line + '\n').slice(-600);
          notify({ type: 'log', message: line });
        }
      }
    });
  };
  pump(child.stdout);
  pump(child.stderr);

  child.on('error', (err) => {
    analyzing = false;
    notify({ type: 'error', message: `启动分析失败：${err.message}\n需要 Python 3 + opencv-python` });
  });

  child.on('close', (code) => {
    analyzing = false;
    if (code === 0) {
      notify({ type: 'done' });
      sendPayload(videoPath);
    } else {
      notify({ type: 'error', message: `分析失败（退出码 ${code}）\n${tail}` });
    }
  });
}

function createMenu() {
  const template = [
    {
      label: '文件',
      submenu: [
        { label: '打开视频…', accelerator: 'CmdOrCtrl+O', click: () => openVideo() },
        {
          label: '分析当前视频章节',
          accelerator: 'CmdOrCtrl+R',
          click: () => win && win.webContents.send('request-analyze'),
        },
        {
          label: '重做章节标题…',
          accelerator: 'CmdOrCtrl+T',
          click: () => win && win.webContents.send('request-retitle'),
        },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '视图',
      submenu: [
        {
          label: '全屏',
          accelerator: 'F11',
          click: () => {
            if (win) win.setFullScreen(!win.isFullScreen());
          },
        },
        { role: 'reload', label: '重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        { label: '打开缓存目录', click: () => shell.openPath(CACHE_DIR) },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function createWindow() {
  serverInfo = await startServer({ root: __dirname, cacheDir: CACHE_DIR });

  win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#ffffff',
    title: 'Stanza',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  win.once('ready-to-show', () => win.show());
  win.loadURL(`http://127.0.0.1:${serverInfo.port}/app/index.html`);

  // 菜单/F11 走的是窗口全屏，和页面里的元素全屏是两套状态，得同步给前端
  const sendFs = (on) => {
    try {
      if (win && !win.isDestroyed() && win.webContents && !win.webContents.isDestroyed()) {
        win.webContents.send('window-fullscreen', on);
      }
    } catch (_) { /* 关窗瞬间发不出去就算了 */ }
  };
  win.on('enter-full-screen', () => sendFs(true));
  win.on('leave-full-screen', () => sendFs(false));

  win.webContents.on('did-finish-load', () => {
    win.webContents.send('desktop-info', { platform: process.platform });
    // 支持「把视频拖到 exe 上」或命令行直接传路径
    const argFile = process.argv
      .slice(1)
      .map((a) => a.replace(/^"+|"+$/g, ''))
      .find((a) => VIDEO_EXT.includes(path.extname(a).toLowerCase()) && fs.existsSync(a));
    if (argFile) sendPayload(path.resolve(argFile));
  });
}

app.whenReady().then(() => {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  createMenu();
  createWindow();

  ipcMain.handle('open-file', () => openVideo());
  ipcMain.on('analyze', (_e, videoPath) => analyze(videoPath));
  ipcMain.on('retitle', (_e, videoPath) => retitle(videoPath));
  ipcMain.on('prepare-audio', (_e, opts) => prepareAudio(opts && opts.path, (opts && opts.duration) || 0));
  ipcMain.on('set-window-fullscreen', (_e, on) => {
    if (win) win.setFullScreen(!!on);
  });
  ipcMain.handle('get-config', () => loadConfig());
  ipcMain.handle('set-config', (_e, patch) => saveConfig(patch));

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (serverInfo) serverInfo.close();
  if (process.platform !== 'darwin') app.quit();
});

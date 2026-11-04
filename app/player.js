const $ = (id) => document.getElementById(id);

const app = $('app');
const video = $('video');
const rail = $('rail');
const segments = $('segments');
const ticks = $('ticks');
const edge = $('edge');
const peek = $('peek');
const peekImg = $('peekImg');
const peekTitle = $('peekTitle');
const peekTime = $('peekTime');
const osd = $('osd');
const drawer = $('drawer');
const chapterList = $('chapterList');
const btnAnalyze = $('btnAnalyze');
const btnRetitle = $('btnRetitle');

const desktop = window.chapterApp || null;
const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

let chapters = [];
let shots = [];
let segEls = [];
let itemEls = [];
let thumbBase = '/data/';
let currentPath = null;
let duration = 0;
let currentIdx = -1;
let idleTimer = null;
let osdTimer = null;
let jobKind = 'analyze';
let jobStart = 0;
let jobFirst = 0;
let jobHideTimer = null;
let lastVolume = 1;
let audioEl = null;        // 外挂音轨（AC-3 / E-AC-3 这类 Chromium 解不了的）
let needsAudio = false;
let audioAsked = false;
let winFs = false;         // 窗口全屏（F11 / 菜单）

function fmt(t) {
  if (!isFinite(t) || t < 0) t = 0;
  const s = Math.floor(t % 60);
  const m = Math.floor((t / 60) % 60);
  const h = Math.floor(t / 3600);
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function chapterAt(t) {
  for (let i = chapters.length - 1; i >= 0; i--) {
    if (t >= chapters[i].start - 0.001) return i;
  }
  return 0;
}

/* ------------------------------------------------------------- 加载 */

async function boot() {
  const onMeta = () => {
    duration = video.duration || 0;
    $('dur').textContent = fmt(duration);
    $('loading').classList.add('gone');
    if (!chapters.length) renderPlaceholder();
    if (needsAudio && !audioAsked && desktop && currentPath) {
      audioAsked = true;
      desktop.prepareAudio(currentPath, duration);
    }
  };
  video.addEventListener('loadedmetadata', onMeta);
  video.addEventListener('canplay', onMeta);
  video.addEventListener('error', () => {
    $('loading').textContent = '这个编码解不了（HEVC / MKV 需要换 libmpv 内核）';
    $('loading').classList.remove('gone');
  });

  if (desktop) {
    document.body.classList.add('desktop');
    desktop.onMedia((payload) => applyMedia(payload));
    desktop.onAnalyze(handleAnalyze);
    if (desktop.onAudio) desktop.onAudio(handleAudio);
    if (desktop.onWindowFullscreen) {
      desktop.onWindowFullscreen((on) => {
        winFs = !!on;
        syncFsClass();
      });
    }
    if (desktop.onRequestRetitle) desktop.onRequestRetitle(() => startRetitle());
    desktop.onRequestAnalyze(() => startAnalyze());
    $('loading').textContent = '选择「文件 → 打开视频」开始';
    $('loading').classList.add('hint');
  } else {
    try {
      const res = await fetch('/data/chapters.json', { cache: 'no-store' });
      const data = await res.json();
      applyMedia({
        videoUrl: `/samples/${data.video.file}`,
        title: data.video.file,
        chaptersUrl: '/data/chapters.json',
      });
    } catch (e) {
      $('loading').textContent = '读不到 chapters.json，先跑 pipeline/analyze.py';
    }
  }

  requestAnimationFrame(loop);
  armIdle();
}

async function applyMedia(payload) {
  if (!payload) return;
  currentPath = payload.videoPath || null;

  detachAudio();
  video.muted = false;
  needsAudio = !!payload.needsAudio;
  audioAsked = false;

  chapters = [];
  shots = [];
  currentIdx = -1;
  segments.innerHTML = '';
  ticks.innerHTML = '';
  chapterList.innerHTML = '';
  segEls = [];
  itemEls = [];
  drawer.classList.remove('open');
  $('nowPlaying').classList.remove('show');
  $('nowMid').classList.remove('show');
  $('cur').textContent = '00:00';
  $('dur').textContent = '00:00';
  duration = 0;

  $('fileName').textContent = payload.title || '未命名';
  $('fileMeta').textContent = '';
  $('loading').classList.remove('gone');
  $('loading').classList.remove('hint');
  $('loading').textContent = '正在加载…';
  $('job').hidden = true;

  video.src = payload.videoUrl;
  video.load();

  btnAnalyze.hidden = !desktop;
  btnAnalyze.disabled = false;
  btnAnalyze.textContent = '分析章节';
  btnRetitle.hidden = true;

  if (!payload.chaptersUrl) return;

  btnAnalyze.hidden = true;
  btnRetitle.hidden = !desktop;
  try {
    const data = await (await fetch(payload.chaptersUrl, { cache: 'no-store' })).json();
    chapters = data.chapters || [];
    shots = data.shots || [];
    thumbBase = payload.chaptersUrl.replace(/[^/]*$/, '');
    if (data.video) {
      $('fileMeta').textContent = [
        data.video.width && `${data.video.width}×${data.video.height}`,
        data.video.fps && `${Math.round(data.video.fps)} fps`,
        data.video.duration && fmt(data.video.duration),
      ].filter(Boolean).join(' · ');
    }
    buildRail();
    buildList();
    currentIdx = -1;
  } catch (e) {
    $('fileMeta').textContent = '章节数据读取失败';
  }
}

function renderPlaceholder() {
  if (chapters.length) return;
  segments.innerHTML = '';
  const el = document.createElement('div');
  el.className = 'seg';
  el.style.flexGrow = '1';
  const fill = document.createElement('div');
  fill.className = 'fill';
  el.appendChild(fill);
  segments.appendChild(el);
  segEls = [{ el, fill }];
  chapters = [{ index: 0, start: 0, end: duration || 1, title: '整段', duration: duration || 0, thumb: '', strength: 0 }];
  currentIdx = -1;
}

function buildRail() {
  segments.innerHTML = '';
  ticks.innerHTML = '';
  segEls = [];
  const total = chapters.reduce((a, c) => a + (c.end - c.start), 0) || 1;

  chapters.forEach((c) => {
    const el = document.createElement('div');
    el.className = 'seg';
    el.style.flexGrow = String(c.end - c.start);
    const fill = document.createElement('div');
    fill.className = 'fill';
    el.appendChild(fill);
    segments.appendChild(el);
    segEls.push({ el, fill });
  });

  shots.forEach((s) => {
    const t = document.createElement('div');
    t.className = 'tick';
    t.style.left = `${(s.t / total) * 100}%`;
    ticks.appendChild(t);
  });
}

function buildList() {
  chapterList.innerHTML = '';
  itemEls = [];
  chapters.forEach((c) => {
    const b = document.createElement('button');
    b.className = 'chap';
    const img = c.thumb ? `<img src="${thumbBase}${c.thumb}" alt="" loading="lazy">` : '';
    b.innerHTML = `
      <span class="thumb">${img}</span>
      <span class="info">
        <span class="t">${escapeHtml(c.title)}</span>
        <span class="s">${fmt(c.start)} · ${Math.round(c.duration)}s</span>
      </span>`;
    b.addEventListener('click', () => {
      seekTo(c.start + 0.05);
      drawer.classList.remove('open');
    });
    chapterList.appendChild(b);
    itemEls.push(b);
  });
  $('chapterCount').textContent = chapters.length;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
}

/* ------------------------------------------------------------- 外挂音轨
 * Chromium 的 <video> 解不了 AC-3 / E-AC-3 / DTS，画面能放、声音是死的。
 * 主进程把音轨单独转成 AAC，这里用 <audio> 跟着视频走。
 */

function attachAudio(url) {
  if (!url) return;
  detachAudio();
  audioEl = new Audio();
  audioEl.preload = 'auto';
  audioEl.src = url;
  audioEl.volume = video.volume;
  audioEl.playbackRate = video.playbackRate;
  audioEl.currentTime = video.currentTime || 0;
  video.muted = true; // 内建音轨本来就没声，静音避免将来重复出声
  if (!video.paused) audioEl.play().catch(() => {});
  flashOSD('音轨已就绪');
}

function detachAudio() {
  if (!audioEl) return;
  try {
    audioEl.pause();
    audioEl.removeAttribute('src');
    audioEl.load();
  } catch (_) { /* 忽略 */ }
  audioEl = null;
}

video.addEventListener('play', () => {
  if (audioEl) audioEl.play().catch(() => {});
});
video.addEventListener('pause', () => {
  if (audioEl) audioEl.pause();
});
video.addEventListener('seeking', () => {
  if (audioEl && !dragging) audioEl.currentTime = video.currentTime;
});
video.addEventListener('ratechange', () => {
  if (audioEl) audioEl.playbackRate = video.playbackRate;
});
video.addEventListener('ended', () => {
  if (audioEl) audioEl.pause();
});


/* ------------------------------------------------------------- 主循环 */

function loop() {
  const t = video.currentTime;
  const p = duration ? Math.min(1, t / duration) : 0;

  edge.style.left = `${p * 100}%`;
  $('cur').textContent = fmt(t);

  for (let i = 0; i < segEls.length; i++) {
    const c = chapters[i];
    if (!c) continue;
    let v = (t - c.start) / Math.max(0.001, c.end - c.start);
    v = Math.max(0, Math.min(1, v));
    segEls[i].fill.style.width = `${(v * 100).toFixed(2)}%`;
    if (itemEls[i]) itemEls[i].style.setProperty('--p', v.toFixed(3));
  }

  const idx = chapters.length ? chapterAt(t) : -1;
  if (idx !== currentIdx) setActive(idx);

  // 外挂音轨对时：偏差大了直接拉回，小了不动（避免一直抖动）
  if (audioEl && !video.paused && !video.seeking && audioEl.readyState >= 2) {
    const drift = audioEl.currentTime - t;
    if (Math.abs(drift) > 0.3) audioEl.currentTime = t;
  }

  requestAnimationFrame(loop);
}

function setActive(idx) {
  currentIdx = idx;
  itemEls.forEach((el, i) => el.classList.toggle('active', i === idx));
  segEls.forEach((s, i) => s.el.classList.toggle('active', i === idx));
  const c = chapters[idx];
  if (!c) return;

  const np = $('nowPlaying');
  np.textContent = c.title;
  np.classList.add('show');

  const mid = $('nowMid');
  mid.innerHTML = `${escapeHtml(c.title)}<span class="dim"> · </span>${fmt(c.start)} – ${fmt(c.end)}`;
  mid.classList.add('show');

  if (drawer.classList.contains('open') && itemEls[idx]) {
    const el = itemEls[idx];
    const top = el.offsetTop - chapterList.offsetTop;
    if (top < chapterList.scrollTop || top + el.offsetHeight > chapterList.scrollTop + chapterList.clientHeight) {
      chapterList.scrollTo({ top: top - chapterList.clientHeight / 2 + el.offsetHeight / 2, behavior: 'smooth' });
    }
  }
}

function seekTo(t) {
  if (!isFinite(t)) return;
  const target = Math.max(0, Math.min(duration || video.duration || 0, t));
  video.currentTime = target;
  // 拖动进度条时别跟着疯狂 seek 音频，松手再对一次
  if (audioEl && !dragging) audioEl.currentTime = target;
}

function flashOSD(text) {
  osd.textContent = text;
  osd.classList.add('show');
  clearTimeout(osdTimer);
  osdTimer = setTimeout(() => osd.classList.remove('show'), 900);
}

/* ------------------------------------------------------------- 时间轴 */

function railRatio(e) {
  const r = rail.getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
}

let dragging = false;

rail.addEventListener('pointerdown', (e) => {
  dragging = true;
  rail.classList.add('dragging');
  rail.setPointerCapture(e.pointerId);
  seekTo(railRatio(e) * (duration || video.duration));
});

rail.addEventListener('pointermove', (e) => {
  if (dragging) seekTo(railRatio(e) * (duration || video.duration));
  showPeek(e);
});

rail.addEventListener('pointerleave', () => {
  if (!dragging) peek.classList.remove('show');
  itemEls.forEach((el) => el.classList.remove('hover'));
});

rail.addEventListener('pointerup', (e) => {
  dragging = false;
  rail.classList.remove('dragging');
  if (audioEl) audioEl.currentTime = video.currentTime;
  try { rail.releasePointerCapture(e.pointerId); } catch (_) {}
});

function showPeek(e) {
  if (!chapters.length || !duration) return;
  const r = rail.getBoundingClientRect();
  const appRect = app.getBoundingClientRect();
  const t = railRatio(e) * duration;
  const idx = chapterAt(t);
  const c = chapters[idx];
  if (!c) return;

  if (c.thumb) {
    peekImg.src = `${thumbBase}${c.thumb}`;
    peekImg.style.display = '';
  } else {
    peekImg.style.display = 'none';
  }
  peekTitle.textContent = c.title;
  peekTime.textContent = `${fmt(t)} / ${fmt(c.end)}`;

  itemEls.forEach((el, i) => el.classList.toggle('hover', i === idx));

  const half = 104;
  const x = Math.max(half + 8, Math.min(appRect.width - half - 8, e.clientX - appRect.left));
  peek.style.left = `${x}px`;
  peek.style.bottom = `${appRect.height - r.top + 24}px`;
  peek.classList.add('show');
}

/* ------------------------------------------------------------- 控件 */

function togglePlay() {
  if (video.paused) video.play(); else video.pause();
}

$('btnPlay').addEventListener('click', togglePlay);
video.addEventListener('click', togglePlay);
video.addEventListener('dblclick', toggleFullscreen);

video.addEventListener('play', () => {
  $('icoPlay').classList.add('hidden');
  $('icoPause').classList.remove('hidden');
});
video.addEventListener('pause', () => {
  $('icoPlay').classList.remove('hidden');
  $('icoPause').classList.add('hidden');
  app.classList.remove('idle');
});

function jumpChapter(delta) {
  if (!chapters.length) return;
  const idx = chapterAt(video.currentTime);
  let target = idx;
  if (delta < 0) {
    const atStart = video.currentTime - chapters[idx].start < 1.2;
    target = atStart ? Math.max(0, idx - 1) : idx;
  } else {
    target = Math.min(chapters.length - 1, idx + 1);
  }
  seekTo(chapters[target].start + 0.04);
}

$('btnPrev').addEventListener('click', () => jumpChapter(-1));
$('btnNext').addEventListener('click', () => jumpChapter(1));

function applySpeed(v) {
  video.playbackRate = v;
  $('btnSpeed').textContent = `${Number.isInteger(v) ? v.toFixed(1) : v}×`;
}

$('btnSpeed').addEventListener('click', () => {
  const i = SPEEDS.indexOf(video.playbackRate);
  const next = SPEEDS[(i + 1) % SPEEDS.length];
  applySpeed(next);
  flashOSD(`${next}×`);
});

function setSpeed(dir) {
  let i = SPEEDS.indexOf(video.playbackRate);
  if (i < 0) i = 2;
  const next = SPEEDS[Math.max(0, Math.min(SPEEDS.length - 1, i + dir))];
  applySpeed(next);
  flashOSD(`${next}×`);
}

$('btnMute').addEventListener('click', () => {
  if (video.volume > 0) { lastVolume = video.volume; video.volume = 0; }
  else video.volume = lastVolume || 1;
  syncVolume();
  flashOSD(video.volume === 0 ? '静音' : `音量 ${Math.round(video.volume * 100)}`);
});

function syncVolume() {
  const fill = $('volFill');
  const icon = $('icoVol');
  if (fill) fill.style.transform = `scaleX(${video.volume})`;
  if (icon) icon.style.opacity = video.volume === 0 ? '0.4' : '1';
  if (audioEl) audioEl.volume = video.volume;
}

const volTrack = $('volTrack');
function volFromEvent(e) {
  const r = volTrack.getBoundingClientRect();
  const v = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  video.volume = v;
  video.muted = false;
  syncVolume();
}
volTrack.addEventListener('pointerdown', (e) => {
  volTrack.setPointerCapture(e.pointerId);
  volFromEvent(e);
  const move = (ev) => volFromEvent(ev);
  const up = () => {
    volTrack.removeEventListener('pointermove', move);
    volTrack.removeEventListener('pointerup', up);
  };
  volTrack.addEventListener('pointermove', move);
  volTrack.addEventListener('pointerup', up);
});

$('btnDrawer').addEventListener('click', () => drawer.classList.toggle('open'));

function syncFsClass() {
  app.classList.toggle('fs', !!document.fullscreenElement || winFs);
}

document.addEventListener('fullscreenchange', syncFsClass);

function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen?.();
    return;
  }
  if (winFs) {
    if (desktop && desktop.setWindowFullscreen) desktop.setWindowFullscreen(false);
    else { winFs = false; syncFsClass(); }
    return;
  }
  const p = app.requestFullscreen?.();
  if (p && p.catch) {
    // 没有用户手势时元素全屏会被拒，退回窗口全屏
    p.catch(() => {
      if (desktop && desktop.setWindowFullscreen) desktop.setWindowFullscreen(true);
    });
  }
}
$('btnFull').addEventListener('click', toggleFullscreen);

$('fileName').addEventListener('click', () => {
  if (desktop) desktop.openFile();
});

/* ------------------------------------------------------------- 分析 */

function startAnalyze() {
  if (!desktop) return;
  if (!currentPath) {
    desktop.openFile();
    return;
  }
  btnAnalyze.hidden = false;
  btnAnalyze.disabled = true;
  btnAnalyze.textContent = '分析中';
  jobStart = Date.now();
  jobFirst = 0;
  showJob(0, '准备中…');
  desktop.analyze(currentPath);
}

btnAnalyze.addEventListener('click', startAnalyze);

/* --------------------------------------------------------- 重做标题 */

const llmModal = $('llmModal');

async function startRetitle() {
  if (!desktop || !currentPath || questionsBusy()) return;
  let cfg = { llmUrl: '', llmModel: '' };
  try {
    cfg = await desktop.getConfig();
  } catch (_) { /* 老版本没有这个接口 */ }

  if (!cfg.llmUrl || !cfg.llmModel) {
    $('llmUrl').value = cfg.llmUrl || 'http://127.0.0.1:8080/v1';
    $('llmModel').value = cfg.llmModel || '';
    $('llmLaunch').value = cfg.llmLaunch || '';
    llmModal.hidden = false;
    ($('llmModel').value ? $('llmUrl') : $('llmModel')).focus();
    return;
  }
  desktop.retitle(currentPath);
}

function questionsBusy() {
  return $('job').hidden === false;
}

btnRetitle.addEventListener('click', startRetitle);

$('llmCancel').addEventListener('click', () => { llmModal.hidden = true; });

$('llmSave').addEventListener('click', async () => {
  const llmUrl = $('llmUrl').value.trim();
  const llmModel = $('llmModel').value.trim();
  const llmLaunch = $('llmLaunch').value.trim();
  try {
    await desktop.setConfig({ llmUrl, llmModel, llmLaunch });
  } catch (_) { /* 忽略 */ }
  llmModal.hidden = true;
  if (llmUrl && llmModel && currentPath) desktop.retitle(currentPath);
});

llmModal.addEventListener('click', (e) => {
  if (e.target === llmModal) llmModal.hidden = true;
});

function fmtSec(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m} 分 ${String(s % 60).padStart(2, '0')} 秒` : `${s} 秒`;
}

function showJob(pct, label) {
  const job = $('job');
  clearTimeout(jobHideTimer);
  job.hidden = false;
  const p = Math.max(0, Math.min(100, pct || 0));
  $('jobFill').style.width = `${p}%`;
  $('jobPct').textContent = `${Math.round(p)}%`;
  if (label) $('jobStage').textContent = label;

  // Python 启动 + 导入 cv2 要两三秒，从首个真实进度开始计时，ETA 才不失真
  if (p > 0 && !jobFirst) jobFirst = Date.now();
  const base = jobFirst || jobStart;
  const elapsed = base ? (Date.now() - base) / 1000 : 0;

  if (p > 2 && p < 99.5) {
    const remain = (elapsed * (100 - p)) / p;
    $('jobEta').textContent = `已用 ${fmtSec(elapsed)} · 剩余约 ${fmtSec(remain)}`;
  } else {
    $('jobEta').textContent = `已用 ${fmtSec(elapsed)}`;
  }
}

function hideJob(delay = 900) {
  clearTimeout(jobHideTimer);
  jobHideTimer = setTimeout(() => { $('job').hidden = true; }, delay);
}

function handleAnalyze(payload) {
  if (!payload) return;
  if (payload.type === 'start') {
    jobKind = payload.kind || 'analyze';
    $('jobTitle').textContent = payload.title || '正在分析章节';
    if (jobKind === 'analyze') {
      btnAnalyze.hidden = false;
      btnAnalyze.disabled = true;
      btnAnalyze.textContent = '分析中';
    } else {
      btnRetitle.disabled = true;
      btnRetitle.textContent = '处理中';
    }
    jobStart = Date.now();
    jobFirst = 0;
    showJob(0, '准备中…');
  } else if (payload.type === 'progress') {
    showJob(payload.pct, payload.label);
    if (jobKind === 'analyze') btnAnalyze.textContent = `分析中 ${Math.round(payload.pct || 0)}%`;
  } else if (payload.type === 'log') {
    if (payload.message) $('jobEta').title = payload.message;
  } else if (payload.type === 'done') {
    btnAnalyze.disabled = false;
    btnAnalyze.textContent = '分析章节';
    btnRetitle.disabled = false;
    btnRetitle.textContent = '重做标题';
    showJob(100, '完成');
    hideJob(1100);
  } else if (payload.type === 'error') {
    btnAnalyze.disabled = false;
    btnAnalyze.textContent = '重试分析';
    btnRetitle.disabled = false;
    btnRetitle.textContent = '重做标题';
    $('job').hidden = true;
    $('loading').textContent = payload.message;
    $('loading').classList.remove('gone');
  }
}

/* ------------------------------------------------------------- 音轨转换 */

function handleAudio(payload) {
  if (!payload) return;
  if (payload.type === 'start') {
    jobKind = 'audio';
    $('jobTitle').textContent = '音轨转换中，转完自动出声';
    jobStart = Date.now();
    jobFirst = 0;
    showJob(0, '准备中…');
  } else if (payload.type === 'progress') {
    if (jobKind === 'audio') showJob(payload.pct, `转换音轨 ${Math.round(payload.pct || 0)}%`);
  } else if (payload.type === 'ready') {
    attachAudio(payload.url);
    if (jobKind === 'audio') {
      showJob(100, payload.cached ? '音轨已缓存' : '完成');
      hideJob(900);
    }
  } else if (payload.type === 'error') {
    if (jobKind === 'audio') $('job').hidden = true;
    flashOSD('音轨转换失败，只有画面');
    console.warn('[audio]', payload.message);
  }
}

/* ------------------------------------------------------------- 键盘 */

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  const k = e.key;
  const step = e.shiftKey ? 1 : 5;

  switch (k) {
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'ArrowLeft': e.preventDefault(); seekTo(video.currentTime - step); flashOSD(`−${step}s`); break;
    case 'ArrowRight': e.preventDefault(); seekTo(video.currentTime + step); flashOSD(`+${step}s`); break;
    case 'ArrowUp': e.preventDefault(); video.volume = Math.min(1, video.volume + 0.05); syncVolume(); flashOSD(`音量 ${Math.round(video.volume * 100)}`); break;
    case 'ArrowDown': e.preventDefault(); video.volume = Math.max(0, video.volume - 0.05); syncVolume(); flashOSD(`音量 ${Math.round(video.volume * 100)}`); break;
    case 'm': case 'M': $('btnMute').click(); break;
    case 'f': case 'F': toggleFullscreen(); break;
    case 'c': case 'C': drawer.classList.toggle('open'); break;
    case 'n': case 'N': jumpChapter(1); flashOSD('下一段'); break;
    case 'p': case 'P': jumpChapter(-1); flashOSD('上一段'); break;
    case 'j': case 'J': seekTo(video.currentTime - 10); flashOSD('−10s'); break;
    case 'l': case 'L': seekTo(video.currentTime + 10); flashOSD('+10s'); break;
    case 'k': case 'K': togglePlay(); break;
    case '[': e.preventDefault(); setSpeed(-1); break;
    case ']': e.preventDefault(); setSpeed(1); break;
    case 'o': case 'O':
      if (desktop && (e.ctrlKey || e.metaKey)) { e.preventDefault(); desktop.openFile(); }
      break;
    case 'Escape': drawer.classList.remove('open'); break;
    default:
      if (/^[0-9]$/.test(k)) { seekTo(duration * (Number(k) / 10)); flashOSD(`${Number(k) * 10}%`); }
  }
  wake();
});

/* ------------------------------------------------------------- 自动隐藏 */

function wake() {
  app.classList.remove('idle');
  armIdle();
}

function armIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (!video.paused && !dragging) app.classList.add('idle');
  }, 2600);
}

app.addEventListener('pointermove', wake);
app.addEventListener('pointerdown', wake);
video.addEventListener('ended', () => app.classList.remove('idle'));

syncVolume();
boot();

// 静态服务器 + 本地文件代理。既能单独跑（浏览器调试），也能被 Electron 主进程内嵌。
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.webm': 'video/webm',
  '.mkv': 'video/x-matroska',
  '.mov': 'video/quicktime',
  '.avi': 'video/x-msvideo',
  '.ts': 'video/mp2t',
  '.mp3': 'audio/mpeg',
  '.m4a': 'audio/mp4',
  '.flac': 'audio/flac',
  '.wav': 'audio/wav',
  '.srt': 'text/plain; charset=utf-8',
  '.ass': 'text/plain; charset=utf-8',
  '.vtt': 'text/vtt; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

const MEDIA_EXT = new Set([
  '.mp4', '.m4v', '.webm', '.mkv', '.mov', '.avi', '.ts',
  '.mp3', '.m4a', '.flac', '.wav',
  '.srt', '.ass', '.vtt', '.jpg', '.jpeg', '.png',
]);

function sendFile(req, res, filePath, extraHeaders = {}) {
  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('404');
      return;
    }

    const type = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
    const range = req.headers.range;
    const base = { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store', ...extraHeaders };

    if (range && /^bytes=/.test(range)) {
      const [s, e] = range.replace(/bytes=/, '').split('-');
      let start = parseInt(s, 10);
      let end = e ? parseInt(e, 10) : stat.size - 1;
      if (isNaN(start)) { start = stat.size - parseInt(e, 10); end = stat.size - 1; }
      if (start < 0) start = 0;
      if (end >= stat.size) end = stat.size - 1;
      if (start > end) {
        res.writeHead(416, { 'Content-Range': `bytes */${stat.size}` }).end();
        return;
      }
      res.writeHead(206, {
        ...base,
        'Content-Range': `bytes ${start}-${end}/${stat.size}`,
        'Content-Length': end - start + 1,
      });
      fs.createReadStream(filePath, { start, end }).pipe(res);
      return;
    }

    res.writeHead(200, { ...base, 'Content-Length': stat.size });
    fs.createReadStream(filePath).pipe(res);
  });
}

function createServer({ root = ROOT, cacheDir = null } = {}) {
  const rootAbs = path.resolve(root);

  return http.createServer((req, res) => {
    let url;
    try {
      url = new URL(req.url, 'http://127.0.0.1');
    } catch (_) {
      res.writeHead(400).end();
      return;
    }

    let pathname = decodeURIComponent(url.pathname);

    // 任意本地媒体文件：/fs?p=<绝对路径>
    if (pathname === '/fs') {
      const p = url.searchParams.get('p');
      if (!p) { res.writeHead(400).end('missing p'); return; }
      const abs = path.resolve(p);
      if (!MEDIA_EXT.has(path.extname(abs).toLowerCase())) {
        res.writeHead(403).end('forbidden type');
        return;
      }
      sendFile(req, res, abs);
      return;
    }

    // 分析结果缓存目录：/cache/<相对路径>
    if (pathname.startsWith('/cache/')) {
      if (!cacheDir) { res.writeHead(404).end(); return; }
      const cacheAbs = path.resolve(cacheDir);
      const rel = path.normalize(pathname.slice('/cache/'.length)).replace(/^([/\\])+/, '');
      const abs = path.join(cacheAbs, rel);
      if (!abs.startsWith(cacheAbs)) { res.writeHead(403).end(); return; }
      sendFile(req, res, abs);
      return;
    }

    if (pathname === '/') {
      res.writeHead(302, { Location: '/app/' }).end();
      return;
    }
    if (pathname.endsWith('/')) pathname += 'index.html';

    const abs = path.join(rootAbs, path.normalize(pathname).replace(/^([/\\])+/, ''));
    if (!abs.startsWith(rootAbs)) { res.writeHead(403).end(); return; }
    sendFile(req, res, abs);
  });
}

function startServer(opts = {}) {
  const port = opts.port === undefined ? 0 : opts.port;
  const server = createServer(opts);
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => {
      resolve({
        port: server.address().port,
        close: () => new Promise((r) => server.close(r)),
      });
    });
  });
}

module.exports = { createServer, startServer };

if (require.main === module) {
  startServer({ port: Number(process.env.PORT || 5173) }).then(({ port }) => {
    console.log(`播放器: http://127.0.0.1:${port}/`);
  });
}

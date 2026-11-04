const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('chapterApp', {
  isDesktop: true,
  openFile: () => ipcRenderer.invoke('open-file'),
  analyze: (videoPath) => ipcRenderer.send('analyze', videoPath),
  retitle: (videoPath) => ipcRenderer.send('retitle', videoPath),
  getConfig: () => ipcRenderer.invoke('get-config'),
  setConfig: (patch) => ipcRenderer.invoke('set-config', patch),
  prepareAudio: (path, duration) => ipcRenderer.send('prepare-audio', { path, duration }),
  setWindowFullscreen: (on) => ipcRenderer.send('set-window-fullscreen', !!on),
  onMedia: (cb) => ipcRenderer.on('media', (_e, payload) => cb(payload)),
  onAnalyze: (cb) => ipcRenderer.on('analyze', (_e, payload) => cb(payload)),
  onAudio: (cb) => ipcRenderer.on('audio', (_e, payload) => cb(payload)),
  onWindowFullscreen: (cb) => ipcRenderer.on('window-fullscreen', (_e, on) => cb(on)),
  onDesktopInfo: (cb) => ipcRenderer.on('desktop-info', (_e, payload) => cb(payload)),
  onRequestAnalyze: (cb) => ipcRenderer.on('request-analyze', () => cb()),
  onRequestRetitle: (cb) => ipcRenderer.on('request-retitle', () => cb()),
});

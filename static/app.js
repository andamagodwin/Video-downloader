'use strict';

const ACTIVE = new Set(['queued', 'starting', 'downloading', 'processing']);
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent);
const $ = (sel) => document.querySelector(sel);

const els = {
  form: $('#url-form'),
  url: $('#url'),
  fetchBtn: $('#fetch-btn'),
  msg: $('#form-msg'),
  preview: $('#preview'),
  thumb: $('#pv-thumb'),
  duration: $('#pv-duration'),
  title: $('#pv-title'),
  meta: $('#pv-meta'),
  video: $('#pv-video'),
  audio: $('#pv-audio'),
  audioGroup: $('#audio-group'),
  note: $('#pv-note'),
  dlBtn: $('#dl-btn'),
  jobs: $('#jobs'),
  empty: $('#empty'),
  dir: $('#dir'),
  clear: $('#clear-btn'),
  openFolder: $('#open-folder'),
  jobTpl: $('#job-tpl'),
  settingsBtn: $('#settings-btn'),
  settings: $('#settings'),
  cookies: $('#cookies-browser'),
  settingsSaved: $('#settings-saved'),
  settingsHint: $('#settings-hint'),
};

let config = { ffmpeg: true, download_dir: '' };
let current = null;      // info for the link being previewed
let lookupSeq = 0;       // ignore stale lookups if the user pastes again
let pollTimer = null;
const jobEls = new Map();

// ------------------------------------------------------------------ helpers

async function api(path, { method = 'GET', data } = {}) {
  const res = await fetch(path, {
    method,
    headers: { 'X-Requested-With': 'fetch', ...(data ? { 'Content-Type': 'application/json' } : {}) },
    body: data ? JSON.stringify(data) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    const err = new Error((payload && payload.error) || `Request failed (${res.status})`);
    err.needsLogin = Boolean(payload && payload.needs_login);
    throw err;
  }
  return payload;
}

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};

function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 || n >= 100 ? 0 : 1)} ${units[i]}`;
}

function fmtDuration(sec) {
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? h + ':' : ''}${mm}:${String(s).padStart(2, '0')}`;
}

function fmtEta(sec) {
  if (sec < 60) return `${Math.max(1, Math.round(sec))}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

function looksLikeUrl(text) {
  return /^https?:\/\/\S+\.\S+/i.test(text.trim());
}

function showMsg(text, kind = '', action = null) {
  els.msg.textContent = text;
  els.msg.className = `form-msg ${kind}`;
  if (action) els.msg.append(button(action.label, action.onClick));
}

const loginAction = () => ({ label: 'Open settings', onClick: openSettingsForLogin });

function button(label, onClick, cls = 'btn ghost small') {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = cls;
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

// ------------------------------------------------------------------ lookup

async function lookup() {
  const url = els.url.value.trim();
  if (!url) { els.url.focus(); return; }
  if (!looksLikeUrl(url)) { showMsg('That doesn’t look like a link. Paste the full address, starting with https://', 'error'); return; }

  const seq = ++lookupSeq;
  current = null;
  showMsg('');
  els.fetchBtn.classList.add('busy');
  els.fetchBtn.disabled = true;
  showLoading();

  try {
    const info = await api('/api/info', { method: 'POST', data: { url } });
    if (seq !== lookupSeq) return;
    current = info;
    renderPreview(info);
  } catch (err) {
    if (seq !== lookupSeq) return;
    els.preview.hidden = true;
    showMsg(err.message, 'error', err.needsLogin ? loginAction() : null);
  } finally {
    if (seq === lookupSeq) {
      els.fetchBtn.classList.remove('busy');
      els.fetchBtn.disabled = false;
    }
  }
}

function showLoading() {
  els.preview.hidden = false;
  els.preview.classList.add('loading');
  els.thumb.hidden = true;
  els.duration.hidden = true;
  els.title.textContent = 'Looking up the video… this can take a few seconds';
  els.meta.textContent = '';
  els.note.hidden = true;
}

function heightOption(h) {
  const badge = h >= 2160 ? '4K' : h >= 1440 ? '2K' : h >= 720 ? 'HD' : '';
  return { value: String(h), label: `${h}p`, badge };
}

function pill({ value, label, badge }) {
  const wrap = document.createElement('label');
  wrap.className = 'pill';
  const input = document.createElement('input');
  input.type = 'radio';
  input.name = 'quality';
  input.value = value;
  const span = document.createElement('span');
  span.textContent = label;
  if (badge) {
    const em = document.createElement('em');
    em.textContent = badge;
    span.append(em);
  }
  wrap.append(input, span);
  return wrap;
}

function renderPreview(info) {
  els.preview.classList.remove('loading');
  els.preview.hidden = false;

  els.thumb.hidden = !info.thumbnail;
  if (info.thumbnail) els.thumb.src = info.thumbnail;
  els.duration.hidden = !info.duration;
  els.duration.textContent = info.duration ? fmtDuration(info.duration) : '';
  els.title.textContent = info.title;

  const isInstagram = info.site === 'Instagram';
  const groupName = !isInstagram ? 'Playlist' : /\/stories\//.test(info.url) ? 'Stories' : 'Post';
  const meta = [];
  if (info.site && info.site !== 'YouTube') meta.push(info.site);
  if (info.type === 'playlist') meta.push(`${groupName} · ${info.count ?? '?'} videos`);
  if (info.uploader && info.uploader !== info.site) meta.push(info.uploader);
  if (info.views) meta.push(`${compact.format(info.views)} views`);
  els.meta.textContent = meta.join(' · ');

  const heights = info.heights || [];
  const videoOpts = heights.map(heightOption);
  if (info.type === 'playlist' || !videoOpts.length) videoOpts.unshift({ value: 'best', label: 'Best' });
  els.video.replaceChildren(...videoOpts.map(pill));
  els.audio.replaceChildren(...[{ value: 'mp3', label: 'MP3' }, { value: 'm4a', label: 'M4A' }].map(pill));
  els.audioGroup.hidden = !config.ffmpeg;

  selectDefault(info, heights);

  let note = '';
  if (info.is_live) note = 'This is a live stream. It can be downloaded once the broadcast has ended.';
  else if (info.type === 'playlist') {
    note = isInstagram
      ? `All ${info.count ?? ''} videos are saved together in one folder. If a resolution isn’t available, the closest one is used.`
      : 'Every video in the playlist is saved into its own folder. If a resolution isn’t available, the closest one is used.';
  }
  else if (!config.ffmpeg) note = 'ffmpeg isn’t installed, so only lower-quality single-file video is available. Install it with: brew install ffmpeg';
  els.note.textContent = note;
  els.note.hidden = !note;
  els.dlBtn.disabled = Boolean(info.is_live);
  els.dlBtn.querySelector('span').textContent = info.type !== 'playlist' ? 'Download'
    : info.count ? `Download ${info.count} videos` : 'Download playlist';
}

// Pre-select what the user picked last time (or 1080p, which plays everywhere and stays a reasonable size).
function selectDefault(info, heights) {
  const pref = store.get('quality') || '1080';
  const radios = [...document.querySelectorAll('input[name="quality"]')];
  const pick = (value) => radios.find((r) => r.value === value);

  let choice = null;
  if ((pref === 'mp3' || pref === 'm4a') && config.ffmpeg) choice = pick(pref);
  else if (pref === 'best') choice = pick('best') || pick(String(heights[0]));
  else {
    const ceiling = Number(pref) || 1080;
    const h = heights.find((x) => x <= ceiling) ?? heights[heights.length - 1];
    choice = h != null ? pick(String(h)) : null;
  }
  (choice || radios[0]).checked = true;
}

// ------------------------------------------------------------------ downloads

async function startDownload() {
  if (!current) return;
  const selected = document.querySelector('input[name="quality"]:checked');
  if (!selected) return;
  store.set('quality', selected.value);

  els.dlBtn.disabled = true;
  try {
    await api('/api/downloads', {
      method: 'POST',
      data: {
        url: current.url,
        quality: selected.value,
        playlist: current.type === 'playlist',
        title: current.title,
        thumbnail: current.thumbnail,
        count: current.count,
      },
    });
    current = null;
    els.preview.hidden = true;
    els.url.value = '';
    showMsg('');
    els.url.focus();
    refreshJobs();
  } catch (err) {
    showMsg(err.message, 'error');
  } finally {
    els.dlBtn.disabled = false;
  }
}

async function retry(job) {
  try {
    await api('/api/downloads', {
      method: 'POST',
      data: {
        url: job.url, quality: job.quality, playlist: job.playlist,
        title: job.title, thumbnail: job.thumbnail, count: job.count,
      },
    });
    refreshJobs();
  } catch (err) {
    showMsg(err.message, 'error');
  }
}

async function jobAction(path) {
  try {
    await api(path, { method: 'POST' });
  } catch (err) {
    showMsg(err.message, 'error');
  }
  refreshJobs();
}

function overallPercent(job) {
  if (job.status === 'done') return 100;
  if (job.percent == null) return 0;
  if (job.count && job.index) return ((job.index - 1) + job.percent / 100) / job.count * 100;
  return job.percent;
}

function statusText(job) {
  switch (job.status) {
    case 'queued': return 'Waiting for a free slot…';
    case 'starting': return 'Preparing…';
    case 'downloading': {
      const bits = [];
      if (job.count && job.index) bits.push(`Video ${job.index} of ${job.count}`);
      if (job.part === 'video') bits.push('Video track');
      if (job.part === 'audio') bits.push('Audio track');
      if (job.percent != null) bits.push(`${Math.floor(job.percent)}%`);
      if (job.total) bits.push(`${fmtBytes(job.downloaded)} of ${fmtBytes(job.total)}`);
      if (job.speed) bits.push(`${fmtBytes(job.speed)}/s`);
      if (job.eta > 0) bits.push(`${fmtEta(job.eta)} left`);
      return bits.join(' · ') || 'Downloading…';
    }
    case 'processing': return `${job.step || 'Processing'}…`;
    case 'done': {
      const n = job.files ? job.files.length : 1;
      return `${n > 1 ? `Saved ${n} files` : 'Saved'} · ${fmtBytes(job.size)}`;
    }
    case 'error': return job.error || 'Download failed';
    case 'cancelled': return 'Cancelled';
    default: return '';
  }
}

function createJobEl(job) {
  const el = els.jobTpl.content.firstElementChild.cloneNode(true);
  const img = el.querySelector('img');
  if (job.thumbnail) img.src = job.thumbnail; else img.remove();
  el.querySelector('.job-title').textContent = job.title;
  el.querySelector('.job-title').title = job.title;
  el.querySelector('.tag').textContent = job.playlist
    ? `${job.quality_label} · ${job.count ? `${job.count} videos` : 'playlist'}`
    : job.quality_label;
  return el;
}

function updateJobEl(el, job) {
  const bar = el.querySelector('.bar');
  const waiting = job.status === 'queued' || job.status === 'starting' || job.status === 'processing'
    || (job.status === 'downloading' && job.percent == null);
  bar.classList.toggle('indeterminate', waiting);
  el.querySelector('.fill').style.width = waiting ? '' : `${overallPercent(job)}%`;

  const status = el.querySelector('.job-status');
  status.textContent = statusText(job);
  status.title = job.status === 'done' && job.files ? job.files.join('\n') : '';

  // Only rebuild the buttons when the state changes, so they don't flicker or lose focus.
  const actionKey = job.status === 'processing' ? 'processing' : ACTIVE.has(job.status) ? 'active' : job.status;
  if (el.dataset.actions !== actionKey) {
    el.dataset.actions = actionKey;
    const actions = el.querySelector('.job-actions');
    actions.replaceChildren();
    if (actionKey === 'active') actions.append(button('Cancel', () => jobAction(`/api/downloads/${job.id}/cancel`)));
    if (actionKey === 'done') actions.append(button(IS_MAC ? 'Show in Finder' : 'Show in folder', () => jobAction(`/api/downloads/${job.id}/reveal`)));
    if (actionKey === 'error' && job.needs_login) actions.append(button('Settings', openSettingsForLogin));
    if (actionKey === 'error' || actionKey === 'cancelled') actions.append(button('Try again', () => retry(job)));
  }
  el.dataset.status = job.status;
}

function renderJobs(list) {
  const ids = new Set(list.map((j) => j.id));
  for (const [id, el] of jobEls) {
    if (!ids.has(id)) { el.remove(); jobEls.delete(id); }
  }
  list.forEach((job, i) => {
    let el = jobEls.get(job.id);
    if (!el) { el = createJobEl(job); jobEls.set(job.id, el); }
    if (els.jobs.children[i] !== el) els.jobs.insertBefore(el, els.jobs.children[i] || null);
    updateJobEl(el, job);
  });
  els.empty.hidden = list.length > 0;
  els.clear.hidden = !list.some((j) => !ACTIVE.has(j.status));

  const active = list.find((j) => j.status === 'downloading') || list.find((j) => ACTIVE.has(j.status));
  document.title = active
    ? `${active.status === 'downloading' && active.percent != null ? Math.floor(overallPercent(active)) + '%' : '…'} · Video Downloader`
    : 'Video Downloader';
}

async function refreshJobs() {
  clearTimeout(pollTimer);
  let active = false;
  try {
    const list = await api('/api/downloads');
    renderJobs(list);
    active = list.some((j) => ACTIVE.has(j.status));
  } catch {
    active = true; // server hiccup: keep trying
  }
  if (active) pollTimer = setTimeout(refreshJobs, 800);
}

// ------------------------------------------------------------------ settings

const CHROMIUM_KEYCHAIN = {
  chrome: 'Chrome', edge: 'Microsoft Edge', brave: 'Brave', opera: 'Opera',
  vivaldi: 'Vivaldi', chromium: 'Chromium', whale: 'Whale',
};

function browserHint(id) {
  if (!id || config.platform !== 'darwin') return '';
  if (id === 'safari') {
    return 'macOS protects Safari’s data. If downloads still say you need to log in, give your Terminal app '
      + 'Full Disk Access (System Settings → Privacy & Security → Full Disk Access) and restart this app.';
  }
  if (CHROMIUM_KEYCHAIN[id]) {
    return `The first time it’s used, macOS asks for permission to use “${CHROMIUM_KEYCHAIN[id]} Safe Storage”. `
      + 'Enter your Mac password and choose Always Allow.';
  }
  return '';
}

function renderSettings() {
  const choices = [{ id: '', name: 'Off' }, ...(config.browsers || [])];
  els.cookies.replaceChildren(...choices.map((b) => {
    const option = document.createElement('option');
    option.value = b.id;
    option.textContent = b.name;
    return option;
  }));
  els.cookies.value = config.cookies_browser || '';
  updateHint();
}

function updateHint() {
  const hint = browserHint(els.cookies.value);
  els.settingsHint.textContent = hint;
  els.settingsHint.hidden = !hint;
}

function toggleSettings(open = els.settings.hidden) {
  els.settings.hidden = !open;
  els.settingsBtn.setAttribute('aria-expanded', String(open));
}

function openSettingsForLogin() {
  toggleSettings(true);
  els.settings.classList.remove('flash');
  void els.settings.offsetWidth; // restart the highlight animation
  els.settings.classList.add('flash');
  setTimeout(() => els.settings.classList.remove('flash'), 1600);
  els.settings.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  els.cookies.focus();
}

els.settingsBtn.addEventListener('click', () => toggleSettings());

els.cookies.addEventListener('change', async () => {
  els.settingsSaved.textContent = '';
  updateHint();
  try {
    const res = await api('/api/settings', { method: 'POST', data: { cookies_browser: els.cookies.value || null } });
    config.cookies_browser = res.cookies_browser;
    els.settingsSaved.textContent = 'Saved';
    setTimeout(() => { els.settingsSaved.textContent = ''; }, 2000);
  } catch (err) {
    els.cookies.value = config.cookies_browser || '';
    updateHint();
    showMsg(err.message, 'error');
  }
});

// ------------------------------------------------------------------ events

els.form.addEventListener('submit', (e) => { e.preventDefault(); lookup(); });

els.url.addEventListener('paste', () => {
  setTimeout(() => { if (looksLikeUrl(els.url.value)) lookup(); }, 0);
});

els.dlBtn.addEventListener('click', startDownload);

els.clear.addEventListener('click', async () => {
  try { await api('/api/downloads', { method: 'DELETE' }); } catch (err) { showMsg(err.message, 'error'); }
  refreshJobs();
});

els.openFolder.addEventListener('click', () => api('/api/open-folder', { method: 'POST' }).catch((err) => showMsg(err.message, 'error')));

document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshJobs(); });

// ------------------------------------------------------------------ start

(async function init() {
  try {
    config = await api('/api/config');
    els.dir.textContent = config.download_dir;
  } catch { /* keep defaults */ }
  renderSettings();
  refreshJobs();

  // Support links like http://127.0.0.1:8765/?url=https://youtube.com/watch?v=… (e.g. from a bookmarklet)
  const shared = new URLSearchParams(location.search).get('url');
  if (shared) {
    els.url.value = shared;
    history.replaceState(null, '', '/');
    lookup();
  } else {
    els.url.focus();
  }
})();

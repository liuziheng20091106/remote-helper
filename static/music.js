'use strict';

let musicGeneration = 0;
let musicTicket = null;
let musicPollTimer = 0;
let musicConfirmBusy = false;
let musicConfirmController = null;
let musicRemaining = Infinity;
let musicSearchGeneration = 0;
let musicQuery = '';
let musicOffset = 0;
let musicTotal = 0;
const musicLimit = 20;

function musicError(message, dialog = false) {
  const target = $(dialog ? 'music-confirm-error' : 'music-error');
  target.textContent = message || '';
  target.hidden = !message;
}

function musicTime(milliseconds) {
  const seconds = Math.max(0, Math.floor(Number(milliseconds || 0) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function renderMusicState(state) {
  const account = state.account;
  $('music-account').textContent = state.credentials_loaded ? (account?.nickname ? `网易云 · ${account.nickname}` : `网易云凭据已加载（${state.cookie_count || 0} 项）`) : '尚未导入网易云账号';
  const player = state.player || {};
  const song = player.song;
  const title = song ? `${song.title} · ${song.artists}` : '';
  $('music-player').textContent = player.status === 'playing' ? `播放中 · ${title} · ${musicTime(player.position_ms)} / ${musicTime(player.duration_ms || song?.duration_ms)}` : player.status === 'error' ? `播放失败${title ? ` · ${title}` : ''}` : `已停止${title ? ` · ${title}` : ''}`;
  musicError(player.error || '');
}

async function loadMusicState() {
  const generation = musicGeneration;
  try {
    const state = await api('/api/music/state');
    if (generation === musicGeneration && authenticated) renderMusicState(state);
  } catch (error) {
    if (generation === musicGeneration) musicError(error.message);
  }
}

function cancelMusicTicket(ticket) {
  if (!ticket) return;
  void fetch('/api/music/cancel', {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', keepalive: true,
    headers: {'Content-Type': 'application/json', 'X-Remote-Helper': '1'},
    body: JSON.stringify({ticket})
  }).catch(() => {});
}

function resetMusic() {
  musicConfirmController?.abort();
  musicConfirmController = null;
  musicGeneration++;
  clearTimeout(musicPollTimer);
  musicPollTimer = 0;
  const ticket = musicTicket;
  musicTicket = null;
  musicRemaining = Infinity;
  musicConfirmBusy = false;
  $('music-confirm').disabled = true;
  $('music-confirm').removeAttribute('aria-busy');
  if ($('music-dialog').open) $('music-dialog').close();
  cancelMusicTicket(ticket);
}

function renderMusicConfirmation(data) {
  musicRemaining = Math.max(0, Number(data.remaining_seconds));
  if (!Number.isFinite(musicRemaining)) musicRemaining = Infinity;
  $('music-confirm-song').textContent = `${data.song.title} · ${data.song.artists}`;
  $('music-confirm-clock').textContent = `${data.local_time} · ${data.timezone} · 等待 ${data.wait_seconds} 秒`;
  $('music-countdown').textContent = musicRemaining > 0 ? `还需等待 ${Math.ceil(musicRemaining)} 秒` : '等待已结束，请点击确认播放。';
  $('music-confirm').disabled = musicConfirmBusy || musicRemaining > 0;
}

function musicDialogCurrent(generation, ticket) {
  return generation === musicGeneration && ticket === musicTicket && authenticated && !document.hidden && activeTab === 'music' && $('music-dialog').open;
}

async function pollMusicConfirmation(generation, ticket) {
  if (!musicDialogCurrent(generation, ticket)) return;
  try {
    const data = await api(`/api/music/confirmation/${encodeURIComponent(ticket)}`);
    if (!musicDialogCurrent(generation, ticket)) return;
    renderMusicConfirmation(data);
    musicError('', true);
  } catch (error) {
    if (!musicDialogCurrent(generation, ticket)) return;
    musicRemaining = Infinity;
    $('music-confirm').disabled = true;
    musicError(error.message, true);
  }
  if (musicDialogCurrent(generation, ticket)) musicPollTimer = window.setTimeout(() => void pollMusicConfirmation(generation, ticket), 1000);
}

async function prepareMusic(songId) {
  if (!authenticated || document.hidden || activeTab !== 'music') return;
  resetMusic();
  const generation = musicGeneration;
  $('music-confirm-song').textContent = '正在准备歌曲…';
  $('music-confirm-clock').textContent = '';
  $('music-countdown').textContent = '等待服务器确认…';
  musicError('', true);
  $('music-dialog').showModal();
  try {
    const data = await api('/api/music/prepare', {song_id: songId});
    if (generation !== musicGeneration || !$('music-dialog').open || document.hidden || !authenticated) {
      cancelMusicTicket(data.ticket);
      return;
    }
    musicTicket = data.ticket;
    renderMusicConfirmation(data);
    musicPollTimer = window.setTimeout(() => void pollMusicConfirmation(generation, data.ticket), 1000);
  } catch (error) {
    if (generation === musicGeneration && $('music-dialog').open) {
      musicError(error.message, true);
      $('music-countdown').textContent = '无法准备歌曲';
    }
  }
}

function renderMusicResults(data) {
  musicOffset = data.offset;
  musicTotal = data.total;
  $('music-results-meta').textContent = `共 ${musicTotal} 首${data.songs.length ? ` · ${musicOffset + 1}–${musicOffset + data.songs.length}` : ''}`;
  $('music-results').replaceChildren(...data.songs.map(song => {
    const row = element('div', 'music-song');
    const info = element('div', 'music-song-info');
    info.append(element('strong', '', song.title), element('span', 'muted', `${song.artists} · ${song.album}`), element('small', 'muted', `#${song.id} · ${musicTime(song.duration_ms)}`));
    const button = element('button', 'secondary', '点歌');
    button.type = 'button';
    button.setAttribute('aria-label', `点歌：${song.title}，${song.artists}`);
    button.addEventListener('click', () => void action(button, () => prepareMusic(song.id)));
    row.append(info, button);
    return row;
  }));
  if (!data.songs.length) $('music-results').append(element('p', 'empty', '没有找到歌曲'));
  updateMusicPagination(false);
}

function updateMusicPagination(busy) {
  $('music-previous').disabled = busy || musicOffset <= 0;
  $('music-next').disabled = busy || musicOffset + musicLimit >= musicTotal;
  $('music-page').textContent = musicTotal ? `${Math.floor(musicOffset / musicLimit) + 1} / ${Math.ceil(musicTotal / musicLimit)}` : '—';
}

async function searchMusic(offset = 0) {
  const query = $('music-query').value.trim();
  if (!query) return;
  const generation = ++musicSearchGeneration;
  musicError('');
  updateMusicPagination(true);
  $('music-results-meta').textContent = '搜索中…';
  try {
    const params = new URLSearchParams({q: query, offset: String(offset), limit: String(musicLimit)});
    const data = await api(`/api/music/search?${params}`);
    if (generation !== musicSearchGeneration || !authenticated) return;
    musicQuery = query;
    renderMusicResults(data);
  } catch (error) {
    if (generation === musicSearchGeneration) {
      musicError(error.message);
      $('music-results-meta').textContent = '搜索失败';
      updateMusicPagination(false);
    }
  }
}

$('music-search-form').addEventListener('submit', event => {
  event.preventDefault();
  void action(event.currentTarget.querySelector('[type="submit"]'), () => searchMusic());
});
for (const [id, delta] of [['music-previous', -musicLimit], ['music-next', musicLimit]]) {
  $(id).addEventListener('click', () => {
    if ($(id).disabled) return;
    $('music-query').value = musicQuery;
    void searchMusic(Math.max(0, musicOffset + delta));
  });
}
onClick('music-refresh', loadMusicState);
onClick('music-import', async () => {
  const generation = musicGeneration;
  try {
    const state = await api('/api/music/import', {});
    if (generation === musicGeneration && authenticated) renderMusicState(state);
  } catch (error) { if (generation === musicGeneration) musicError(error.message); }
});
onClick('music-stop', async () => {
  resetMusic();
  const generation = musicGeneration;
  try {
    const state = await api('/api/music/stop', {});
    if (generation === musicGeneration && authenticated) renderMusicState(state);
  } catch (error) { if (generation === musicGeneration) musicError(error.message); }
});
$('music-cancel').addEventListener('click', () => resetMusic());
$('music-dialog').addEventListener('cancel', event => { event.preventDefault(); resetMusic(); });
$('music-dialog').addEventListener('close', () => { if (!$('music-dialog').open && musicTicket) resetMusic(); });
$('music-confirm').addEventListener('click', async () => {
  if (musicConfirmBusy || musicRemaining > 0 || !musicTicket) return;
  const ticket = musicTicket;
  const generation = musicGeneration;
  if (!musicDialogCurrent(generation, ticket)) return;
  musicConfirmBusy = true;
  $('music-confirm').disabled = true;
  $('music-confirm').setAttribute('aria-busy', 'true');
  musicError('', true);
  const controller = new AbortController();
  musicConfirmController = controller;
  try {
    const response = await checkedResponse(await fetch('/api/music/confirm', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Remote-Helper': '1'},
      body: JSON.stringify({ticket}), signal: controller.signal
    }));
    const state = await response.json();
    if (generation !== musicGeneration || !authenticated) return;
    musicTicket = null;
    resetMusic();
    renderMusicState(state);
  } catch (error) {
    if (musicDialogCurrent(generation, ticket)) {
      musicRemaining = Infinity;
      musicError(error.message, true);
    }
  } finally {
    if (musicConfirmController === controller) musicConfirmController = null;
    if (generation === musicGeneration) musicConfirmBusy = false;
    $('music-confirm').removeAttribute('aria-busy');
  }
});
document.addEventListener('visibilitychange', () => { if (document.hidden) resetMusic(); });
window.addEventListener('pagehide', () => resetMusic());

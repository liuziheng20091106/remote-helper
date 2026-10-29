'use strict';

// The server consumes the entry password; never retain it in browser history.
const entryURL = new URL(window.location.href);
if (entryURL.searchParams.has('p')) {
  entryURL.searchParams.delete('p');
  history.replaceState(null, '', entryURL.pathname + entryURL.search + entryURL.hash);
}

const $ = id => document.getElementById(id);
let activeTab = 'home';
let settings = {desktop_zoom: 1, desktop_interval: 2, desktop_quality: 65, desktop_width: 1280};
let commands = [];
let processes = [];
let processesLoaded = false;
let processReadTime = '';
let volumeState = null;
let authenticated = false;
let toolsLoaded = false;
let desktopLoaded = false;
let logsLoaded = false;
let commandRunning = false;
let confirmPending = false;
let live = false;
let liveTimer;
let snapshotPromise = null;
let snapshotController = null;
let snapshotGeneration = 0;
let frameURL = null;
let frameGeometry = null;
let availableKeys = [];
let pointerStart = null;
let pointerMoved = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function notify(message) {
  $('notice-text').textContent = message;
  $('notice').classList.add('error');
  $('notice').hidden = false;
}

function showAuth() {
  authenticated = false;
  stopLive(true);
  if (typeof stopTouchpad === 'function') stopTouchpad();
  if (typeof resetMusic === 'function') resetMusic();
  if (typeof resetPpt === 'function') resetPpt('需要登录后刷新 PowerPoint。');
  if (typeof resetIcc === 'function') resetIcc();
  if (frameURL) { URL.revokeObjectURL(frameURL); frameURL = null; }
  frameGeometry = null;
  $('desktop-image').hidden = true;
  $('desktop-image').removeAttribute('src');
  $('desktop-empty').hidden = false;
  setConnection('需要登录', 'offline');
  for (const dialog of document.querySelectorAll('dialog[open]')) {
    if (dialog.id !== 'auth-dialog') dialog.close('cancel');
  }
  if (!$('auth-dialog').open) $('auth-dialog').showModal();
}

function setConnection(text, status) {
  $('connection').replaceChildren(element('i'), document.createTextNode(text));
  $('connection').className = `connection ${status}`;
}

async function checkedResponse(response) {
  if (response.ok) return response;
  let detail = `请求失败（${response.status}）`;
  try {
    const data = await response.json();
    if (typeof data.detail === 'string') detail = data.detail;
  } catch (_) { /* A non-JSON proxy response still gets a readable HTTP error. */ }
  if (response.status === 401) showAuth();
  throw new Error(detail);
}

async function api(path, body) {
  const options = {credentials: 'same-origin', cache: 'no-store'};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-Remote-Helper': '1'};
    options.body = JSON.stringify(body);
  }
  const response = await checkedResponse(await fetch(path, options));
  return response.json();
}

async function action(button, work) {
  if (button?.disabled) return;
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try {
    const result = await work();
    return result;
  } catch (error) {
    if (error.name !== 'AbortError') notify(error.message || '无法连接电脑，请检查网络。');
  } finally {
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
  }
}

function onClick(id, work) {
  $(id).addEventListener('click', event => { void action(event.currentTarget, work); });
}

function onSubmit(id, work) {
  $(id).addEventListener('submit', event => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    void action(event.currentTarget.querySelector('button[type="submit"]'), work);
  });
}

function ask(title, message, buttonText = '确认') {
  if (confirmPending) return Promise.resolve(false);
  confirmPending = true;
  $('confirm-title').textContent = title;
  $('confirm-message').textContent = message;
  $('confirm-action').textContent = buttonText;
  const dialog = $('confirm-dialog');
  dialog.returnValue = 'cancel';
  return new Promise(resolve => {
    dialog.addEventListener('close', () => {
      confirmPending = false;
      resolve(dialog.returnValue === 'confirm');
    }, {once: true});
    dialog.showModal();
  });
}

function applySettings(next) {
  settings = {...settings, ...next};
  $('desktop-zoom').value = settings.desktop_zoom;
  $('zoom-value').textContent = `${settings.desktop_zoom}×`;
  sizeDesktopImage();
  $('setting-interval').value = settings.desktop_interval;
  $('setting-quality').value = settings.desktop_quality;
  $('setting-width').value = settings.desktop_width;
}

function applyRelay(relay) {
  $('relay-enabled').checked = relay.enabled;
  $('relay-url').value = relay.url;
  $('relay-status').textContent = `${relay.connected ? '已连接' : relay.enabled ? '未连接' : '已关闭'} · ${relay.status} · ${relay.token_configured ? '令牌已配置' : '未配置令牌'}`;
  const url = new URL(relay.url);
  url.pathname = '/r';
  url.search = '';
  url.hash = '';
  $('relay-link').href = url.href;
  $('relay-link').textContent = url.href;
}

async function loadState() {
  const state = await api('/api/state');
  authenticated = true;
  if ($('auth-dialog').open) $('auth-dialog').close();
  setConnection(state.paused ? '控制已暂停' : '电脑已连接', state.paused ? '' : 'online');
  $('hostname').textContent = state.hostname;
  $('platform').textContent = state.platform;
  $('host-details').replaceChildren(
    element('span', '', state.admin ? '管理员权限' : '普通权限'),
    element('span', '', `端口 ${state.port}`),
    element('span', '', state.paused ? '已暂停' : '就绪')
  );
  $('session-state').textContent = `${state.hostname} · ${state.admin ? '管理员权限' : '普通权限'} · ${state.paused ? '已暂停，请在电脑端恢复' : '连接正常'}`;
  applySettings(state.settings);
  applyRelay(state.relay);
  commands = state.commands;
  renderCommands();
  if (Array.isArray(state.favorites)) {
    for (const process of processes) process.favorite = state.favorites.includes(process.favorite_key);
    if (processesLoaded) renderProcesses();
  }
}

function selectTab(tab) {
  if (!document.querySelector(`[data-tab="${tab}"]`)) return;
  if (activeTab === 'desktop' && tab !== 'desktop') stopLive(true);
  if (activeTab !== tab && typeof stopTouchpad === 'function') stopTouchpad();
  if (activeTab !== tab && typeof resetMusic === 'function') resetMusic();
  activeTab = tab;
  for (const panel of document.querySelectorAll('.panel')) {
    const selected = panel.id === `panel-${tab}`;
    panel.hidden = !selected;
    panel.classList.toggle('active', selected);
  }
  $('main').scrollTop = 0;
  for (const button of document.querySelectorAll('[data-tab]')) {
    const selected = button.dataset.tab === tab;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  document.querySelector(`[data-tab="${tab}"]`).scrollIntoView({block: 'nearest', inline: 'nearest'});
  $('main').focus({preventScroll: true});
  window.scrollTo({top: 0, behavior: 'instant'});
  if (!authenticated) return;
  if (tab === 'commands') void action(null, loadCommands);
  if (tab === 'tools' && !toolsLoaded) void action($('tools-refresh'), loadTools);
  if (tab === 'desktop') {
    if (!desktopLoaded) void action($('snapshot'), loadDesktopInfo);
    else requestAnimationFrame(sizeDesktopImage);
  }
  if (tab === 'ppt') void action(null, refreshPpt);
  if (tab === 'music') void action(null, loadMusicState);
  if (tab === 'classisland') void action(null, refreshClassIsland);
  if (tab === 'icc') void action(null, refreshIcc);
  if (tab === 'settings' && !logsLoaded) void action($('logs-refresh'), loadLogs);
  // Process enumeration is deliberately tied only to the Refresh button.
}

function renderVolume(data) {
  volumeState = data;
  $('volume').value = Math.round(data.level);
  $('volume-value').textContent = `${Math.round(data.level)}%`;
  $('mute').setAttribute('aria-pressed', String(data.muted));
  $('mute').textContent = data.muted ? '取消静音' : '静音';
  $('volume').disabled = false;
  $('mute').disabled = false;
}

async function loadVolume() { renderVolume(await api('/api/volume')); }

async function changeVolume(body) {
  $('volume').disabled = true;
  $('mute').disabled = true;
  try { renderVolume(await api('/api/volume', body)); }
  catch (error) {
    if (volumeState) renderVolume(volumeState);
    throw error;
  }
  finally {
    $('volume').disabled = !volumeState;
    $('mute').disabled = !volumeState;
  }
}

async function loadCommands() {
  commands = await api('/api/commands');
  renderCommands();
}

function openCommand(command) {
  $('command-dialog-title').textContent = command ? '编辑快捷命令' : '新建快捷命令';
  $('command-id').value = command?.id || '';
  $('command-name').value = command?.name || '';
  $('command-text').value = command?.command || '';
  $('command-shell').value = command?.shell || 'cmd';
  $('command-timeout').value = command?.timeout || 60;
  $('command-dialog').showModal();
}

function commandButton(text, className, handler) {
  const button = element('button', className, text);
  button.type = 'button';
  button.addEventListener('click', () => { void action(button, handler); });
  return button;
}

function renderCommands() {
  const list = $('command-list');
  list.replaceChildren();
  if (!commands.length) { list.append(element('p', 'empty', '暂无快捷命令')); return; }
  for (const command of commands) {
    const row = element('div', 'list-item');
    const content = element('div', 'item-content');
    content.append(element('p', 'item-title', command.name), element('p', 'item-description', `${command.shell.toUpperCase()} · ${command.timeout} 秒\n${command.command}`));
    const actions = element('div', 'item-actions');
    const runButton = commandButton('运行', 'primary command-run', async () => {
      await runCommand(`/api/commands/${encodeURIComponent(command.id)}/run`, {});
    });
    runButton.disabled = commandRunning;
    actions.append(runButton, commandButton('编辑', 'secondary', async () => { openCommand(command); }), commandButton('删除', 'danger', async () => {
      if (!await ask('删除快捷命令', `删除「${command.name}」？此操作仅移除保存的指令。`, '删除')) return;
      await api(`/api/commands/${encodeURIComponent(command.id)}/delete`, {});
      commands = commands.filter(item => item.id !== command.id);
      renderCommands();
    }));
    row.append(content, actions);
    list.append(row);
  }
}

async function runCommand(path, body) {
  if (commandRunning) throw new Error('上一条命令仍在执行，请等待完成。');
  commandRunning = true;
  $('command-result-meta').textContent = '命令执行中，请等待…';
  $('command-output').textContent = '';
  for (const button of document.querySelectorAll('.command-run, #shell-form button[type=submit]')) button.disabled = true;
  try {
    const result = await api(path, body);
    $('command-output').textContent = result.output || '（无输出）';
    const duration = Number(result.duration);
    $('command-result-meta').textContent = `退出码 ${result.returncode ?? '—'} · 耗时 ${Number.isFinite(duration) ? duration.toFixed(2) : '—'} 秒${result.timed_out ? ' · 已超时并停止' : ''}${result.truncated ? ' · 输出已截断' : ''}`;
    if (result.timed_out || result.returncode) notify(result.timed_out ? '命令超时' : `命令退出码 ${result.returncode}`);
  } catch (error) {
    $('command-result-meta').textContent = '命令执行失败';
    $('command-output').textContent = error.message;
    throw error;
  } finally {
    commandRunning = false;
    for (const button of document.querySelectorAll('.command-run, #shell-form button[type=submit]')) button.disabled = false;
  }
}

async function loadTools() {
  const tools = await api('/api/shortcuts');
  const list = $('tool-list');
  list.replaceChildren();
  if (!tools.length) list.append(element('p', 'empty', '暂无工具'));
  for (const tool of tools) {
    const button = commandButton('', 'tool-button', async () => {
      await api(`/api/shortcuts/${encodeURIComponent(tool.id)}/launch`, {});
    });
    button.title = tool.path;
    const label = element('span', '', tool.name);
    button.append(label);
    list.append(button);
  }
  toolsLoaded = true;
}

async function loadProcesses() {
  processes = await api('/api/processes');
  processesLoaded = true;
  processReadTime = new Date().toLocaleTimeString('zh-CN');
  renderProcesses();
}

function renderProcesses() {
  const search = $('process-search').value.trim().toLocaleLowerCase();
  const filtered = processes.filter(process => (!$('favorites-only').checked || process.favorite) && `${process.name} ${process.pid} ${process.username || ''} ${process.exe || ''}`.toLocaleLowerCase().includes(search));
  filtered.sort((a, b) => Number(Boolean(b.favorite)) - Number(Boolean(a.favorite)) || Number(b.memory_mb) - Number(a.memory_mb));
  $('process-meta').textContent = processesLoaded ? `${filtered.length} / ${processes.length} 个进程 · 上次刷新 ${processReadTime}` : '尚未刷新';
  const list = $('process-list');
  list.replaceChildren();
  if (!processesLoaded) return;
  if (!filtered.length) { list.append(element('p', 'empty', '无匹配进程')); return; }
  const fragment = document.createDocumentFragment();
  for (const process of filtered) {
    const row = element('div', 'list-item process-item');
    const favorite = commandButton(process.favorite ? '★' : '☆', 'quiet favorite-toggle', async () => {
      const next = !process.favorite;
      await api('/api/processes/favorite', {key: process.favorite_key, favorite: next});
      for (const entry of processes) if (entry.favorite_key === process.favorite_key) entry.favorite = next;
      renderProcesses();
    });
    favorite.setAttribute('aria-pressed', String(Boolean(process.favorite)));
    favorite.setAttribute('aria-label', `${process.favorite ? '取消收藏' : '收藏'} ${process.name}`);
    favorite.disabled = !process.favorite_key;
    const content = element('div', 'item-content');
    content.append(element('p', 'item-title', process.name));
    const details = element('div', 'process-details');
    const memory = Number(process.memory_mb);
    const cpu = Number(process.cpu_percent);
    details.append(element('span', '', `PID ${process.pid}`), element('span', '', `${Number.isFinite(memory) ? memory.toFixed(1) : '—'} MB`), element('span', '', `CPU ${Number.isFinite(cpu) ? cpu.toFixed(1) : '—'}%`));
    if (process.username) details.append(element('span', '', process.username));
    content.append(details);
    if (process.termination_requested) content.append(element('p', 'hint', '已请求结束，待刷新'));
    if (process.exe) content.append(element('p', 'process-path', process.exe));
    const actions = element('div', 'item-actions');
    for (const force of [false, true]) {
      const terminate = commandButton(force ? '强制结束' : '结束', force ? 'danger' : 'secondary', async () => {
        const verb = force ? '强制结束' : '结束';
        if (!await ask(`${verb}进程`, `${verb}「${process.name}」（PID ${process.pid}）？\n未保存的内容可能丢失。结束进程前请确认目标。`, verb)) return;
        await api(`/api/processes/${process.pid}/terminate`, {create_time: process.create_time, force});
        process.termination_requested = true;
        renderProcesses();
      });
      terminate.disabled = Boolean(process.termination_requested) || !Number.isFinite(process.create_time);
      actions.append(terminate);
    }
    row.append(favorite, content, actions);
    fragment.append(row);
  }
  list.append(fragment);
}

async function loadDesktopInfo() {
  const info = await api('/api/desktop/info');
  const selected = $('monitor').value;
  $('monitor').replaceChildren();
  for (const monitor of info.monitors) {
    const option = element('option', '', `${monitor.name || `显示器 ${monitor.id}`} · ${monitor.width}×${monitor.height} (${monitor.left}, ${monitor.top})`);
    option.value = monitor.id;
    $('monitor').append(option);
  }
  if ([...$('monitor').options].some(option => option.value === selected)) $('monitor').value = selected;
  if (!info.monitors.length) throw new Error('电脑当前没有可用的显示器。');
  availableKeys = info.keys;
  $('key-suggestions').replaceChildren();
  for (const key of availableKeys) {
    const option = element('option');
    option.value = key;
    $('key-suggestions').append(option);
  }
  const common = ['esc', 'enter', 'tab', 'backspace', 'space', 'up', 'down', 'left', 'right', 'win', 'volumeup', 'volumedown'];
  $('key-buttons').replaceChildren();
  for (const key of common) {
    const actual = availableKeys.find(value => value.toLowerCase() === key);
    if (!actual) continue;
    const button = commandButton(actual, 'secondary', async () => {
      await api('/api/desktop/key', {keys: [actual]});
    });
    $('key-buttons').append(button);
  }
  desktopLoaded = true;
}

function sizeDesktopImage() {
  const viewport = $('desktop-viewport');
  if (frameGeometry && viewport.clientWidth) $('desktop-image').style.width = `${viewport.clientWidth * Number($('desktop-zoom').value)}px`;
}

function stopLive(abort = false) {
  live = false;
  clearTimeout(liveTimer);
  $('live-toggle').setAttribute('aria-pressed', 'false');
  $('live-toggle').textContent = '连续画面：关';
  if (abort) {
    snapshotGeneration++;
    snapshotController?.abort();
  }
}

function snapshot() {
  if (snapshotPromise) {
    if (!snapshotController?.signal.aborted) return snapshotPromise;
    return snapshotPromise.catch(error => {
      if (error.name !== 'AbortError') throw error;
    }).then(snapshot);
  }
  if (document.hidden || activeTab !== 'desktop') return Promise.resolve();
  const generation = snapshotGeneration;
  snapshotController = new AbortController();
  const controller = snapshotController;
  snapshotPromise = (async () => {
    if (!desktopLoaded) await loadDesktopInfo();
    if (generation !== snapshotGeneration || controller.signal.aborted) return;
    const query = new URLSearchParams({monitor: $('monitor').value, width: settings.desktop_width, quality: settings.desktop_quality});
    $('snapshot-status').textContent = '正在截取画面…';
    $('snapshot').disabled = true;
    $('snapshot').setAttribute('aria-busy', 'true');
    let nextURL = null;
    try {
      const response = await checkedResponse(await fetch(`/api/desktop/screenshot?${query}`, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal}));
      const geometry = {};
      for (const [key, header] of Object.entries({left: 'X-Desktop-Left', top: 'X-Desktop-Top', width: 'X-Desktop-Width', height: 'X-Desktop-Height', imageWidth: 'X-Image-Width', imageHeight: 'X-Image-Height'})) {
        const raw = response.headers.get(header);
        if (raw === null || !Number.isFinite(Number(raw))) throw new Error('截图缺少屏幕坐标信息，无法安全操作。');
        geometry[key] = Number(raw);
      }
      if (geometry.width <= 0 || geometry.height <= 0 || geometry.imageWidth <= 0 || geometry.imageHeight <= 0) throw new Error('截图尺寸无效。');
      const blob = await response.blob();
      if (controller.signal.aborted || generation !== snapshotGeneration) return;
      nextURL = URL.createObjectURL(blob);
      const image = new Image();
      image.src = nextURL;
      await image.decode();
      if (controller.signal.aborted || generation !== snapshotGeneration || activeTab !== 'desktop' || document.hidden) return;
      if (image.naturalWidth !== geometry.imageWidth || image.naturalHeight !== geometry.imageHeight) throw new Error('截图像素与坐标信息不一致，请重新截取。');
      const previousURL = frameURL;
      frameURL = nextURL;
      nextURL = null;
      frameGeometry = geometry;
      $('desktop-image').src = frameURL;
      $('desktop-image').hidden = false;
      $('desktop-empty').hidden = true;
      sizeDesktopImage();
      if (previousURL) URL.revokeObjectURL(previousURL);
      $('snapshot-status').textContent = `${new Date().toLocaleTimeString('zh-CN')}${live ? ' · 连续画面' : ''}`;
    } catch (error) {
      if (error.name !== 'AbortError') $('snapshot-status').textContent = `截取失败：${error.message}`;
      else if (generation !== snapshotGeneration) $('snapshot-status').textContent = '截图已停止';
      throw error;
    } finally {
      if (nextURL) URL.revokeObjectURL(nextURL);
      $('snapshot').disabled = false;
      $('snapshot').removeAttribute('aria-busy');
    }
  })().finally(() => {
    snapshotPromise = null;
    if (snapshotController === controller) snapshotController = null;
  });
  return snapshotPromise;
}

async function liveFrame() {
  if (!live || document.hidden || activeTab !== 'desktop') { stopLive(true); return; }
  try { await snapshot(); }
  catch (error) {
    if (error.name !== 'AbortError') notify(error.message);
    stopLive();
    return;
  }
  if (live && !document.hidden && activeTab === 'desktop') liveTimer = setTimeout(liveFrame, Number(settings.desktop_interval) * 1000);
}

function clickBody(x, y) {
  const kind = $('click-kind').value;
  return {x, y, button: kind === 'double' ? 'left' : kind, clicks: kind === 'double' ? 2 : 1};
}

async function sendClick(x, y) {
  await api('/api/desktop/click', clickBody(x, y));
  $('click-status').textContent = `已发送${$('click-kind').selectedOptions[0].textContent}：(${x}, ${y})`;
}

function mappedPoint(clientX, clientY) {
  if (!frameGeometry || $('desktop-image').hidden) throw new Error('请先截取桌面画面。');
  const rect = $('desktop-image').getBoundingClientRect();
  if (!rect.width || !rect.height || clientX < rect.left || clientX >= rect.right || clientY < rect.top || clientY >= rect.bottom) return null;
  return {
    x: frameGeometry.left + Math.min(frameGeometry.width - 1, Math.floor((clientX - rect.left) / rect.width * frameGeometry.width)),
    y: frameGeometry.top + Math.min(frameGeometry.height - 1, Math.floor((clientY - rect.top) / rect.height * frameGeometry.height))
  };
}

async function loadLogs() {
  const data = await api('/api/logs');
  $('logs-output').textContent = data.lines.length ? data.lines.join('\n') : '暂无日志记录。';
  logsLoaded = true;
}

for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click', () => selectTab(button.dataset.tab));
for (const button of document.querySelectorAll('[data-open]')) button.addEventListener('click', () => selectTab(button.dataset.open));
$('notice-close').addEventListener('click', () => { $('notice').hidden = true; });
$('auth-dialog').addEventListener('cancel', event => event.preventDefault());
onSubmit('auth-form', async () => {
  const password = $('auth-password').value;
  $('auth-password').value = '';
  const target = new URL('/r', window.location.origin);
  target.searchParams.set('p', password);
  window.location.replace(target.href);
});
onClick('state-refresh', loadState);
onClick('volume-refresh', loadVolume);
$('volume').addEventListener('input', () => { $('volume-value').textContent = `${$('volume').value}%`; });
$('volume').addEventListener('change', () => { void action(null, () => changeVolume({level: Number($('volume').value)})); });
$('mute').addEventListener('click', () => { if (volumeState) void action(null, () => changeVolume({muted: !volumeState.muted})); });
onSubmit('shell-form', () => runCommand('/api/shell', {command: $('shell-command').value, shell: $('shell-type').value, timeout: Number($('shell-timeout').value)}));
onClick('command-add', async () => { openCommand(); });
onClick('command-close', async () => { $('command-dialog').close(); });
onSubmit('command-form', async () => {
  const body = {name: $('command-name').value.trim(), command: $('command-text').value, shell: $('command-shell').value, timeout: Number($('command-timeout').value)};
  if (!body.name || !body.command.trim()) throw new Error('请填写名称与命令内容。');
  if ($('command-id').value) body.id = $('command-id').value;
  const saved = await api('/api/commands', body);
  const index = commands.findIndex(command => command.id === saved.id);
  if (index < 0) commands.push(saved); else commands[index] = saved;
  renderCommands();
  $('command-dialog').close();
});
onClick('output-clear', async () => {
  if (commandRunning) throw new Error('命令仍在执行，请等待完成后清空。');
  $('command-output').textContent = '';
  $('command-result-meta').textContent = '尚未执行';
});
onClick('tools-refresh', loadTools);
onClick('process-refresh', loadProcesses);
$('process-search').addEventListener('input', renderProcesses);
$('favorites-only').addEventListener('change', renderProcesses);
onClick('snapshot', snapshot);
onClick('live-toggle', async () => {
  if (live) { stopLive(true); return; }
  live = true;
  $('live-toggle').setAttribute('aria-pressed', 'true');
  $('live-toggle').textContent = '连续画面：开';
  void liveFrame();
});
$('monitor').addEventListener('change', () => {
  stopLive(true);
  frameGeometry = null;
  if (frameURL) { URL.revokeObjectURL(frameURL); frameURL = null; }
  $('desktop-image').hidden = true;
  $('desktop-image').removeAttribute('src');
  $('desktop-empty').hidden = false;
  void action(null, async () => {
    if (snapshotPromise) { try { await snapshotPromise; } catch (_) {} }
    await snapshot();
  });
});
$('desktop-zoom').addEventListener('input', () => {
  const viewport = $('desktop-viewport');
  const oldWidth = $('desktop-image').getBoundingClientRect().width || viewport.clientWidth;
  const centerX = (viewport.scrollLeft + viewport.clientWidth / 2) / oldWidth;
  const centerY = (viewport.scrollTop + viewport.clientHeight / 2) / oldWidth;
  sizeDesktopImage();
  const newWidth = $('desktop-image').getBoundingClientRect().width;
  viewport.scrollLeft = centerX * newWidth - viewport.clientWidth / 2;
  viewport.scrollTop = centerY * newWidth - viewport.clientHeight / 2;
  $('zoom-value').textContent = `${Number($('desktop-zoom').value)}×`;
});
$('desktop-zoom').addEventListener('change', () => {
  void action(null, async () => {
    const zoom = Number($('desktop-zoom').value);
    settings = {...settings, ...await api('/api/settings', {desktop_zoom: zoom})};
  });
});
$('click-enabled').addEventListener('change', () => {
  $('click-status').textContent = $('click-enabled').checked ? '画面点击已开启' : '仅选取坐标';
});
$('desktop-image').addEventListener('pointerdown', event => {
  pointerMoved = false;
  pointerStart = {id: event.pointerId, x: event.clientX, y: event.clientY, scrollLeft: $('desktop-viewport').scrollLeft, scrollTop: $('desktop-viewport').scrollTop, mouse: event.pointerType === 'mouse'};
  if (pointerStart.mouse && event.button === 0) { $('desktop-image').setPointerCapture(event.pointerId); event.preventDefault(); }
});
$('desktop-image').addEventListener('pointermove', event => {
  if (!pointerStart || pointerStart.id !== event.pointerId) return;
  const dx = event.clientX - pointerStart.x;
  const dy = event.clientY - pointerStart.y;
  if (Math.hypot(dx, dy) > 7) pointerMoved = true;
  if (pointerStart.mouse && pointerMoved) {
    $('desktop-viewport').scrollLeft = pointerStart.scrollLeft - dx;
    $('desktop-viewport').scrollTop = pointerStart.scrollTop - dy;
  }
});
$('desktop-image').addEventListener('pointerup', () => { pointerStart = null; });
$('desktop-image').addEventListener('pointercancel', () => { pointerMoved = true; pointerStart = null; });
$('desktop-image').addEventListener('contextmenu', event => event.preventDefault());
$('desktop-image').addEventListener('click', event => {
  if (pointerMoved) return;
  void action(null, async () => {
    const point = mappedPoint(event.clientX, event.clientY);
    if (!point) return;
    $('coordinate-x').value = point.x;
    $('coordinate-y').value = point.y;
    $('click-status').textContent = `已选 (${point.x}, ${point.y})`;
    if ($('click-enabled').checked) await sendClick(point.x, point.y);
  });
});
onSubmit('coordinate-form', async () => {
  const x = Number($('coordinate-x').value);
  const y = Number($('coordinate-y').value);
  if (!Number.isSafeInteger(x) || !Number.isSafeInteger(y)) throw new Error('坐标必须为整数。');
  await sendClick(x, y);
});
onSubmit('key-form', async () => {
  if (!desktopLoaded) await loadDesktopInfo();
  const requested = $('key-chord').value.split('+').map(key => key.trim()).filter(Boolean);
  if (!requested.length) throw new Error('请输入按键名称。');
  const keys = requested.map(key => availableKeys.find(value => value.toLowerCase() === key.toLowerCase()));
  if (keys.some(key => !key)) throw new Error('存在不支持的按键，请从键名建议中选择。');
  await api('/api/desktop/key', {keys});
});
onSubmit('text-form', async () => { await api('/api/desktop/text', {text: $('desktop-text').value}); });
onSubmit('scroll-form', async () => { await api('/api/desktop/scroll', {amount: Number($('scroll-amount').value)}); });
onSubmit('settings-form', async () => {
  applySettings(await api('/api/settings', {desktop_interval: Number($('setting-interval').value), desktop_quality: Number($('setting-quality').value), desktop_width: Number($('setting-width').value)}));
});
onSubmit('relay-form', async () => {
  const body = {enabled: $('relay-enabled').checked, url: $('relay-url').value.trim()};
  const token = $('relay-token').value;
  $('relay-token').value = '';
  if (token) body.token = token;
  applyRelay(await api('/api/relay', body));
});
onClick('logs-refresh', loadLogs);
onClick('logout', async () => {
  if (!await ask('退出远程助手', '退出当前浏览器的会话？重新连接需要访问密码。', '退出登录')) return;
  stopTouchpad();
  resetMusic();
  await api('/api/logout', {});
  showAuth();
});
document.addEventListener('visibilitychange', () => { if (document.hidden) stopLive(true); });
window.addEventListener('pagehide', () => {
  stopLive(true);
  if (frameURL) { URL.revokeObjectURL(frameURL); frameURL = null; }
});
window.addEventListener('pageshow', event => {
  if (event.persisted) {
    if (!$('desktop-image').hidden && !frameURL) {
      frameGeometry = null;
      $('desktop-image').hidden = true;
      $('desktop-image').removeAttribute('src');
      $('desktop-empty').hidden = false;
    }
    void action(null, loadState);
  }
});
window.addEventListener('resize', sizeDesktopImage);

void action(null, async () => {
  try { await loadState(); }
  catch (error) {
    setConnection($('auth-dialog').open ? '需要登录' : '连接未就绪', 'offline');
    $('hostname').textContent = '尚未连接';
    $('platform').textContent = '连接失败';
    throw error;
  }
  await loadVolume();
});

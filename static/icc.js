'use strict';

let iccItems = [];
let iccReady = false;
let iccBusy = false;
let iccGeneration = 0;

function iccControls() {
  $('icc-refresh').disabled = iccBusy;
  for (const button of document.querySelectorAll('#icc-actions button, #icc-uri-form button')) button.disabled = iccBusy || !iccReady;
}

function renderIcc() {
  const search = $('icc-search').value.trim().toLocaleLowerCase();
  const matches = iccItems.filter(item => `${item.name} ${item.group} ${item.uri}`.toLocaleLowerCase().includes(search));
  const list = $('icc-actions');
  list.replaceChildren();
  for (const item of matches) {
    const button = commandButton('', `tool-button icc-shortcut${item.confirmation ? ' icc-caution' : ''}`, () => runIcc(item.uri, item.confirmation));
    button.dataset.iccUri = item.uri;
    button.title = item.uri;
    button.append(element('span', '', item.name));
    const lastUsed = item.last_used ? new Date(item.last_used).toLocaleString('zh-CN', {hour12: false}) : '未使用';
    button.append(element('span', 'muted', `${item.group} · ${lastUsed}`));
    list.append(button);
  }
  if (!matches.length) list.append(element('p', 'empty', '无匹配操作'));
  iccControls();
}

function applyIcc(state) {
  iccItems = state.items;
  iccReady = state.registered;
  $('icc-status').textContent = state.message;
  renderIcc();
}

async function refreshIcc() {
  if (iccBusy) return;
  const generation = ++iccGeneration;
  const state = await api('/api/icc');
  if (generation === iccGeneration && authenticated) applyIcc(state);
}

async function runIcc(uri, confirmation) {
  if (iccBusy || !iccReady) return;
  iccBusy = true;
  const generation = ++iccGeneration;
  iccControls();
  try {
    if (confirmation && !await ask('调用 ICC', `${confirmation}\n${uri}`, '调用')) return;
    const state = await api('/api/icc/launch', {uri});
    if (generation !== iccGeneration || !authenticated) return;
    applyIcc(state);
    $('icc-result').textContent = `${state.dispatched_uri} · ${state.notice}`;
  } finally {
    iccBusy = false;
    iccControls();
  }
}

function resetIcc() {
  ++iccGeneration;
  iccItems = [];
  iccReady = false;
  $('icc-status').textContent = '需要登录后刷新 ICC。';
  renderIcc();
}

onClick('icc-refresh', refreshIcc);
$('icc-search').addEventListener('input', renderIcc);
onSubmit('icc-uri-form', () => {
  const uri = $('icc-uri').value.trim();
  return runIcc(uri, '将此 URI 提交给受控电脑的 ICC？具体操作可能改变墨迹、配置或退出程序。');
});

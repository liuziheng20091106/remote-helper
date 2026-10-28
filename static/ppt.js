'use strict';

let pptState = null;
let pptPins = new Set();
let pptRequest = 0;
let pptRevision = 0;
let pptImageGeneration = 0;
let pptPreview = 0;
let pptPreviewRequest = 0;
let pptBusy = false;
const pptImageURLs = new Set();
const pptImageControllers = new Set();
const pptObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    pptObserver.unobserve(entry.target);
    void loadPptImage(entry.target, Number(entry.target.dataset.slide), 'thumb');
  }
}, {rootMargin: '160px'});

function clearPptImages() {
  pptImageGeneration++;
  pptObserver.disconnect();
  for (const controller of pptImageControllers) controller.abort();
  pptImageControllers.clear();
  for (const url of pptImageURLs) URL.revokeObjectURL(url);
  pptImageURLs.clear();
  $('ppt-preview-image').hidden = true;
  $('ppt-preview-image').removeAttribute('src');
}

function resetPpt(message = '请刷新 PowerPoint 状态。', moduleConnected = false) {
  pptRequest++;
  clearPptImages();
  pptState = null;
  pptPins.clear();
  pptPreview = 0;
  if ($('ppt-dialog').open) $('ppt-dialog').close();
  $('ppt-name').textContent = '未连接 PowerPoint';
  $('ppt-module-status').textContent = moduleConnected ? 'PPT 模块已连接 · 普通权限' : 'PPT 模块未连接';
  $('ppt-status').textContent = message;
  $('ppt-slides').replaceChildren();
  renderPptPins();
  updatePptControls();
}

function updatePptControls() {
  const disabled = pptBusy || !pptState?.available || !pptState.slides.length;
  for (const button of $('ppt-controls').querySelectorAll('button')) button.disabled = disabled;
  $('ppt-page').disabled = disabled;
  $('ppt-goto-form').querySelector('button').disabled = disabled;
  $('ppt-preview-goto').disabled = disabled;
  for (const button of $('ppt-pins').querySelectorAll('button')) button.disabled = disabled;
  for (const card of $('ppt-slides').children) {
    const current = Number(card.dataset.slide) === pptState?.current_slide;
    card.classList.toggle('ppt-current', current);
    const preview = card.querySelector('.ppt-thumbnail');
    if (current) preview.setAttribute('aria-current', 'page');
    else preview.removeAttribute('aria-current');
  }
}

function pptPinKey() { return `remote-helper:ppt-pins:${pptState.bookmark_id}`; }

function loadPptPins() {
  try {
    const saved = JSON.parse(localStorage.getItem(pptPinKey()) || '[]');
    pptPins = new Set(Array.isArray(saved) ? saved.filter(id => Number.isInteger(id) && pptState.slides.some(slide => slide.id === id)) : []);
  } catch (_) { pptPins = new Set(); }
}

function renderPptPins() {
  const strip = $('ppt-pins');
  strip.replaceChildren();
  for (const slide of pptState?.slides || []) {
    if (!pptPins.has(slide.id)) continue;
    const page = slide.index;
    const button = element('button', 'secondary', `第 ${page} 页`);
    button.title = `${slide.title || '幻灯片'} · 点击跳转到电脑`;
    button.addEventListener('click', () => { void action(null, () => gotoPpt(page)); });
    strip.append(button);
  }
  if (!strip.childElementCount) strip.append(element('p', 'muted', '点击缩略图下的「固定」。'));
  for (const button of document.querySelectorAll('[data-ppt-pin]')) {
    const pinned = pptPins.has(pptState?.slides.find(slide => slide.index === Number(button.dataset.pptPin))?.id);
    button.textContent = pinned ? '取消固定' : '固定';
    button.setAttribute('aria-pressed', String(pinned));
  }
  if (pptPreview) {
    const pinned = pptPins.has(pptState?.slides.find(slide => slide.index === pptPreview)?.id);
    $('ppt-preview-pin').textContent = pinned ? '取消固定' : '固定';
    $('ppt-preview-pin').setAttribute('aria-pressed', String(pinned));
  }
  updatePptControls();
}

function togglePptPin(page) {
  const slide = pptState?.slides.find(slide => slide.index === page);
  if (!pptState?.available || !slide) return;
  if (pptPins.has(slide.id)) pptPins.delete(slide.id);
  else pptPins.add(slide.id);
  try { localStorage.setItem(pptPinKey(), JSON.stringify([...pptPins])); }
  catch (_) { $('ppt-status').textContent = '浏览器无法保存固定页面；当前会话仍可使用。'; }
  renderPptPins();
}

async function loadPptImage(img, page, size) {
  const deck = pptState?.deck_id;
  if (!deck) return;
  const generation = pptImageGeneration;
  const previewRequest = pptPreviewRequest;
  const controller = new AbortController();
  pptImageControllers.add(controller);
  const current = () => generation === pptImageGeneration && deck === pptState?.deck_id && img.isConnected && (size !== 'large' || previewRequest === pptPreviewRequest && $('ppt-dialog').open);
  try {
    const query = new URLSearchParams({deck, size, refresh: String(pptRevision)});
    const response = await checkedResponse(await fetch(`/api/ppt/slides/${page}?${query}`, {credentials: 'same-origin', cache: 'no-store', headers: {'X-Remote-Helper': '1'}, signal: controller.signal}));
    const blob = await response.blob();
    if (!current()) return;
    const url = URL.createObjectURL(blob);
    pptImageURLs.add(url);
    if (img.src.startsWith('blob:')) {
      URL.revokeObjectURL(img.src);
      pptImageURLs.delete(img.src);
    }
    img.onerror = () => {
      if (!current()) return;
      img.hidden = true;
      $(size === 'large' ? 'ppt-preview-status' : 'ppt-status').textContent = `第 ${page} 页图片无法显示，请刷新。`;
    };
    img.src = url;
    img.hidden = false;
    if (size === 'large') $('ppt-preview-status').textContent = '仅预览；点击「跳转到电脑」才会切换页面。';
  } catch (error) {
    if (error.name !== 'AbortError' && current()) {
      img.hidden = true;
      $(size === 'large' ? 'ppt-preview-status' : 'ppt-status').textContent = `第 ${page} 页：${error.message || '图片加载失败，请刷新。'}`;
    }
  } finally { pptImageControllers.delete(controller); }
}

function renderPptSlides() {
  clearPptImages();
  $('ppt-slides').replaceChildren();
  for (const slide of pptState.slides) {
    const card = element('div', 'ppt-slide');
    card.dataset.slide = slide.index;
    const label = `第 ${slide.index} 页${slide.title ? ` · ${slide.title}` : ''}`;
    const preview = element('button', 'ppt-thumbnail');
    preview.type = 'button';
    preview.title = `${label} · 放大预览`;
    preview.setAttribute('aria-label', `${label}，放大预览`);
    const img = element('img');
    img.alt = label;
    img.title = label;
    img.loading = 'lazy';
    img.decoding = 'async';
    img.dataset.slide = slide.index;
    if (pptState.width > 0 && pptState.height > 0) img.style.aspectRatio = `${pptState.width} / ${pptState.height}`;
    preview.append(img);
    preview.addEventListener('click', () => openPptPreview(slide.index));
    const caption = element('div', 'ppt-slide-caption');
    const title = element('span', '', label);
    title.title = label;
    const pin = element('button', 'secondary', '固定');
    pin.dataset.pptPin = slide.index;
    pin.setAttribute('aria-label', `固定或取消固定第 ${slide.index} 页`);
    pin.addEventListener('click', () => togglePptPin(slide.index));
    caption.append(title, pin);
    card.append(preview, caption);
    $('ppt-slides').append(card);
    pptObserver.observe(img);
  }
}

function applyPptState(state, refreshImages) {
  $('ppt-module-status').textContent = state.module_connected ? 'PPT 模块已连接 · 普通权限' : 'PPT 模块未连接';
  if (!state.available) {
    resetPpt(state.message || '没有可用的 PowerPoint 演示文稿，请在电脑上打开后刷新。', state.module_connected);
    return;
  }
  const changedDeck = pptState?.deck_id !== state.deck_id;
  const changedSlides = JSON.stringify(pptState?.slides) !== JSON.stringify(state.slides);
  pptState = state;
  if (changedDeck) {
    if ($('ppt-dialog').open) $('ppt-dialog').close();
    pptPreview = 0;
    loadPptPins();
  }
  $('ppt-name').textContent = state.name;
  $('ppt-status').textContent = `第 ${state.current_slide || '—'} / ${state.slides.length} 页 · ${state.slideshow ? '放映中' : '编辑模式'}${state.message ? ` · ${state.message}` : ''}`;
  $('ppt-page').max = String(state.slides.length);
  if (changedDeck) $('ppt-page').value = String(state.current_slide || 1);
  if (changedDeck || changedSlides || refreshImages) {
    renderPptSlides();
    if ($('ppt-dialog').open && state.slides.some(slide => slide.index === pptPreview)) openPptPreview(pptPreview);
    else if ($('ppt-dialog').open) $('ppt-dialog').close();
  }
  renderPptPins();
}

async function requestPpt(path = '/api/ppt/state', body, refreshImages = false) {
  if (pptBusy) return;
  pptBusy = true;
  const request = ++pptRequest;
  updatePptControls();
  $('ppt-status').textContent = '正在读取 PowerPoint…';
  if (refreshImages) pptRevision = Math.max(Date.now(), pptRevision + 1);
  try {
    const state = await api(path, body);
    if (request === pptRequest && authenticated) applyPptState(state, refreshImages);
  } catch (error) {
    if (request === pptRequest) resetPpt(error.message || '无法读取 PowerPoint，请刷新重试。');
  } finally {
    pptBusy = false;
    updatePptControls();
  }
}

function refreshPpt() { return requestPpt('/api/ppt/state', undefined, true); }

async function gotoPpt(page) {
  if (!pptState?.available || pptBusy) return;
  if (!Number.isSafeInteger(page) || !pptState.slides.some(slide => slide.index === page)) {
    $('ppt-status').textContent = '请输入有效的幻灯片页码。';
    return;
  }
  return requestPpt('/api/ppt/goto', {slide: page, deck: pptState.deck_id});
}

function openPptPreview(page) {
  const slide = pptState?.slides.find(slide => slide.index === page);
  if (!slide) return;
  pptPreview = page;
  pptPreviewRequest++;
  const img = $('ppt-preview-image');
  img.hidden = true;
  img.alt = `第 ${page} 页${slide.title ? ` · ${slide.title}` : ''}`;
  $('ppt-preview-title').textContent = img.alt;
  $('ppt-preview-status').textContent = '正在加载大图…';
  const index = pptState.slides.indexOf(slide);
  $('ppt-preview-prev').disabled = index === 0;
  $('ppt-preview-next').disabled = index === pptState.slides.length - 1;
  if (!$('ppt-dialog').open) $('ppt-dialog').showModal();
  renderPptPins();
  void loadPptImage(img, page, 'large');
}

onClick('ppt-refresh', refreshPpt);
for (const button of document.querySelectorAll('[data-ppt-action]')) {
  button.addEventListener('click', () => {
    if (button.disabled) return;
    void action(null, () => requestPpt('/api/ppt/action', {action: button.dataset.pptAction}));
  });
}
$('ppt-goto-form').addEventListener('submit', event => {
  event.preventDefault();
  if (event.currentTarget.reportValidity()) void action(null, () => gotoPpt(Number($('ppt-page').value)));
});
onClick('ppt-preview-close', async () => { $('ppt-dialog').close(); });
$('ppt-dialog').addEventListener('close', () => { pptPreviewRequest++; });
for (const [id, direction] of [['ppt-preview-prev', -1], ['ppt-preview-next', 1]]) {
  $(id).addEventListener('click', () => {
    const index = pptState?.slides.findIndex(slide => slide.index === pptPreview);
    const slide = pptState?.slides[index + direction];
    if (slide) openPptPreview(slide.index);
  });
}
$('ppt-preview-goto').addEventListener('click', () => { void action(null, () => gotoPpt(pptPreview)); });
onClick('ppt-preview-pin', async () => togglePptPin(pptPreview));
window.addEventListener('pagehide', clearPptImages);
window.addEventListener('pageshow', event => {
  if (event.persisted && activeTab === 'ppt' && authenticated) void action(null, refreshPpt);
});

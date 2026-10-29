'use strict';

const pad = $('touchpad-surface');
const padPointers = new Map();
const padHolds = new Map();
const padClient = window.crypto?.randomUUID?.() || `pad-${Date.now()}-${Math.random().toString(36).slice(2)}`;
let padSequence = 0;
let padQueue = [];
let padInFlight = false;
let padRequestController = null;
let padRelease = false;
let padReleasing = false;
let padUsed = false;
let padFrame = 0;
let padHeartbeat = 0;
let padLongPress = 0;
let padTapTimer = 0;
let padLastTap = 0;
let padGesture = null;
let padRemainder = {x: 0, y: 0};
let padCursor = {x: 0.5, y: 0.5};
let padSensitivity = 1.5;
try {
  const saved = Number(localStorage.getItem('touchpad-sensitivity'));
  if (saved >= 0.5 && saved <= 3) padSensitivity = saved;
} catch (_) {}
$('touchpad-sensitivity').value = padSensitivity;
$('touchpad-sensitivity-value').textContent = `${padSensitivity}×`;

function padEnabled() {
  return authenticated && activeTab === 'touchpad' && !document.hidden && !padRelease && !padReleasing;
}

function padStatus(message) { $('touchpad-status').textContent = message; }

function padBindInput(target, handlers) {
  if (typeof window.PointerEvent === 'function') {
    for (const [name, handler] of Object.entries(handlers)) target.addEventListener(`pointer${name}`, handler);
    target.addEventListener('pointercancel', () => stopTouchpad());
    return;
  }
  for (const [name, kind] of [['touchstart', 'down'], ['touchmove', 'move'], ['touchend', 'up']]) {
    const handler = handlers[kind];
    if (!handler) continue;
    target.addEventListener(name, event => {
      if (kind === 'down' && !padEnabled()) return;
      event.preventDefault();
      for (const touch of Array.from(event.changedTouches)) handler({
        pointerId: touch.identifier, pointerType: 'touch', button: 0,
        clientX: touch.clientX, clientY: touch.clientY,
        preventDefault: () => event.preventDefault()
      });
    }, {passive: false});
  }
  target.addEventListener('touchcancel', () => stopTouchpad(), {passive: false});
}

function padSchedule(delay = 16) {
  if (!padFrame && !padInFlight) padFrame = window.setTimeout(() => { padFrame = 0; void padFlush(); }, delay);
}

function padEnqueue(event) {
  if (!padEnabled()) return;
  const last = padQueue[padQueue.length - 1];
  if ((event.type === 'move' || event.type === 'wheel') && last?.type === event.type) {
    const bound = event.type === 'move' ? 4096 : 2400;
    last.dx = Math.max(-bound, Math.min(bound, last.dx + event.dx));
    last.dy = Math.max(-bound, Math.min(bound, last.dy + event.dy));
  } else if (padQueue.length < 64) padQueue.push(event);
  else {
    stopTouchpad();
    padStatus('网络较慢，已释放鼠标，请重新操作。');
    return;
  }
  padSchedule();
}

async function padFlush(heartbeat = false) {
  if (padInFlight || (!padRelease && !padQueue.length && !heartbeat)) return;
  const releasing = padRelease;
  const events = releasing ? [] : padQueue.splice(0, 64);
  padRelease = false;
  padReleasing = releasing;
  const payload = {id: padClient, seq: ++padSequence, events, release: releasing};
  padInFlight = true;
  padUsed = true;
  const controller = new AbortController();
  padRequestController = controller;
  const timeout = window.setTimeout(() => controller.abort(), 2000);
  try {
    await checkedResponse(await fetch('/api/desktop/touchpad', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-Remote-Helper': '1'},
      body: JSON.stringify(payload), signal: controller.signal
    }));
    if (releasing) padUsed = false;
  } catch (error) {
    stopTouchpad();
    if (!authenticated) padRelease = padUsed = false;
    padStatus(error.name === 'AbortError' ? '连接超时或操作已取消，已停止操作。' : error.message || '连接中断，已停止操作。');
  } finally {
    clearTimeout(timeout);
    padRequestController = null;
    padInFlight = false;
    padReleasing = false;
    if (padRelease || padQueue.length) padSchedule(releasing ? 500 : 16);
  }
}

function padHeld(button) { return [...padHolds.values()].includes(button); }

function padHold(source, button, down) {
  const wasHeld = padHeld(button);
  if (down) padHolds.set(source, button); else padHolds.delete(source);
  const held = padHeld(button);
  if (held !== wasHeld) padEnqueue({type: 'button', button, down: held});
  if (padHolds.size && !padHeartbeat) {
    padHeartbeat = window.setInterval(() => {
      if (!padEnabled()) { stopTouchpad(); return; }
      // Empty batches renew the server's drag lease without overlapping requests.
      if (!padInFlight && !padQueue.length) void padFlush(true);
    }, 1000);
  } else if (!padHolds.size) {
    clearInterval(padHeartbeat);
    padHeartbeat = 0;
  }
  $('touchpad-lock').setAttribute('aria-pressed', String(padHolds.has('lock')));
  pad.classList.toggle('dragging', padHeld('left'));
}


function stopTouchpad(release = true) {
  if (release && !padReleasing) padRequestController?.abort();
  clearTimeout(padLongPress);
  clearTimeout(padTapTimer);
  clearTimeout(padFrame);
  clearInterval(padHeartbeat);
  padLongPress = padTapTimer = padFrame = padHeartbeat = 0;
  padLastTap = 0;
  padPointers.clear();
  padHolds.clear();
  padQueue = [];
  padGesture = null;
  padRemainder = {x: 0, y: 0};
  $('touchpad-fingers').replaceChildren();
  $('touchpad-lock').setAttribute('aria-pressed', 'false');
  pad.classList.remove('dragging');
  if (release && padUsed) { padRelease = true; padSchedule(); }
}

function padMove(dx, dy) {
  padRemainder.x += dx * padSensitivity;
  padRemainder.y += dy * padSensitivity;
  const x = Math.trunc(padRemainder.x);
  const y = Math.trunc(padRemainder.y);
  padRemainder.x -= x;
  padRemainder.y -= y;
  if (!x && !y) return;
  padEnqueue({type: 'move', dx: Math.max(-4096, Math.min(4096, x)), dy: Math.max(-4096, Math.min(4096, y))});
  padCursor.x = Math.max(0, Math.min(1, padCursor.x + x / Math.max(1, pad.clientWidth)));
  padCursor.y = Math.max(0, Math.min(1, padCursor.y + y / Math.max(1, pad.clientHeight)));
  $('touchpad-cursor').style.left = `${padCursor.x * 100}%`;
  $('touchpad-cursor').style.top = `${padCursor.y * 100}%`;
}

function padGeometry() {
  const points = [...padPointers.values()];
  const x = points.reduce((sum, point) => sum + point.x, 0) / points.length;
  const y = points.reduce((sum, point) => sum + point.y, 0) / points.length;
  const distance = points.length === 2 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0;
  return {x, y, distance};
}

function padDrawFingers() {
  const bounds = pad.getBoundingClientRect();
  $('touchpad-fingers').replaceChildren(...[...padPointers.values()].map(point => {
    const dot = element('span', 'touchpad-finger');
    dot.style.left = `${point.x - bounds.left}px`;
    dot.style.top = `${point.y - bounds.top}px`;
    return dot;
  }));
}

function padBeginDrag() {
  if (!padGesture || padPointers.size !== 1 || padGesture.max !== 1) return;
  padGesture.drag = true;
  padGesture.double = false;
  padLastTap = 0;
  padHold('gesture', 'left', true);
  padStatus('拖动中');
}

function padPointerDown(event) {
  if (!padEnabled()) return;
  const first = padPointers.values().next().value;
  if (first && Boolean(first.mouse) !== (event.pointerType === 'mouse')) return;
  event.preventDefault();
  if (typeof window.PointerEvent === 'function') pad.setPointerCapture?.(event.pointerId);
  if (event.pointerType === 'mouse') {
    padPointers.set(event.pointerId, {x: event.clientX, y: event.clientY, mouse: true});
    const button = ['left', 'middle', 'right'][event.button];
    if (button) padHold(`mouse-${button}`, button, true);
    return;
  }
  clearTimeout(padLongPress);
  const now = performance.now();
  if (!padPointers.size) {
    const double = padLastTap > 0 && now - padLastTap < 300;
    if (double) { clearTimeout(padTapTimer); padTapTimer = 0; }
    padGesture = {started: now, max: 1, moved: 0, double, drag: false, mode: '', dx: 0, dy: 0, zoom: 0};
  }
  padPointers.set(event.pointerId, {x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY});
  padGesture.max = Math.max(padGesture.max, padPointers.size);
  if (padPointers.size > 1) {
    clearTimeout(padTapTimer);
    padTapTimer = 0;
    padLastTap = 0;
    padHold('gesture', 'left', false);
    padGesture.drag = false;
    padGesture.double = false;
  }
  padGesture.geometry = padGeometry();
  padGesture.origin = padGesture.geometry;
  if (padPointers.size === 1) padLongPress = window.setTimeout(padBeginDrag, padGesture.double ? 180 : 480);
  padDrawFingers();
}

function padPointerMove(event) {
  const point = padPointers.get(event.pointerId);
  if (!point || !padEnabled()) return;
  event.preventDefault();
  const dx = event.clientX - point.x;
  const dy = event.clientY - point.y;
  point.x = event.clientX;
  point.y = event.clientY;
  if (point.mouse) { padMove(dx, dy); return; }
  const gesture = padGesture;
  if (!gesture) return;
  gesture.moved = Math.max(gesture.moved, Math.hypot(point.x - point.startX, point.y - point.startY));
  if (gesture.moved > 7) clearTimeout(padLongPress);
  if (padPointers.size === 1 && gesture.max === 1) {
    if (gesture.double && gesture.moved > 7) padBeginDrag();
    padMove(dx, dy);
  } else if (padPointers.size === 2 && gesture.max === 2) {
    const next = padGeometry();
    if (!gesture.mode) {
      if (Math.abs(next.distance - gesture.origin.distance) > 9) gesture.mode = 'zoom';
      else if (Math.hypot(next.x - gesture.origin.x, next.y - gesture.origin.y) > 6) gesture.mode = 'scroll';
    }
    if (gesture.mode === 'zoom' && next.distance > 0 && gesture.geometry.distance > 0) {
      gesture.zoom += Math.log(next.distance / gesture.geometry.distance) * 10;
      const steps = Math.max(-10, Math.min(10, Math.trunc(gesture.zoom)));
      if (steps) { padEnqueue({type: 'zoom', steps}); gesture.zoom -= steps; padStatus('缩放'); }
    } else if (gesture.mode === 'scroll') {
      const wheelX = Math.round((next.x - gesture.geometry.x) * -6 * padSensitivity);
      const wheelY = Math.round((next.y - gesture.geometry.y) * 6 * padSensitivity);
      if (wheelX || wheelY) padEnqueue({type: 'wheel', dx: Math.max(-2400, Math.min(2400, wheelX)), dy: Math.max(-2400, Math.min(2400, wheelY))});
      padStatus('双指滚动');
    }
    gesture.geometry = next;
  } else if (padPointers.size === 3 && gesture.max === 3) {
    const next = padGeometry();
    gesture.dx += next.x - gesture.geometry.x;
    gesture.dy += next.y - gesture.geometry.y;
    gesture.geometry = next;
  }
  padDrawFingers();
}

function padPointerUp(event) {
  const point = padPointers.get(event.pointerId);
  if (!point) return;
  event.preventDefault();
  if (point.mouse) {
    for (const button of ['left', 'middle', 'right']) padHold(`mouse-${button}`, button, false);
    padPointers.delete(event.pointerId);
    return;
  }
  clearTimeout(padLongPress);
  const gesture = padGesture;
  padPointers.delete(event.pointerId);
  padDrawFingers();
  if (padPointers.size || !gesture) return;
  padHold('gesture', 'left', false);
  const elapsed = performance.now() - gesture.started;
  if (gesture.max === 3) {
    const {dx, dy} = gesture;
    if (Math.max(Math.abs(dx), Math.abs(dy)) > 50) {
      const name = Math.abs(dy) > Math.abs(dx) ? (dy < 0 ? 'task_view' : 'desktop') : (dx < 0 ? 'previous_app' : 'next_app');
      padEnqueue({type: 'gesture', name});
      padStatus({task_view: '任务视图', desktop: '显示桌面', previous_app: '上个应用', next_app: '下个应用'}[name]);
    }
  } else if (gesture.max === 2 && !gesture.mode && gesture.moved < 10 && elapsed < 350) {
    padEnqueue({type: 'click', button: 'right', clicks: 1});
    padStatus('右键单击');
  } else if (gesture.max === 1 && !gesture.drag && !padHeld('left') && gesture.moved < 7 && elapsed < 350) {
    if (gesture.double) {
      padLastTap = 0;
      padEnqueue({type: 'click', button: 'left', clicks: 2});
      padStatus('左键双击');
    } else {
      padLastTap = performance.now();
      padTapTimer = window.setTimeout(() => {
        padTapTimer = 0;
        padLastTap = 0;
        if (!padHeld('left')) padEnqueue({type: 'click', button: 'left', clicks: 1});
        padStatus('左键单击');
      }, 300);
    }
  } else padStatus('就绪');
  padGesture = null;
}

padBindInput(pad, {down: padPointerDown, move: padPointerMove, up: padPointerUp});
if (typeof window.PointerEvent === 'function') pad.addEventListener('lostpointercapture', event => { if (padPointers.has(event.pointerId)) stopTouchpad(); });
pad.addEventListener('contextmenu', event => event.preventDefault());
pad.addEventListener('wheel', event => {
  if (!padEnabled()) return;
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? pad.clientHeight : 1;
  if (event.ctrlKey) padEnqueue({type: 'zoom', steps: event.deltaY < 0 ? 1 : -1});
  else padEnqueue({type: 'wheel', dx: Math.max(-2400, Math.min(2400, Math.round(event.deltaX * scale))), dy: Math.max(-2400, Math.min(2400, Math.round(-event.deltaY * scale)))});
}, {passive: false});

for (const button of document.querySelectorAll('[data-pad-button]')) {
  const source = `button-${button.dataset.padButton}`;
  padBindInput(button, {
    down: event => {
      if (!padEnabled() || event.button !== 0) return;
      event.preventDefault();
      if (typeof window.PointerEvent === 'function') button.setPointerCapture?.(event.pointerId);
      padHold(source, button.dataset.padButton, true);
    },
    up: () => padHold(source, button.dataset.padButton, false)
  });
  if (typeof window.PointerEvent === 'function') button.addEventListener('lostpointercapture', () => { if (padHolds.has(source)) stopTouchpad(); });
  button.addEventListener('contextmenu', event => event.preventDefault());
  button.addEventListener('click', event => {
    if (event.detail === 0 && padEnabled()) padEnqueue({type: 'click', button: button.dataset.padButton, clicks: 1});
  });
}
$('touchpad-lock').addEventListener('click', () => {
  if (!padEnabled()) return;
  padHold('lock', 'left', !padHolds.has('lock'));
  padStatus(padHolds.has('lock') ? '左键已锁定，再点一次释放' : '左键已释放');
});
$('touchpad-sensitivity').addEventListener('input', () => {
  padSensitivity = Number($('touchpad-sensitivity').value);
  $('touchpad-sensitivity-value').textContent = `${padSensitivity}×`;
  try { localStorage.setItem('touchpad-sensitivity', padSensitivity); } catch (_) {}
});
window.addEventListener('blur', () => stopTouchpad());
window.addEventListener('offline', () => { stopTouchpad(); padStatus('连接中断，已停止操作。'); });
window.addEventListener('pagehide', () => stopTouchpad());
document.addEventListener('visibilitychange', () => { if (document.hidden) stopTouchpad(); });

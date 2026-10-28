'use strict';

let ciState = null;
let ciGeneration = 0;

function renderClassIsland(state) {
  ciState = state;
  const connected = state.connected === true;
  $('ci-status').textContent = connected ? `${state.version} · ${state.visible ? '主界面已显示' : '主界面已隐藏'} · 偏移 ${state.time_offset_seconds} 秒` : state.message || 'ClassIsland 插件未连接';
  for (const id of ['ci-show', 'ci-hide', 'ci-clear', 'ci-offset-save', 'ci-notify-send']) $(id).disabled = !connected;
  if (connected) $('ci-offset').value = state.time_offset_seconds;
  $('ci-plan-name').textContent = state.plan_name || '当前没有课表';
  const previousLesson = $('ci-lesson').value;
  const previousSubject = $('ci-subject').value;
  $('ci-lesson').replaceChildren(...(state.lessons || []).map(lesson => {
    const option = element('option', '', `${lesson.index + 1}. ${lesson.start}–${lesson.end} · ${lesson.subject}`);
    option.value = lesson.index;
    return option;
  }));
  $('ci-subject').replaceChildren(...(state.subjects || []).map(subject => {
    const option = element('option', '', subject.name);
    option.value = subject.id;
    return option;
  }));
  if ([...$('ci-lesson').options].some(option => option.value === previousLesson)) $('ci-lesson').value = previousLesson;
  if ([...$('ci-subject').options].some(option => option.value === previousSubject)) $('ci-subject').value = previousSubject;
  const canChange = connected && Boolean(state.plan_id) && $('ci-lesson').options.length > 0 && $('ci-subject').options.length > 0;
  for (const id of ['ci-lesson', 'ci-subject', 'ci-lesson-save']) $(id).disabled = !canChange;
}

async function refreshClassIsland() {
  const generation = ++ciGeneration;
  try {
    const state = await api('/api/classisland/state');
    if (generation === ciGeneration && authenticated) renderClassIsland(state);
  } catch (error) {
    if (generation === ciGeneration) renderClassIsland({connected: false, message: error.message});
  }
}

async function classIslandAction(path, body = {}) {
  const generation = ++ciGeneration;
  try {
    const state = await api(`/api/classisland/${path}`, body);
    if (generation === ciGeneration && authenticated) renderClassIsland(state);
  } catch (error) {
    await refreshClassIsland();
    throw error;
  }
}

onClick('ci-refresh', refreshClassIsland);
onClick('ci-show', () => classIslandAction('visibility', {visible: true}));
onClick('ci-hide', () => classIslandAction('visibility', {visible: false}));
onClick('ci-clear', () => classIslandAction('notifications/clear'));
onSubmit('ci-offset-form', () => classIslandAction('time-offset', {seconds: Number($('ci-offset').value)}));
onSubmit('ci-lesson-form', async () => {
  if (!ciState?.connected || !ciState.plan_id) throw new Error('请先刷新当前课表。');
  await classIslandAction('lesson', {plan_id: ciState.plan_id, index: Number($('ci-lesson').value), subject_id: $('ci-subject').value});
});
onSubmit('ci-notify-form', () => classIslandAction('notify', {
  title: $('ci-notify-title').value.trim(), message: $('ci-notify-message').value.trim(),
  duration_seconds: Number($('ci-notify-duration').value), sound: $('ci-notify-sound').checked
}));

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const secretFields = ['bridge_token', 'bili_sessdata', 'bili_buvid3'];
  const listFields = ['extra_uids', 'bili_uids', 'bili_rooms'];
  const textFields = ['target_umo', 'bridge_url', 'bili_dedeuserid'];
  const numberFields = ['interval_seconds', 'gift_min_yuan'];
  const switchFields = ['notify_stream', 'notify_live_end', 'notify_superchat'];
  let bridge;
  let state;
  let loadedRevision;
  let dirty = false;
  let busy = true;
  let rowCounter = 0;
  let pollTimer;
  const clearSecrets = new Set();

  function message(text, kind = '') {
    $('message').textContent = text;
    $('message').className = `message ${kind}`;
    $('message').hidden = !text;
  }

  function setBusy(value) {
    busy = value;
    document.querySelectorAll('button, input, textarea').forEach((element) => { element.disabled = value; });
    $('save').textContent = value && state ? '正在应用…' : '保存并应用';
    if (!value) {
      $('test-send').disabled = !state?.settings.target_umo;
      secretFields.forEach((field) => {
        document.querySelector(`[data-clear-secret="${field}"]`).disabled = !state?.secrets_configured[field] && !$(field).value;
      });
    }
  }

  function markDirty() {
    dirty = true;
    $('save-hint').textContent = '有未保存的更改。';
  }

  function selectTab(button) {
    document.querySelectorAll('[role="tab"]').forEach((tab) => {
      const selected = tab === button;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !selected;
    });
  }

  document.querySelectorAll('[role="tab"]').forEach((tab, index, tabs) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      selectTab(next);
      next.focus();
    });
  });

  function removeButton(label, remove) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'text-button';
    button.textContent = '删除';
    button.setAttribute('aria-label', label);
    button.addEventListener('click', () => { remove(); updateEmptyStates(); markDirty(); });
    return button;
  }

  function updateEmptyStates() {
    $('rooms-empty').hidden = $('room-rows').children.length > 0;
    $('feeds-empty').hidden = $('feed-rows').children.length > 0;
  }

  function addRoom(room = '', uid = '') {
    const row = document.createElement('tr');
    rowCounter += 1;
    for (const [field, label, value] of [['room', '房间号', room], ['uid', '主播 UID', uid]]) {
      const cell = document.createElement('td');
      cell.dataset.label = label;
      const input = document.createElement('input');
      input.dataset[field] = '';
      input.value = value;
      input.inputMode = 'numeric';
      input.autocomplete = 'off';
      input.maxLength = 20;
      input.placeholder = label;
      input.setAttribute('aria-label', `${label} ${rowCounter}`);
      cell.append(input);
      row.append(cell);
    }
    const status = document.createElement('td');
    status.className = 'room-status';
    status.textContent = '待应用';
    const action = document.createElement('td');
    action.append(removeButton(`删除直播间 ${room || rowCounter}`, () => row.remove()));
    row.append(status, action);
    $('room-rows').append(row);
    updateEmptyStates();
    return row;
  }

  function addFeed(url = '') {
    rowCounter += 1;
    const row = document.createElement('div');
    row.className = 'feed-row';
    const label = document.createElement('label');
    label.htmlFor = `feed-${rowCounter}`;
    label.textContent = '订阅源 URL';
    const input = document.createElement('input');
    input.id = label.htmlFor;
    input.type = 'url';
    input.value = url;
    input.placeholder = 'https://…';
    input.autocomplete = 'off';
    input.setAttribute('aria-label', `订阅源 URL ${rowCounter}`);
    row.append(label, input, removeButton('删除订阅源', () => row.remove()));
    $('feed-rows').append(row);
    updateEmptyStates();
    return input;
  }

  function renderStatus(snapshot) {
    const configured = snapshot.rooms.length > 0 || snapshot.sources.length > 0;
    $('running-label').textContent = !configured ? '待配置' : snapshot.running ? '运行中' : '已停止';
    $('running-dot').className = `dot ${snapshot.running && configured ? 'green' : 'amber'}`;
    $('binding-label').textContent = snapshot.settings.target_umo ? '已绑定推送会话' : '尚未绑定推送会话';
    $('binding-label').title = snapshot.settings.target_umo;
    $('pending-count').textContent = snapshot.pending_messages;
    $('updated-at').textContent = `更新时间 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
    const detailsOpen = $('source-status').querySelector('details')?.open || false;
    $('source-status').replaceChildren();
    const details = document.createElement('details');
    details.open = detailsOpen;
    const summary = document.createElement('summary');
    summary.textContent = `订阅检查状态（${snapshot.sources.length} 项）`;
    details.append(summary);
    snapshot.sources.forEach((source) => {
      const line = document.createElement('div');
      line.textContent = `${source.name} · ${source.status}`;
      details.append(line);
    });
    if (snapshot.sources.length) $('source-status').append(details);
    document.querySelectorAll('#room-rows tr').forEach((row) => {
      const room = row.querySelector('[data-room]').value.trim();
      const source = snapshot.rooms.find((entry) => entry.room_id === room);
      const cell = row.querySelector('.room-status');
      cell.textContent = source ? `${source.status} · ${source.messages} 条` : '待应用';
      cell.className = `room-status ${source?.status === '已连接' ? 'good' : 'warn'}`;
    });
    $('conflict').hidden = !loadedRevision || snapshot.revision === loadedRevision;
    $('runtime-warnings').textContent = snapshot.warnings.join('\n');
    $('runtime-warnings').hidden = !snapshot.warnings.length;
    if (!busy) $('test-send').disabled = !snapshot.settings.target_umo;
  }

  function loadForm(snapshot) {
    state = snapshot;
    loadedRevision = snapshot.revision;
    const settings = snapshot.settings;
    $('room-rows').replaceChildren();
    settings.rooms.forEach((entry) => { const [room, uid] = entry.split(':'); addRoom(room, uid); });
    $('feed-rows').replaceChildren();
    settings.feed_urls.forEach((url) => addFeed(url));
    listFields.forEach((field) => { $(field).value = settings[field].join(field === 'extra_uids' ? ', ' : '\n'); });
    [...textFields, ...numberFields].forEach((field) => { $(field).value = settings[field]; });
    switchFields.forEach((field) => { $(field).checked = settings[field]; });
    clearSecrets.clear();
    secretFields.forEach((field) => {
      $(field).value = '';
      $(field).placeholder = snapshot.secrets_configured[field] ? '已配置，留空沿用' : '可选';
      document.querySelector(`[data-clear-secret="${field}"]`).textContent = '清除已保存值';
    });
    dirty = false;
    $('save-hint').textContent = '保存后重新连接事件源并应用订阅。';
    updateEmptyStates();
    renderStatus(snapshot);
  }

  async function api(method, endpoint, body) {
    let timer;
    try {
      return await Promise.race([
        method === 'GET' ? bridge.apiGet(endpoint) : bridge.apiPost(endpoint, body),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('管理面板请求超时，请检查 AstrBot 连接')), 20000); }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  function collectSettings() {
    const settings = {};
    settings.rooms = Array.from(document.querySelectorAll('#room-rows tr')).flatMap((row) => {
      const room = row.querySelector('[data-room]').value.trim();
      const uid = row.querySelector('[data-uid]').value.trim();
      if (!room && !uid) return [];
      if (!room || !uid) throw new Error('请同时填写直播间房间号和主播 UID');
      return [`${room}:${uid}`];
    });
    settings.feed_urls = Array.from(document.querySelectorAll('#feed-rows input')).map((input) => input.value.trim()).filter(Boolean);
    listFields.forEach((field) => { settings[field] = $(field).value.split(/[\s,，]+/).filter(Boolean); });
    textFields.forEach((field) => { settings[field] = $(field).value.trim(); });
    numberFields.forEach((field) => {
      if (!$(field).value.trim()) throw new Error('请填写轮询间隔和礼物最低金额');
      settings[field] = Number($(field).value);
    });
    switchFields.forEach((field) => { settings[field] = $(field).checked; });
    secretFields.forEach((field) => {
      if ($(field).value.trim()) settings[field] = $(field).value.trim();
      else if (clearSecrets.has(field)) settings[field] = null;
    });
    return settings;
  }

  $('settings').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !state) return;
    try {
      const settings = collectSettings();
      setBusy(true);
      message('正在保存并应用设置。');
      const snapshot = await api('POST', 'config', { revision: loadedRevision, settings });
      loadForm(snapshot);
      message('已保存并应用设置。', 'success');
    } catch (error) {
      message(error.message || '保存失败，请稍后重试', 'error');
    } finally {
      setBusy(false);
    }
  });

  $('settings').addEventListener('input', markDirty);
  $('settings').addEventListener('change', markDirty);
  $('add-room').addEventListener('click', () => { const row = addRoom(); row.querySelector('input').focus(); markDirty(); });
  $('add-feed').addEventListener('click', () => { addFeed().focus(); markDirty(); });
  secretFields.forEach((field) => {
    const clearButton = document.querySelector(`[data-clear-secret="${field}"]`);
    clearButton.addEventListener('click', () => {
      $(field).value = '';
      clearSecrets.add(field);
      clearButton.textContent = '保存后清除';
      markDirty();
    });
    $(field).addEventListener('input', () => {
      clearSecrets.delete(field);
      clearButton.textContent = '清除已保存值';
      clearButton.disabled = !state?.secrets_configured[field] && !$(field).value;
    });
  });

  async function refresh(reloadForm = false) {
    if (busy) return;
    try {
      const snapshot = await api('GET', 'state');
      state = snapshot;
      if (reloadForm) { loadForm(snapshot); setBusy(false); message('已重新载入设置。', 'success'); }
      else renderStatus(snapshot);
    } catch (error) {
      message(error.message || '状态读取失败', 'error');
    }
  }
  $('refresh').addEventListener('click', () => refresh());
  $('reload-settings').addEventListener('click', () => refresh(true));
  $('test-send').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api('POST', 'test', {});
      message(result.message, 'success');
    } catch (error) {
      message(error.message || '测试发送失败', 'error');
    } finally { setBusy(false); }
  });

  async function poll() {
    if (!document.hidden) await refresh();
    pollTimer = setTimeout(poll, 5000);
  }

  async function start() {
    setBusy(true);
    bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!bridge) {
      message('请从 AstrBot 插件详情中的“管理面板”打开此页面。', 'error');
      $('running-label').textContent = '等待 AstrBot 连接';
      return;
    }
    try {
      await Promise.race([
        bridge.ready(),
        new Promise((_, reject) => setTimeout(() => reject(new Error('等待 AstrBot 页面连接超时，请重新打开管理面板')), 12000)),
      ]);
      const snapshot = await api('GET', 'state');
      loadForm(snapshot);
      setBusy(false);
      pollTimer = setTimeout(poll, 5000);
    } catch (error) {
      message(error.message || '管理面板连接失败', 'error');
      $('running-label').textContent = '连接失败';
    }
  }

  window.addEventListener('beforeunload', (event) => {
    clearTimeout(pollTimer);
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  start();
})();

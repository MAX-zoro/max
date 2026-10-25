'use strict';
const $ = (id) => document.getElementById(id);
let loggedIn = false, currentStatus = {}, selected = new Set(), knownChannels = [];
let guildLoaded = false, accountId = null, before = null, viewingOlder = false, refreshing = false, noticeTimer;

function notice(message) {
  $('notice').textContent = message; $('notice').hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 6500);
}
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    throw new Error(data.error || '请求失败');
  }
  return data;
}
function showLogin() { loggedIn = false; $('workspace').hidden = true; $('login-view').hidden = false; }
function node(tag, text, cls) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (cls) result.className = cls;
  return result;
}
function safeLink(url, label) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return node('span', label);
    const link = node('a', label); link.href = parsed.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  } catch { return node('span', label); }
}
function formatDate(value) { return value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : ''; }

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { await api('/api/login', {password: $('password').value}); $('password').value = ''; await enter(); }
  catch (error) { notice(error.message); } finally { button.disabled = false; }
});
$('logout').onclick = async () => { try { await api('/api/logout', {}); showLogin(); } catch (error) { notice(error.message); } };
$('connect-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const token = $('token').value.trim(); $('token').value = '';
  $('connect-button').disabled = true;
  try { await api('/api/connect', {token}); guildLoaded = false; await refresh(); notice('正在连接，请稍等。'); }
  catch (error) { notice(error.message); }
  finally { $('connect-button').disabled = false; }
});
$('disconnect').onclick = async () => {
  try { await api('/api/disconnect', {}); guildLoaded = false; await refresh(); notice('已断开账号并清除保存的 token，已归档消息保留。'); }
  catch (error) { notice(error.message); }
};

async function loadGuilds() {
  const items = await api('/api/guilds'); const dropdown = $('guild'); dropdown.replaceChildren();
  for (const guild of items) { const option = node('option', guild.name); option.value = guild.id; dropdown.append(option); }
  const preferred = items.find(g => g.name.includes('币风港')) || items[0];
  if (preferred) { dropdown.value = preferred.id; await loadChannels(); }
  guildLoaded = true;
}
async function loadChannels() {
  if (!$('guild').value) return;
  knownChannels = await api(`/api/guilds/${$('guild').value}/channels`); renderChannels();
}
function renderChannels() {
  const list = $('channel-list'); list.replaceChildren();
  const query = $('channel-search').value.trim().toLowerCase();
  for (const channel of knownChannels.filter(c => c.name.toLowerCase().includes(query))) {
    const label = node('label', undefined, 'channel-option'), input = node('input');
    input.type = 'checkbox'; input.checked = selected.has(channel.id);
    input.onchange = () => { input.checked ? selected.add(channel.id) : selected.delete(channel.id); renderCount(); };
    const name = node('span', `${channel.thread ? '↳' : '#'} ${channel.name}`);
    if (channel.category) name.append(node('small', channel.category));
    label.append(input, name); list.append(label);
  }
  if (!list.children.length) list.append(node('p', '没有匹配的可读频道。帖子可稍后刷新查看。', 'muted small'));
  renderCount();
}
function renderCount() { $('selected-count').textContent = `已选择 ${selected.size} 个频道`; }
$('guild').onchange = () => loadChannels().catch(e => notice(e.message));
$('refresh-channels').onclick = () => loadChannels().catch(e => notice(e.message));
$('channel-search').oninput = renderChannels;
$('save-channels').onclick = async () => {
  $('save-channels').disabled = true;
  try { await api('/api/selection', {channels: [...selected]}); await refresh(); notice('已保存，从现在开始监听新消息。'); }
  catch (error) { notice(error.message); }
  finally { $('save-channels').disabled = currentStatus.state !== 'connected'; }
};
$('toggle-running').onclick = async () => {
  try { await api('/api/running', {running: !currentStatus.running}); await refresh(); }
  catch (error) { notice(error.message); }
};
function renderStatus(status) {
  currentStatus = status;
  const connected = status.state === 'connected';
  const states = {connected: '账号已连接', disconnected: '未连接账号', connecting: '正在连接', reconnecting: '正在重连', error: '连接异常'};
  $('connection').textContent = states[status.state] || status.state;
  $('connection').className = `badge${connected ? ' online' : ''}`;
  $('account').textContent = status.account?.name || '';
  $('guild').disabled = !connected; $('save-channels').disabled = !connected; $('refresh-channels').disabled = !connected;
  $('disconnect').disabled = status.state === 'disconnected';
  $('stat-messages').textContent = status.messages.toLocaleString(); $('stat-channels').textContent = status.channels.length;
  $('stat-files').textContent = status.files.toLocaleString(); $('stat-pending').textContent = `${status.pending_files} / ${status.failed_files}`;
  $('toggle-running').textContent = status.running ? '暂停同步' : '恢复同步';
  $('toggle-running').disabled = !status.channels.length;
  $('sync-label').textContent = status.running ? (connected ? '持续监听中 · 每 5 秒刷新' : '等待账号连接') : '同步已暂停';
  const errors = [status.error, ...status.channels.filter(c => c.error).map(c => `# ${c.name}：${c.error}`)].filter(Boolean);
  $('sync-errors').hidden = !errors.length; $('sync-errors').textContent = errors.join('\n');
  const filter = $('feed-filter'), value = filter.value;
  // Preserve archived-channel filters even when the channel is no longer selected.
  for (const channel of status.channels) {
    if (![...filter.options].some(option => option.value === channel.id)) {
      const option = node('option', `# ${channel.name}`); option.value = channel.id; filter.append(option);
    }
  }
  filter.value = value;
}
function renderMessage(message) {
  const card = node('article', undefined, 'message'); card.dataset.messageId = message.id;
  const top = node('div', undefined, 'message-top'), identity = node('div');
  identity.append(node('div', message.author, 'author'), node('div', `${message.guild_name} / # ${message.channel_name} · ${formatDate(message.created_at)}${message.edited_at ? ' · 已编辑' : ''}`, 'message-meta'));
  top.append(node('div', message.author.slice(0, 1), 'avatar'), identity, safeLink(message.url, '查看原文 ↗')); card.append(top);
  if (message.reply_to) card.append(node('p', `回复消息 ${message.reply_to}`, 'small muted'));
  if (message.content) card.append(node('div', message.content, 'message-content'));
  for (const embed of message.embeds || []) {
    const block = node('div', undefined, 'embed');
    if (embed.author?.name) block.append(node('p', embed.author.name, 'muted'));
    if (embed.title) { const heading = node('h3'); heading.append(embed.url ? safeLink(embed.url, embed.title) : node('span', embed.title)); block.append(heading); }
    if (embed.description) block.append(node('p', embed.description));
    for (const field of embed.fields || []) { const dl = node('dl'); dl.append(node('dt', field.name), node('dd', field.value)); block.append(dl); }
    if (embed.footer?.text) block.append(node('p', embed.footer.text, 'muted'));
    if (block.children.length) card.append(block);
  }
  if (message.files?.length) {
    const files = node('div', undefined, 'files');
    for (const file of message.files) {
      const item = node('div', undefined, 'file');
      if (file.path) {
        const url = `/media/${file.id}`;
        if (['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.mime)) {
          const img = node('img'); img.src = url; img.alt = file.filename; img.loading = 'lazy'; item.append(img);
        } else if (['video/mp4', 'video/webm', 'audio/mpeg', 'audio/ogg'].includes(file.mime)) {
          const media = node(file.mime.startsWith('video/') ? 'video' : 'audio'); media.src = url; media.controls = true; media.preload = 'none'; item.append(media);
        }
        const link = node('a', `↓ ${file.filename}`); link.href = `${url}?download=1`; item.append(link);
      } else {
        item.append(node('span', file.filename), node('p', file.error || '正在保存…', 'muted'));
      }
      files.append(item);
    }
    card.append(files);
  }
  return card;
}
async function loadMessages(append = false) {
  const params = new URLSearchParams(); if ($('feed-filter').value) params.set('channel', $('feed-filter').value);
  if (append && before) params.set('before', before);
  const data = await api(`/api/messages?${params}`); const list = $('messages');
  const existing = new Map([...list.children].filter(child => child.dataset.messageId).map(child => [child.dataset.messageId, child]));
  const incoming = [];
  for (const message of data.items) {
    const fingerprint = JSON.stringify(message);
    let card = existing.get(message.id);
    if (!card || card.dataset.fingerprint !== fingerprint) {
      card = renderMessage(message); card.dataset.fingerprint = fingerprint;
    }
    incoming.push(card);
  }
  if (append) { for (const card of incoming) list.append(card); }
  else {
    const keep = new Set(incoming);
    for (const child of [...list.children]) if (!keep.has(child)) child.remove();
    incoming.forEach((card, index) => { if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null); });
  }
  if (!list.children.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('div', '↘', 'empty-icon'), node('h2', '等一条新消息'), node('p', '连接账号并选择频道后，新消息会自动出现在这里。'));
    list.append(empty);
  }
  before = data.next_before; $('load-more').hidden = !before;
}
$('load-more').onclick = async () => { viewingOlder = true; try { await loadMessages(true); } catch (e) { notice(e.message); } };
$('latest').onclick = async () => { viewingOlder = false; try { await loadMessages(); } catch (e) { notice(e.message); } };
$('feed-filter').onchange = () => { viewingOlder = false; loadMessages().catch(e => notice(e.message)); };
async function refresh() {
  if (refreshing || !loggedIn) return; refreshing = true;
  try {
    const status = await api('/api/status'); renderStatus(status);
    if (status.account?.id && status.account.id !== accountId) {
      accountId = status.account.id; guildLoaded = false; selected = new Set(status.channels.map(c => c.id));
    }
    if (status.state === 'connected' && !guildLoaded) await loadGuilds();
    if (!viewingOlder) await loadMessages();
  } finally { refreshing = false; }
}
async function enter() {
  const status = await api('/api/status'); selected = new Set(status.channels.map(c => c.id));
  guildLoaded = false; accountId = status.account?.id || null;
  loggedIn = true; $('login-view').hidden = true; $('workspace').hidden = false;
  renderStatus(status); await refresh();
}
enter().catch(() => showLogin());
setInterval(() => { if (loggedIn) refresh().catch(error => notice(error.message)); }, 5000);

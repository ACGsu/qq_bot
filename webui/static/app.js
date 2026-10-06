import {durationSeconds as readDurationSeconds, convertDuration} from './duration.mjs';

const $ = (selector) => document.querySelector(selector);
const labels = {overview: ['运行概览', '管理插件与容器，让每次配置更清晰。'], plugins: ['插件管理', '按需启用，独立配置。所有修改保存后才会生效。'], logs: ['运行日志', '观察容器输出，定位问题。日志不代表 QQ 连接状态。']};
const configurable = new Set(['rps', 'auto_emoji', 'daily_wife', 'timetable', 'summary']);
let csrf = '', config = null, edits = {}, enabled = [], tags = [], page = 'overview';
let dirty = false, busy = false, operationPending = false, polling = false, rawLogs = '', activePlugin = null;
let duration = {value: '60', unit: 'seconds'}, durationChanged = false;

async function api(path, method = 'GET', data) {
  const response = await fetch(`/api/${path}`, {method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== 'login') showLogin();
    throw new Error(result.detail || '请求失败，请重试');
  }
  return result;
}
function message(text, error = false) { $('#notice').hidden = false; $('#notice').textContent = text; $('#notice').classList.toggle('error', error); }
function setState(text) { $('#save-state').textContent = text; }
function markDirty() { dirty = true; setState('有未保存的修改'); updateCount(); }
function updateCount() { $('#enabled-count').textContent = `${config ? config.definitions.filter(p => enabled.includes(p.plugin_id)).length : 0} / 9`; }
function fieldValue(plugin, key, fallback = '') { return edits.plugin_settings?.[plugin]?.[key] ?? config.plugin_settings[plugin]?.[key] ?? fallback; }
function bool(value) { return ['true', '1', 'yes', 'on'].includes(String(value).toLowerCase()); }
function change(plugin, key, value) { (edits.plugin_settings[plugin] ??= {})[key] = value; markDirty(); renderCards(); }
function showLogin() { $('#workspace').hidden = true; $('#login-screen').hidden = false; $('#settings-dialog').close(); csrf = ''; config = null; }
async function loadConfig() {
  config = await api('config'); enabled = [...config.enabled_plugins]; edits = {plugin_settings: {}}; tags = structuredClone(config.emoji); dirty = false;
  duration = {value: config.plugin_settings.rps?.ban_seconds ?? '60', unit: 'seconds'}; durationChanged = false;
  renderCards(); updateCount(); setState(config.application); controls();
}
async function start() {
  $('#login-screen').hidden = true; $('#workspace').hidden = false; $('#notice').hidden = true;
  try { await loadConfig(); } catch (error) { message(error.message, true); setState('配置读取失败，禁止保存'); controls(); }
  await refresh();
}
function node(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
function button(text, onClick, className = 'quiet') { const b = node('button', text, className); b.type = 'button'; b.addEventListener('click', onClick); return b; }
function renderCards() {
  if (!config) return;
  const grid = $('#plugin-grid'); grid.replaceChildren();
  const search = $('#search').value.toLowerCase(), filter = $('#filter').value;
  for (const plugin of config.definitions) {
    const id = plugin.plugin_id, on = enabled.includes(id);
    if (!`${plugin.name} ${plugin.description} ${plugin.commands.join(' ')}`.toLowerCase().includes(search) || (filter === 'enabled' && !on) || (filter === 'disabled' && on)) continue;
    const changed = !!edits.plugin_settings[id] || on !== config.enabled_plugins.includes(id) || (id === 'auto_emoji' && edits.retained_emoji_ids !== undefined) || (id === 'rps' && durationChanged);
    const card = node('article', undefined, `plugin-card${changed ? ' changed' : ''}`);
    const top = node('div', undefined, 'plugin-top'); top.append(node('h3', plugin.name));
    const toggle = node('label', undefined, 'toggle'), checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = on; checkbox.setAttribute('aria-label', `启用${plugin.name}`); checkbox.disabled = busy || operationPending;
    checkbox.addEventListener('change', () => { enabled = checkbox.checked ? [...new Set([...enabled, id])] : enabled.filter(x => x !== id); markDirty(); renderCards(); });
    toggle.append(checkbox, node('span', on ? '已启用' : '已关闭')); top.append(toggle); card.append(top, node('p', plugin.description, 'description'));
    const commands = node('div', undefined, 'commands'); plugin.commands.forEach(cmd => commands.append(node('code', cmd))); card.append(commands);
    const bottom = node('div', undefined, 'plugin-bottom'); bottom.append(node('span', changed ? '● 未保存' : ''));
    if (configurable.has(id)) bottom.append(button('配置插件 ↗', () => openSettings(plugin))); else bottom.append(node('small', '无需额外配置'));
    card.append(bottom); grid.append(card);
  }
  $('#no-results').hidden = grid.childElementCount !== 0;
}
function addField(fieldset, title, key, plugin, {type = 'text', fallback = '', placeholder = '', note = ''} = {}) {
  const field = node('div', undefined, 'field'), label = node('label', title), input = document.createElement('input'); input.type = type;
  input.id = `setting-${plugin}-${key}`; label.htmlFor = input.id; input.value = fieldValue(plugin, key, fallback); input.placeholder = placeholder; input.autocomplete = type === 'password' ? 'new-password' : 'off';
  input.addEventListener('input', () => change(plugin, key, input.value)); field.append(label, input); if (note) field.append(node('small', note)); fieldset.append(field); return input;
}
function addCheck(fieldset, title, key, plugin, note) {
  const field = node('div', undefined, 'field'), label = node('label', undefined, 'inline'), input = document.createElement('input'); input.type = 'checkbox'; input.checked = bool(fieldValue(plugin, key, 'true'));
  input.addEventListener('change', () => { if (key === 'group_isolation_enabled' && !input.checked && !confirm('关闭群聊隔离后，同一 QQ 的课表可跨群共享，其他共同群成员也可查阅。确认关闭？')) { input.checked = true; return; } change(plugin, key, String(input.checked)); });
  label.append(input, node('span', title)); field.append(label, node('small', note)); fieldset.append(field);
}
function syncTags() {
  edits.retained_emoji_ids = tags.filter(t => config.emoji.some(old => old.id === t.id)).map(t => t.id);
  edits.emoji_text = tags.filter(t => !config.emoji.some(old => old.id === t.id)).map(t => t.label).join(''); markDirty(); renderCards();
}
function renderTags(parent) {
  parent.replaceChildren();
  tags.forEach(t => { const tag = node('span', undefined, `tag${t.legacy ? ' legacy' : ''}`); tag.append(node('span', t.label), button('×', () => { tags = tags.filter(x => x.id !== t.id); syncTags(); renderTags(parent); })); tag.lastChild.setAttribute('aria-label', `删除${t.label}`); parent.append(tag); });
}
function durationSeconds() { return readDurationSeconds(duration); }

function openSettings(plugin) {
  activePlugin = plugin.plugin_id; const id = activePlugin;
  $('#settings-title').textContent = plugin.name; $('#settings-description').textContent = plugin.description;
  const body = $('#settings-body'); body.replaceChildren();
  if (!enabled.includes(id)) body.append(node('p', '插件已关闭，设置保留。重新启用后可编辑。', 'callout'));
  const fs = document.createElement('fieldset'); fs.disabled = !enabled.includes(id) || busy || operationPending; body.append(fs);
  if (id === 'rps') {
    const field = node('div', undefined, 'field'), label = node('label', '输家禁言时长'), row = node('div', undefined, 'duration-row');
    const input = document.createElement('input'); input.type = 'text'; input.inputMode = 'decimal'; input.maxLength = 32; input.value = duration.value; input.id = 'ban-duration'; label.htmlFor = input.id;
    const select = document.createElement('select'); select.setAttribute('aria-label', '禁言时长单位'); for (const [value, text] of [['seconds','秒'],['minutes','分钟']]) { const o = node('option', text); o.value = value; select.append(o); } select.value = duration.unit;
    const summary = node('small');
    function update(next) { duration = next ?? {value: input.value, unit: select.value}; durationChanged = true; markDirty(); renderCards(); showDuration(); }
    function showDuration() { try { const seconds = durationSeconds(); summary.textContent = `最终时长：${Number(seconds) / 60} 分钟（${seconds} 秒）`; summary.className = ''; } catch (e) { summary.textContent = e.message; summary.className = 'error-text'; } }
    input.addEventListener('input', () => update()); select.addEventListener('change', () => { let next; try { next = convertDuration(duration, select.value); input.value = next.value; } catch {} update(next); }); row.append(input, select); field.append(label, row, summary, node('small', '范围 1 秒～24 小时，默认 60 秒。机器人必须具有群管理权限；应用成功不保证禁言成功。')); fs.append(field); showDuration();
  } else if (id === 'auto_emoji') {
    addField(fs, '目标 QQ', 'target_qq', id, {placeholder: '留空继承环境配置', note: '关闭插件不会清空目标 QQ。留空沿用机器人环境变量。'});
    const title = node('label', '要贴的表情'), input = document.createElement('input'); title.htmlFor = 'emoji-input'; input.id = 'emoji-input'; input.placeholder = '粘贴 😀 😂 👍 ❤️ 🔥 等表情'; input.maxLength = 1000;
    const tagList = node('div', undefined, 'tags'), error = node('small', '', 'error-text');
    async function add(text) { if (busy || operationPending) return; busy = true; controls(); try { const result = await api('emoji/preview', 'POST', {text}); for (const t of result.emoji) if (!tags.some(x => x.id === t.id)) tags.push(t); syncTags(); renderTags(tagList); input.value = ''; error.textContent = ''; } catch (e) { error.textContent = e.message; } finally { busy = false; controls(); } }
    const actions = node('div', undefined, 'emoji-buttons'); for (const emoji of ['😀', '😂', '👍', '❤️', '🔥', '🎉', '🍬', '㊗️']) actions.append(button(emoji, () => add(emoji))); actions.append(button('解析并添加', () => add(input.value), 'secondary'));
    fs.append(title, input, actions, error, tagList, node('small', `${config.emoji_source}。支持粘贴单码点 Unicode 表情（如 😀 😂 👍 ❤️ 🔥），不限于快捷按钮；自动转换为回应 ID。实际可用性取决于 QQ／NapCat。不支持肤色、旗帜和家庭等组合或 QQ 专属表情。未知旧版标签仅保留，不展示编码。`)); renderTags(tagList);
  } else if (id === 'daily_wife') addCheck(fs, '启用结芬子功能', 'marriage_enabled', id, '关闭后仅保留每日抽取，不处理结芬及其回应；主插件关闭不改变此偏好。');
  else if (id === 'timetable') addCheck(fs, '启用群聊隔离', 'group_isolation_enabled', id, '默认各群独立。关闭后跨群共享同一 QQ 最近更新的课表，注意隐私影响。');
  else if (id === 'summary') {
    addField(fs, 'DeepSeek API Key', 'deepseek_api_key', id, {type:'password', placeholder: config.api_key_set ? '已设置，留空保留' : '未设置，输入新 Key', note: `来源：${config.api_key_source}。密钥不会回传。`});
    const clear = node('label', undefined, 'inline'), check = document.createElement('input'); check.type = 'checkbox'; check.checked = edits.clear_api_key ?? false; check.addEventListener('change', () => { edits.clear_api_key = check.checked; markDirty(); }); clear.append(check, node('span', '显式清除 JSON 中的 API Key')); fs.append(clear, node('small', '若机器人 .env 中仍有 Key，清除 JSON 后会继承该值；WebUI 不修改 .env。'));
    addField(fs, 'API 地址', 'deepseek_api_base_url', id, {placeholder:'继承环境／https://api.deepseek.com', note:'自定义地址会接收 API Key，请仅使用信任的服务。'});
    addField(fs, '模型', 'deepseek_model', id, {placeholder:'继承环境／deepseek-chat'});
  }
  $('#settings-dialog').showModal();
}
function controls() {
  const disabled = busy || operationPending;
  document.querySelectorAll('.plugin-card input[type=checkbox]').forEach(input => { input.disabled = disabled || !config; });
  document.querySelectorAll('.plugin-bottom button').forEach(button => { button.disabled = disabled || !config; });
  $('#reload').disabled = disabled;
  for (const id of ['save','apply','validate','enable-all','disable-all']) $(`#${id}`).disabled = disabled || !config;
  $('[data-action="start"]').disabled = disabled; $('[data-action="stop"]').disabled = disabled; $('[data-action="build"]').disabled = disabled;
  if ($('#settings-body fieldset')) $('#settings-body fieldset').disabled = disabled || !config || !enabled.includes(activePlugin);
}
function payload() { if (!config) throw new Error('配置尚未正确读取'); const data = structuredClone(edits); data.version = config.version; data.enabled_plugins = enabled; if (durationChanged) (data.plugin_settings.rps ??= {}).ban_seconds = durationSeconds(); return data; }
async function save(mode) {
  if (busy || operationPending) return;
  if (mode === 'apply' && !confirm('保存并重新创建机器人容器（不构建镜像）。部分内存会话和每日记录将丢失，持久化课表数据保留。确认应用？')) return;
  busy = true; controls();
  try {
    const data = payload();
    if (mode === 'validate') { await api('config/validate','POST',data); message('配置校验通过，尚未保存，也未操作容器。'); }
    else if (mode === 'save') { await api('config','PUT',data); await loadConfig(); setState('已保存待应用'); message('配置已保存，机器人容器未变动。需要生效时请选择保存并应用。'); }
    else { const task = await api('config/apply','POST',data); operationPending = true; await loadConfig(); setState('已保存，正在应用…'); message(`任务已提交：${task.id.slice(0,8)}。正在重新创建并验证容器配置。`); await refresh(); }
  } catch (e) { message(e.message, true); }
  finally { busy = false; controls(); renderCards(); }
}
async function refresh() {
  if (!csrf || polling) return;
  polling = true;
  try {
    const [status, result] = await Promise.all([api('status'), api('tasks')]);
    $('#docker-state').textContent = status.available ? '可用' : '不可用';
    $('#container-state').textContent = status.available ? status.containers.map(x => x.state).join(' / ') || '未创建' : '未知';
    const wasPending = operationPending; operationPending = result.tasks.some(t => ['queued','running'].includes(t.state));
    if (wasPending && !operationPending) message(status.application, result.tasks[0]?.state === 'failed');
    if (!busy) setState(!config ? '配置读取失败，禁止保存' : dirty ? (operationPending ? '容器任务执行中 · 有未保存的修改' : '有未保存的修改') : (operationPending ? '容器任务执行中…' : status.application));
    const list = $('#tasks'); list.replaceChildren(); list.className = result.tasks.length ? '' : 'empty'; if (!result.tasks.length) list.textContent = '暂无容器操作';
    const actionLabels = {start:'启动',stop:'停止',build:'构建并启动',apply:'保存并应用'};
    result.tasks.forEach(t => { const row = node('article', undefined, 'task'), top = node('div', undefined, 'task-header'); top.append(node('strong', `${actionLabels[t.action]} · ${t.phase}`), node('small', new Date(t.created * 1000).toLocaleTimeString())); row.append(top); if (t.output) row.append(node('pre',t.output)); list.append(row); }); controls();
  } catch (e) { message(e.message, true); }
  finally { polling = false; }
}
function renderLogs() { const term = $('#log-search').value.toLowerCase(); $('#log-output').textContent = rawLogs.split('\n').filter(line => line.toLowerCase().includes(term)).join('\n') || '暂无匹配日志'; if (!$('#log-pause').checked) $('#log-output').scrollTop = $('#log-output').scrollHeight; }
async function logs() { if (!csrf) return; try { rawLogs = (await api('logs')).text; renderLogs(); } catch(e) { message(e.message,true); } }
$('#login-form').addEventListener('submit', async e => { e.preventDefault(); const b = $('#login-form button'); b.disabled = true; try { csrf = (await api('login','POST',{password:$('#password').value})).csrf; $('#password').value = ''; $('#login-error').textContent = ''; await start(); } catch(err) { $('#login-error').textContent = err.message; } finally { b.disabled = false; } });
$('#logout').addEventListener('click', async () => { if (dirty && !confirm('存在未保存修改，仍要退出登录？')) return; try { await api('logout','POST',{}); dirty = false; showLogin(); } catch(e) { message(e.message,true); } });
$('#reload').addEventListener('click', async () => { if (busy || operationPending || (dirty && !confirm('重新加载会丢弃未保存修改。继续？'))) return; busy = true; controls(); try { await loadConfig(); message('已重新加载磁盘配置。'); } catch(e) { config = null; setState('配置读取失败，禁止保存'); message(e.message,true); } finally { busy = false; controls(); } });
for (const item of document.querySelectorAll('[data-page]')) item.addEventListener('click', () => { page = item.dataset.page; document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== page); document.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('active', b === item)); $('#page-title').textContent = labels[page][0]; $('#page-description').textContent = labels[page][1]; if (page === 'logs') logs(); });
$('#search').addEventListener('input',renderCards); $('#filter').addEventListener('change',renderCards);
$('#enable-all').addEventListener('click', () => { enabled = [...new Set([...enabled,...config.definitions.map(p=>p.plugin_id)])]; markDirty(); renderCards(); });
$('#disable-all').addEventListener('click', () => { if (confirm('全部关闭后，应用配置将停用所有插件。设置仍保留，确认？')) { enabled = enabled.filter(id => !config.definitions.some(p => p.plugin_id === id)); markDirty(); renderCards(); } });
function canCloseSettings() { const input = $('#emoji-input'); return !busy && (!input?.value.trim() || confirm('输入的表情尚未解析添加，仍关闭设置面板？')); }
for (const id of ['close-settings','done-settings']) $(`#${id}`).addEventListener('click', () => { if (canCloseSettings()) $('#settings-dialog').close(); });
$('#settings-dialog').addEventListener('cancel', e => { if (!canCloseSettings()) e.preventDefault(); });
for (const id of ['save','apply','validate']) $(`#${id}`).addEventListener('click', () => save(id));
for (const b of document.querySelectorAll('[data-action]')) b.addEventListener('click', async () => { const action=b.dataset.action; if (action !== 'start' && !confirm(action === 'stop' ? '确认仅停止 qq-bot？WebUI 仍可访问，不删除卷或网络。' : '确认重新构建镜像并重新创建机器人？内存会话和每日记录可能丢失，持久化课表保留。未保存的页面修改不会应用。')) return; busy=true; controls(); try { await api('tasks','POST',{action}); operationPending=true; setState('容器任务执行中…'); await refresh(); } catch(e) { message(e.message,true); } finally { busy=false; controls(); } });
$('#refresh-status').addEventListener('click', refresh); $('#refresh-logs').addEventListener('click', logs); $('#log-search').addEventListener('input',renderLogs);
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
setInterval(()=>{ if(csrf){refresh();if(page==='logs'&&!$('#log-pause').checked)logs();}},5000);
try { csrf=(await api('session')).csrf; await start(); } catch { showLogin(); }

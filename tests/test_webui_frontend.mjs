// Pure state/handler regression with a minimal DOM stub; not visual acceptance.
// Run with Node.js, no npm packages or browser/network required.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {durationSeconds as readDurationSeconds, convertDuration} from '../webui/static/duration.mjs';

function element() {
  return {value:'', handlers:{}, disabled:false, textContent:'', hidden:false, children:[],
    get lastChild(){return this.children.at(-1);}, get childElementCount(){return this.children.length;},
    addEventListener(event, handler) { this.handlers[event] = handler; },
    classList:{toggle(){}, add(){}, remove(){}},
    replaceChildren(...items){this.children=[...items];}, append(...items){this.children.push(...items);}, close(){this.open=false;}, showModal(){this.open=true;}, setAttribute(){}};
}
const elements = new Map();
const get = key => { if (!elements.has(key)) elements.set(key, element()); return elements.get(key); };
const checkbox = element(), settingsButton = element();
const context = vm.createContext({
  document:{querySelector:get, querySelectorAll:selector => selector === '.plugin-card input[type=checkbox]' ? [checkbox] : selector === '.plugin-bottom button' ? [settingsButton] : [], createElement:element},
  window:{addEventListener(){}}, setInterval(){}, structuredClone,
  confirm:() => false, readDurationSeconds, convertDuration,
});
const source = readFileSync(new URL('../webui/static/app.js', import.meta.url), 'utf8');
assert.ok(source.includes("try { csrf=(await api('session'))"));
vm.runInContext(source.replace(/^import[^\n]+\n/, '').split("try { csrf=(await api('session'))")[0], context);
const run = code => vm.runInContext(code, context);
context.fetch = async url => ({ok:true, json:async () => url.endsWith('/status') ? {available:false, containers:[], application:'应用状态未知'} : {tasks:[]}});

run("csrf='fake'; config={}; dirty=false; operationPending=false; busy=false;");
get('#save-state').textContent = '已验证应用';
await run('refresh()');
assert.equal(get('#save-state').textContent, '应用状态未知');
run('dirty=true; operationPending=true;');
await run('refresh()');
assert.equal(get('#save-state').textContent, '有未保存的修改');
run('busy=true; dirty=false;');
get('#save-state').textContent = '正在保存';
await run('refresh()');
assert.equal(get('#save-state').textContent, '正在保存');
run('busy=false; config=null;');
await run('refresh()');
assert.equal(get('#save-state').textContent, '配置读取失败，禁止保存');
assert.equal(checkbox.disabled, true);
assert.equal(settingsButton.disabled, true);

run("config={}; enabled=['rps']; activePlugin='rps'; operationPending=false; controls();");
assert.equal(checkbox.disabled, false);
run('operationPending=true; controls();');
assert.equal(get('#reload').disabled, true);
run('operationPending=false;');
let resolveFetch;
context.fetch = () => new Promise(resolve => { resolveFetch=resolve; });
const loading = get('#reload').handlers.click();
assert.equal(checkbox.disabled, true);
assert.equal(settingsButton.disabled, true);
resolveFetch({ok:false, status:422, json:async()=>({detail:'配置读取失败'})});
await loading;
assert.equal(get('#save').disabled, true);
assert.equal(checkbox.disabled, true);
assert.equal(run('busy'), false);

get('#emoji-input').value = '尚未解析';
assert.equal(run('canCloseSettings()'), false);
let cancelled=false;
get('#settings-dialog').handlers.cancel({preventDefault(){cancelled=true;}});
assert.equal(cancelled, true);
context.confirm=()=>true;
assert.equal(run('canCloseSettings()'), true);
run('busy=true;');
assert.equal(run('canCloseSettings()'), false);
console.log('Frontend state regression passed: stale application status, dirty state, reload locks, corrupt config, Escape confirmation.');

// New Unicode tags must remain editable, preserve old IDs, and serialize as characters.
run("busy=false; config={emoji:[{id:'99999',label:'旧版表情（保留）',legacy:true}], definitions:[], enabled_plugins:['auto_emoji']}; enabled=['auto_emoji']; edits={plugin_settings:{}}; tags=[...config.emoji,{id:'128512',label:'😀',legacy:false},{id:'10084',label:'❤',legacy:false}]; syncTags();");
assert.equal(run('edits.emoji_text'), '😀❤');
assert.deepEqual(Array.from(run('edits.retained_emoji_ids')), ['99999']);
run("tags=tags.filter(t=>t.id!=='128512'); syncTags();");
assert.equal(run('edits.emoji_text'), '❤');
console.log('Extended Unicode emoji draft and legacy retention regression passed.');

// Card headings share the top row with toggles, without the old initial tile.
run("config={definitions:[{plugin_id:'auto_emoji',name:'自动贴表情',description:'测试说明',commands:['被动监听']},{plugin_id:'rps',name:'猜拳',description:'测试说明',commands:['/猜拳']}],enabled_plugins:['auto_emoji']}; enabled=['auto_emoji']; edits={plugin_settings:{}}; busy=false; operationPending=false; renderCards();");
const cards = get('#plugin-grid').children;
assert.equal(cards.length, 2);
for (const [index, card] of cards.entries()) {
  const top = card.children[0];
  assert.equal(top.className, 'plugin-top');
  assert.equal(top.children.length, 2);
  assert.equal(top.children[0].textContent, index === 0 ? '自动贴表情' : '猜拳');
  assert.equal(top.children[1].className, 'toggle');
  assert.equal(top.children[1].children[0].checked, index === 0);
  assert.equal(card.children.at(-1).className, 'plugin-bottom');
}
assert.ok(!source.includes('plugin-icon'));
console.log('Plugin card heading/toggle layout structure regression passed.');

// Migrated GUI behavior: child defaults, disabled parents and bulk toggles.
for (const [id, key] of [['daily_wife', 'marriage_enabled'], ['timetable', 'group_isolation_enabled']]) {
  run(`config={definitions:[{plugin_id:'${id}',name:'test',description:'test',commands:[]}],plugin_settings:{},version:'test'}; enabled=['${id}']; edits={plugin_settings:{}}; busy=false; operationPending=false; durationChanged=false;`);
  run('openSettings(config.definitions[0])');
  let fieldset = get('#settings-body').children.at(-1);
  assert.equal(fieldset.disabled, false);
  let child = fieldset.children[0].children[0].children[0];
  assert.equal(child.checked, true, 'legacy configuration defaults child to enabled');
  child.checked = false;
  context.confirm = () => true;
  child.handlers.change();
  assert.equal(run(`edits.plugin_settings.${id}.${key}`), 'false');
  get('#disable-all').handlers.click();
  run('openSettings(config.definitions[0])');
  fieldset = get('#settings-body').children.at(-1);
  assert.equal(fieldset.disabled, true);
  assert.equal(fieldset.children[0].children[0].children[0].checked, false);
  get('#enable-all').handlers.click();
  run('openSettings(config.definitions[0])');
  fieldset = get('#settings-body').children.at(-1);
  assert.equal(fieldset.disabled, false);
  assert.equal(fieldset.children[0].children[0].children[0].checked, false);
  assert.equal(run(`payload().plugin_settings.${id}.${key}`), 'false');
}
console.log('Migrated child-setting defaults, disabled-state and bulk-toggle preservation passed.');

// Migrated child-setting coverage from the retired Tkinter interface.
for (const [id, key] of [['daily_wife','marriage_enabled'], ['timetable','group_isolation_enabled']]) {
  context.childId = id;
  context.childKey = key;
  run("config={version:'test', definitions:[{plugin_id:childId,name:childId,description:'',commands:[]}],plugin_settings:{},emoji:[]}; enabled=[childId]; edits={plugin_settings:{}}; busy=false; operationPending=false; durationChanged=false; activePlugin=childId;");
  const open = () => {
    run('openSettings(config.definitions[0])');
    const fs = get('#settings-body').children.at(-1);
    return {fs, input:fs.children[0].children[0].children[0]};
  };
  assert.equal(open().input.checked, true, 'legacy missing child setting defaults on');
  for (const value of [true, 'true', false, 'false']) {
    context.childValue = value;
    run('config.plugin_settings[childId]={[childKey]:childValue}');
    assert.equal(open().input.checked, value === true || value === 'true');
  }
  const input = open().input;
  input.checked = true;
  input.handlers.change();
  assert.equal(run('edits.plugin_settings[childId][childKey]'), 'true');
  get('#disable-all').handlers.click();
  assert.equal(open().fs.disabled, true);
  assert.equal(open().input.checked, true, 'disabled parent retains child preference');
  get('#enable-all').handlers.click();
  assert.equal(open().fs.disabled, false);
  assert.equal(open().input.checked, true);
  assert.equal(run('payload().plugin_settings[childId][childKey]'), 'true');
}
console.log('Migrated child-setting regression passed: defaults, boolean compatibility, editing, parent bulk toggles and payload retention.');

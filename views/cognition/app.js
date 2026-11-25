import { renderWorld } from './people.js';
const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView;
let scopes = [],
    schema = {};
let scopeRequest = 0, overviewRequest = 0;
let memoryView, experienceLimit = 8;
let settingsRequest;
let savedSettings = {};
const sessionSelections = new Map();
const sessionKinds = {FriendMessage: '私聊', GroupMessage: '群聊'};
const labels = {
    fact: '事实',
    preference: '偏好',
    episode: '经历',
    lesson: '相处经验',
    self: '自我认识'
};
const el = (tag, content, className) => {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    if (className) node.className = className;
    return node;
};

function notice(message, error = false) {
    $('notice').textContent = message;
    $('notice').classList.toggle('error', error);
}
async function action(task) {
    try {
        await task();
    } catch (error) {
        notice(error.message || '操作失败，请检查 AstrBot 日志。', true);
    }
}

function empty(container, message) {
    container.replaceChildren(el('p', message, 'empty'));
}

function confirmDeletion(message) {
    const dialog = $('confirm-action');
    $('confirm-message').textContent = message;
    dialog.returnValue = 'cancel';
    dialog.showModal();
    return new Promise(resolve => {
        dialog.addEventListener('close', () => resolve(dialog.returnValue === 'delete'), {once: true});
    });
}

function selected() {
    return $('scope').value;
}

async function loadScope() {
    const scope = selected();
    const request = ++scopeRequest;
    $('scope-details').hidden = true;
    $('scope-empty').hidden = Boolean(scope);
    $('search').disabled = !scope;
    $('scope-loading').hidden = !scope;
    $('scope-loading').textContent = scope ? '正在读取这段会话的记忆…' : '';
    $('memory').setAttribute('aria-busy', String(Boolean(scope)));
    if (!scope) return true;
    let data;
    try {
        data = await bridge.apiGet('scope', {scope, query: $('query').value});
    } catch (error) {
        if (request === scopeRequest) {
            $('scope-loading').textContent = '暂时没能读取记录，请点击“刷新认知”重试。';
            $('memory').setAttribute('aria-busy', 'false');
            notice(error.message || '读取失败', true);
        }
        return false;
    }
    if (request !== scopeRequest) return false;
    $('scope-loading').hidden = true;
    $('memory').setAttribute('aria-busy', 'false');
    const state = scopes.find(s => s.id === scope),
        stats = state?.stats || {};
    $('stats').replaceChildren();
    for (const [label, value] of [
            ['认识的人物', data.entities.length],
            ['人物间关系', data.relations.length],
            ['待整理', stats.pending || 0],
            ['运行状态', state?.paused ? '已暂停' : stats.failed ? `${stats.failed} 项失败` : '正常']
        ]) {
        const node = el('div', undefined, 'stat');
        node.append(el('span', label), el('strong', String(value)));
        $('stats').append(node);
    }
    renderWorld(data, scope);
    $('questions').replaceChildren(); // Dashboard shows questions for all actors, not just a fake empty sender.
    (data.all_questions || data.curiosity).forEach(q => $('questions').append(el('div', q.question, 'question')));
    if (!$('questions').children.length) empty($('questions'), '没有待解的问题，也可以安静地陪伴。');
    memoryView = {data, scope};
    renderMemories();
    renderExperiences();
    $('character').replaceChildren();
    const character = data.character || {};
    for (const [key, label] of [['values','一直在意'],['preferences','自己的喜好'],['habits','相处习惯'],['interests','最近兴趣'],['outlook','近来想法']]) {
        const value = character[key];
        if (value && (!Array.isArray(value) || value.length)) $('character').append(el('p', `${label}：${Array.isArray(value) ? value.join('；') : value}`));
    }
    $('intentions').replaceChildren();
    const kinds = {care:'关心',promise:'约定',share:'想分享',play:'想一起玩',explore:'想探索',ask:'好奇'};
    (data.intentions || []).forEach(thought => {
        const node = el('div', undefined, 'question');
        node.append(el('span', kinds[thought.kind] || '挂念', 'tag'), el('p', thought.thought),
            el('small', thought.status === 'pending' ? (thought.occasion || '等合适的时候') : '已经提起，先等对方回应'));
        $('intentions').append(node);
    });
    if (!$('character').children.length && !$('intentions').children.length) empty($('character'), '自己的喜好、习惯和挂念会随着经历延续。');
    $('scope-details').hidden = false;
}
function renderMemories() {
    if (!memoryView || memoryView.scope !== selected()) return;
    $('memories').replaceChildren();
    const {data, scope} = memoryView;
    const rows = data.memories.filter(row => !$('memory-kind').value || row.kind === $('memory-kind').value);
    $('memory-count').textContent = `本次相关记忆 ${rows.length} / ${data.memories.length} 条`;
    rows.forEach(r => {
        const node = el('div', undefined, 'memory');
        node.append(el('span', labels[r.kind] || r.kind, 'tag'), el('p', r.content));
        const detail = el('details');
        detail.append(el('summary', `#${r.id} · ${Math.round(r.confidence*100)}% 置信度 · 查看依据`), el('div', `原文：${r.evidence}`), el('div', `来源：${r.source}`), el('div', `更新：${new Date(r.updated*1000).toLocaleString()}`));
        const remove = el('button', '忘记这条');
        remove.addEventListener('click', () => action(async () => {
            if (!await confirmDeletion('将删除此事实的全部修订和此会话人格的待整理原文。派生画像、别称、经历与挂念也会重置。')) return;
            await bridge.apiPost('forget', {
                scope,
                id: r.id
            });
            await refresh();
            notice('已删除该事实及其历史修订。');
        }));
        node.append(detail, remove);
        $('memories').append(node);
    });
    if (!rows.length) empty($('memories'), '暂时没有相关回忆。换个关键词，或先聊一会儿。');
}
function renderExperiences() {
    if (!memoryView || memoryView.scope !== selected()) return;
    $('experiences').replaceChildren();
    const events = memoryView.data.experiences || [];
    $('experience-count').textContent = `本次可查看 ${events.length} 段经历`;
    $('experiences-more').hidden = experienceLimit >= events.length;
    events.slice(0, experienceLimit).forEach(event => {
        const node = el('div', undefined, 'question');
        const source = {dialogue: '共同相处', observed: '听到的交谈', companion: '自己的活动', legacy: '早先记下'};
        node.append(el('small', `${source[event.source_kind] || '一段经历'} · ${new Date(event.created * 1000).toLocaleDateString()}`),
            el('b', event.title || '一段相处'), el('p', event.story));
        if (event.feeling) node.append(el('small', event.feeling));
        $('experiences').append(node);
    });
    if (!$('experiences').children.length) empty($('experiences'), '聊过一段、一起做过些事，就会慢慢留下完整的经历。');
}
async function refresh() {
    const request = ++overviewRequest;
    ++scopeRequest;
    $('memory').setAttribute('aria-busy', 'true');
    $('scope-details').hidden = true;
    $('scope-loading').hidden = false;
    $('scope-loading').textContent = '正在刷新会话与认知记录…';
    let data;
    try { data = await bridge.apiGet('overview'); }
    catch (error) {
        if (request === overviewRequest) {
            $('scope-loading').textContent = '刷新失败，请重试。';
            $('memory').setAttribute('aria-busy', 'false');
            notice(error.message || '刷新失败', true);
        }
        return;
    }
    if (request !== overviewRequest) return;
    const old = selected();
    scopes = data.scopes;
    $('scope').replaceChildren();
    scopes.forEach(s => {
        const [platform, messageType] = s.origin.split(':', 2);
        const name = s.display_name || s.origin;
        const source = name === s.origin ? name : `${name}（${sessionKinds[messageType] || '会话'} · ${platform}）`;
        const option = el('option', `${source} · ${s.persona}`);
        option.value = s.id;
        $('scope').append(option);
    });
    if (scopes.some(s => s.id === old)) $('scope').value = old;
    if (!scopes.length) {
        const option = el('option', '尚无对话：开始聊天后自动创建');
        option.value = '';
        $('scope').append(option);
    }
    if (await loadScope() !== false) notice(data.last_error || '');
}

function sessionPicker(key, spec, values, sessions) {
    const field = el('fieldset', undefined, 'field session-field');
    const picker = el('details', undefined, 'session-picker');
    const summary = el('summary');
    const search = el('input');
    search.type = 'search';
    search.placeholder = '搜索群名、昵称、别名或平台';
    search.setAttribute('aria-label', `搜索${spec.description}`);
    const results = el('div', undefined, 'session-options');
    let visibleLimit = 50;
    const selected = new Set(values);
    sessionSelections.set(key, selected);
    const requiredType = {observation_origins: 'GroupMessage', proactive_origins: 'FriendMessage'}[key];
    const options = new Map(sessions.filter(s => !requiredType || s.message_type === requiredType)
        .map(s => [s.umo, s]));
    for (const umo of selected) {
        if (!options.has(umo)) {
            options.set(umo, {umo, display_name: umo, message_type: umo.split(':', 2)[1], savedOnly: true});
        }
    }
    const updateSummary = () => {
        summary.textContent = selected.size ? `选择会话 · 已选 ${selected.size} 个` : '选择会话';
    };
    function renderOptions() {
        const query = search.value.trim().toLowerCase();
        const matches = [...options.values()].filter(session =>
            [session.display_name, session.auto_name, session.user_alias, session.umo, session.platform]
                .some(value => value?.toLowerCase().includes(query))
        );
        matches.sort((a, b) => Number(selected.has(b.umo)) - Number(selected.has(a.umo)));
        results.replaceChildren();
        for (const session of matches.slice(0, visibleLimit)) {
            const row = el('label', undefined, 'session-option');
            const check = el('input');
            check.type = 'checkbox';
            check.checked = selected.has(session.umo);
            check.addEventListener('change', () => {
                if (check.checked) selected.add(session.umo);
                else selected.delete(session.umo);
                updateSummary();
            });
            const name = el('span', session.display_name);
            const kind = sessionKinds[session.message_type] || (session.savedOnly ? '原有配置' : '会话');
            name.append(el('small', [kind, session.platform, session.session_id].filter(Boolean).join(' · ')));
            row.title = session.umo;
            row.append(check, name);
            results.append(row);
        }
        if (!matches.length) {
            empty(results, query ? '没有匹配的会话。' : '暂无会话，先在 AstrBot 中与目标会话互动。');
        } else if (matches.length > visibleLimit) {
            const more = el('button', `显示更多（共 ${matches.length} 项）`);
            more.type = 'button';
            more.onclick = () => {
                visibleLimit += 50;
                renderOptions();
            };
            results.append(more);
        }
    }
    search.addEventListener('input', () => {
        visibleLimit = 50;
        renderOptions();
    });
    picker.addEventListener('toggle', () => {
        if (picker.open) renderOptions();
    });
    updateSummary();
    picker.append(summary, search, results);
    field.append(el('legend', spec.description), picker, el('small', spec.hint));
    return field;
}

async function settings() {
    const [data, catalog] = await Promise.all([bridge.apiGet('settings'), bridge.apiGet('sessions')]);
    schema = data.schema;
    const values = data.values;
    savedSettings = values;
    $('settings-form').replaceChildren();
    const advanced = el('details', undefined, 'advanced-settings');
    advanced.append(el('summary', '更多设置：记忆容量、保留时间与后台参数'));
    const advancedKeys = new Set(['analysis_timeout_seconds', 'queue_limit', 'recall_limit', 'context_char_limit', 'raw_retention_days', 'reflection_batch_turns', 'reflection_max_wait_minutes', 'interop_gap_minutes']);
    for (const [key, spec] of Object.entries(schema)) {
        if (spec._special === 'umo') {
            $('settings-form').append(sessionPicker(key, spec, values[key], catalog.sessions));
            continue;
        }
        const field = el('div', undefined, 'field'),
            label = el('label', spec.description),
            input = document.createElement(['text', 'list'].includes(spec.type) ? 'textarea' : 'input');
        input.id = 'setting-' + key;
        label.htmlFor = input.id;
        input.dataset.key = key;
        if (spec.type === 'bool') {
            input.type = 'checkbox';
            input.checked = values[key];
        } else if (['int', 'float'].includes(spec.type)) {
            input.type = 'number';
            input.value = values[key];
            if (spec.slider) {
                input.min = spec.slider.min;
                input.max = spec.slider.max;
            }
            input.step = spec.type === 'int' ? '1' : String(spec.slider?.step || .1);
        } else {
            input.value = spec.type === 'list' ? values[key].join('\n') : values[key];
        }
        field.append(label, input);
        if (spec.hint) field.append(el('small', spec.hint));
        (advancedKeys.has(key) ? advanced : $('settings-form')).append(field);
    }
    $('settings-form').append(advanced);
    $('save').disabled = false;
}
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.panel').forEach(panel => panel.hidden = panel.id !== button.dataset.tab);
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button));
    $('refresh').hidden = button.dataset.tab === 'settings';
    if (bridge && button.dataset.tab === 'settings' && !settingsRequest) {
        settingsRequest = settings().catch(error => {
            settingsRequest = undefined;
            notice(error.message, true);
        });
    }
}));
$('refresh').onclick = () => action(refresh);
$('settings-form').onsubmit = event => event.preventDefault();
$('scope').onchange = () => {
    ++overviewRequest;
    $('query').value = '';
    $('memory-kind').value = '';
    experienceLimit = 8;
    action(loadScope);
};
$('memory-kind').onchange = renderMemories;
$('experiences-more').onclick = () => { experienceLimit += 8; renderExperiences(); };
$('search').onclick = () => action(loadScope);
$('query').onkeydown = event => {
    if (event.key === 'Enter') action(loadScope);
};
$('save').onclick = () => action(async () => {
    $('save').disabled = true;
    try {
        await saveSettings();
    } finally {
        $('save').disabled = false;
    }
});
async function saveSettings() {
    const update = {};
    for (const [key, spec] of Object.entries(schema)) {
        if (sessionSelections.has(key)) {
            update[key] = [...sessionSelections.get(key)];
            continue;
        }
        const input = $('setting-' + key);
        update[key] = spec.type === 'bool' ? input.checked : ['int', 'float'].includes(spec.type) ? Number(input.value) : spec.type === 'list' ? input.value.split('\n').map(s => s.trim()).filter(Boolean) : input.value;
    }
    for (const key of Object.keys(update)) {
        if (JSON.stringify(update[key]) === JSON.stringify(savedSettings[key])) delete update[key];
    }
    if (!Object.keys(update).length) {
        notice('没有需要保存的更改。');
        return;
    }
    await bridge.apiPost('settings', update);
    Object.assign(savedSettings, update);
    notice('配置已保存，后续对话与后台任务使用新设置。');
}
document.querySelectorAll('[data-action]').forEach(button => button.onclick = () => action(async () => {
    const scope = selected();
    if (!scope) return;
    const op = button.dataset.action;
    if (op === 'clear_confirm' && !await confirmDeletion('将永久删除此会话和人格的全部记忆、关系、好感和待解问题。')) return;
    await bridge.apiPost('control', {
        scope,
        action: op
    });
    await refresh();
    notice('操作已完成。');
}));
if (!bridge) {
    notice('请从 AstrBot 插件管理中的可视化视图打开此页。配置也可在插件配置页修改。', true);
} else {
    await action(async () => {
        await bridge.ready();
        await refresh();
    });
}

const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView;
let scopes = [],
    schema = {},
    values = {};
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

function selected() {
    return $('scope').value;
}

function groupedRelations(data) {
    const groups = new Map();
    for (const row of data.relations) {
        const key = JSON.stringify([row.source, row.target]);
        if (!groups.has(key)) groups.set(key, {source: row.source, target: row.target, rows: []});
        groups.get(key).rows.push(row);
    }
    return [...groups.values()];
}

function tagText(row) { return (row.status === 'ended' ? '曾经：' : '') + row.label; }

function graph(data) {
    const svg = $('graph');
    svg.replaceChildren();
    const ns = 'http://www.w3.org/2000/svg';
    const shape = (name, attributes) => {
        const node = document.createElementNS(ns, name);
        for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
        return node;
    };
    const defs = shape('defs', {}),
        marker = shape('marker', {
            id: 'arrow',
            viewBox: '0 0 10 10',
            refX: 9,
            refY: 5,
            markerWidth: 7,
            markerHeight: 7,
            orient: 'auto-start-reverse'
        });
    marker.append(shape('path', {
        d: 'M 0 0 L 10 5 L 0 10 z',
        fill: '#89a38b'
    }));
    defs.append(marker);
    svg.append(defs);
    const ids = [...new Set(data.relations.flatMap(r => [r.source, r.target]))].slice(0, 18);
    if (!ids.length) {
        const note = shape('text', {
            x: 320,
            y: 190,
            'text-anchor': 'middle'
        });
        note.textContent = '明确提到的人物关系，会在这里连成图。';
        svg.append(note);
        return;
    }
    const names = new Map(data.entities.map(e => [e.id, e.name]));
    const positions = new Map(ids.map((id, i) => [id, {
        x: 320 + 220 * Math.cos(2 * Math.PI * i / ids.length),
        y: 190 + 130 * Math.sin(2 * Math.PI * i / ids.length)
    }]));
    groupedRelations(data).forEach(r => {
        const a = positions.get(r.source),
            b = positions.get(r.target);
        if (!a || !b || r.source === r.target) return;
        const d = Math.hypot(b.x - a.x, b.y - a.y),
            dx = (b.x - a.x) / d,
            dy = (b.y - a.y) / d;
        svg.append(shape('line', {
            x1: a.x + dx * 31,
            y1: a.y + dy * 31,
            x2: b.x - dx * 34,
            y2: b.y - dy * 34,
            stroke: '#89a38b',
            'stroke-width': 1.5,
            'marker-end': 'url(#arrow)'
        }));
        const label = shape('text', {
            x: (a.x + b.x) / 2,
            y: (a.y + b.y) / 2 - 7,
            'text-anchor': 'middle'
        });
        label.textContent = r.rows.map(tagText).join(' · ');
        svg.append(label);
    });
    ids.forEach(id => {
        const p = positions.get(id);
        svg.append(shape('circle', {
            cx: p.x,
            cy: p.y,
            r: 29,
            fill: '#e1ebd8',
            stroke: '#a6bd9b'
        }));
        const label = shape('text', {
            x: p.x,
            y: p.y + 4,
            'text-anchor': 'middle'
        });
        label.textContent = (names.get(id) || id).slice(0, 8);
        svg.append(label);
    });
}
async function loadScope() {
    const scope = selected();
    if (!scope) return;
    const state = scopes.find(s => s.id === scope),
        stats = state?.stats || {};
    $('stats').replaceChildren();
    for (const [label, value] of [
            ['记忆与修订', stats.memories || 0],
            ['关系标签', stats.relations || 0],
            ['待整理', stats.pending || 0],
            ['运行状态', state?.paused ? '已暂停' : stats.failed ? `${stats.failed} 项失败` : '正常']
        ]) {
        const node = el('div', undefined, 'stat');
        node.append(el('span', label), el('strong', String(value)));
        $('stats').append(node);
    }
    const data = await bridge.apiGet('scope', {
        scope,
        query: $('query').value
    });
    graph(data);
    $('relations').replaceChildren();
    groupedRelations(data).forEach(r => {
        const names = new Map(data.entities.map(e => [e.id, e.name]));
        const node = el('div', `${names.get(r.source)||r.source} → ${names.get(r.target)||r.target}`, 'relation');
        const tags = el('div');
        r.rows.forEach(row => {
            const tag = el('span', tagText(row), 'tag');
            tag.title = row.description || row.evidence;
            tags.append(tag, document.createTextNode(' '));
        });
        node.append(tags);
        [...new Set(r.rows.map(row => row.description).filter(Boolean))].forEach(description => node.append(el('small', description)));
        const changes = el('details');
        changes.append(el('summary', '关系的来处与变化'));
        const history = (data.relation_history || []).filter(h => h.source === r.source && h.target === r.target);
        for (const h of history.length ? history : r.rows) {
            changes.append(el('small', `${new Date((h.created || h.updated)*1000).toLocaleDateString()} · ${tagText(h)} · ${names.get(h.reporter)||'聊天中提到'}：${h.evidence}`));
        }
        node.append(changes);
        $('relations').append(node);
    });
    $('feelings').replaceChildren();
    data.entities.filter(e => e.kind === 'user').forEach(e => {
        const node = el('div', undefined, 'feeling');
        const portrait = data.portraits?.[e.id] || {};
        node.append(el('b', e.name), el('p', portrait.impression || '正在慢慢熟悉。一起经历过的事情，会在后台逐渐沉淀成相处印象。'));
        if (portrait.shared_story) node.append(el('small', `共同经历：${portrait.shared_story}`));
        if (portrait.style) node.append(el('small', `相处习惯：${portrait.style}`));
        if (portrait.unfinished) node.append(el('small', `还挂心：${portrait.unfinished}`));
        const records = el('details');
        records.append(el('summary', '查看累计记录'), el('small', `长期好感 ${e.affection.toFixed(1)} · 信任 ${e.trust.toFixed(2)}`));
        node.append(records);
        $('feelings').append(node);
    });
    if (data.portraits?.self?.impression) {
        const own = el('div', undefined, 'feeling');
        own.append(el('h3', '最近对自己的认识'), el('p', data.portraits.self.impression));
        $('feelings').append(own);
    }
    if (!$('feelings').children.length) empty($('feelings'), '相处印象会在聊完一段后自动形成，不需要手动教它记。');
    $('questions').replaceChildren(); // Dashboard shows questions for all actors, not just a fake empty sender.
    (data.all_questions || data.curiosity).forEach(q => $('questions').append(el('div', q.question, 'question')));
    if (!$('questions').children.length) empty($('questions'), '没有待解的问题，也可以安静地陪伴。');
    $('memories').replaceChildren();
    data.memories.forEach(r => {
        const node = el('div', undefined, 'memory');
        node.append(el('span', labels[r.kind] || r.kind, 'tag'), el('p', r.content));
        const detail = el('details');
        detail.append(el('summary', `#${r.id} · ${Math.round(r.confidence*100)}% 置信度 · 查看依据`), el('div', `原文：${r.evidence}`), el('div', `来源：${r.source}`), el('div', `更新：${new Date(r.updated*1000).toLocaleString()}`));
        const remove = el('button', '忘记这条');
        remove.addEventListener('click', () => action(async () => {
            if (!confirm('删除此事实的全部修订，并清除本会话待整理任务及原文，防止旧分析写回。继续？')) return;
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
    if (!data.memories.length) empty($('memories'), '暂时没有相关回忆。换个关键词，或先聊一会儿。');
}
async function refresh() {
    const old = selected();
    const data = await bridge.apiGet('overview');
    scopes = data.scopes;
    $('scope').replaceChildren();
    scopes.forEach(s => {
        const option = el('option', `${s.origin} · ${s.persona}`);
        option.value = s.id;
        $('scope').append(option);
    });
    if (scopes.some(s => s.id === old)) $('scope').value = old;
    if (!scopes.length) {
        const option = el('option', '尚无对话：开始聊天后自动创建');
        option.value = '';
        $('scope').append(option);
    } else await loadScope();
    notice(data.last_error || '');
}
async function settings() {
    const data = await bridge.apiGet('settings');
    schema = data.schema;
    values = data.values;
    $('settings-form').replaceChildren();
    const advanced = el('details', undefined, 'advanced-settings');
    advanced.append(el('summary', '更多设置：记忆容量、保留时间与后台参数'));
    const advancedKeys = new Set(['analysis_timeout_seconds', 'queue_limit', 'recall_limit', 'context_char_limit', 'raw_retention_days', 'reflection_batch_turns', 'reflection_max_wait_minutes', 'interop_gap_minutes']);
    for (const [key, spec] of Object.entries(schema)) {
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
            input.min = spec.slider?.min;
            input.max = spec.slider?.max;
            input.step = spec.type === 'int' ? '1' : String(spec.slider?.step || .1);
        } else {
            input.value = spec.type === 'list' ? values[key].join('\n') : values[key];
        }
        field.append(label, input);
        if (spec.hint) field.append(el('small', spec.hint));
        (advancedKeys.has(key) ? advanced : $('settings-form')).append(field);
    }
    $('settings-form').append(advanced);
}
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.panel').forEach(panel => panel.hidden = panel.id !== button.dataset.tab);
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button));
}));
$('refresh').onclick = () => action(refresh);
$('scope').onchange = () => action(loadScope);
$('search').onclick = () => action(loadScope);
$('query').onkeydown = event => {
    if (event.key === 'Enter') action(loadScope);
};
$('save').onclick = () => action(async () => {
    const update = {};
    for (const [key, spec] of Object.entries(schema)) {
        const input = $('setting-' + key);
        update[key] = spec.type === 'bool' ? input.checked : ['int', 'float'].includes(spec.type) ? Number(input.value) : spec.type === 'list' ? input.value.split('\n').map(s => s.trim()).filter(Boolean) : input.value;
    }
    await bridge.apiPost('settings', update);
    notice('配置已保存，后续对话与后台任务使用新设置。');
});
document.querySelectorAll('[data-action]').forEach(button => button.onclick = () => action(async () => {
    if (!selected()) return;
    const op = button.dataset.action;
    if (op === 'clear_confirm' && !confirm('永久删除选定会话和人格的全部记忆、关系、好感与待解问题？')) return;
    await bridge.apiPost('control', {
        scope: selected(),
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
        await settings();
    });
}

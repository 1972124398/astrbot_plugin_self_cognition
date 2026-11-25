// Management-only view. It reads the selected scope and never writes memory or chat state.
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (cls) value.className = cls;
    return value;
};
const date = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleDateString() : '时间未记录';
let peopleById = new Map();
let world, currentScope, focus = '', graphPage = 0, peopleLimit = 12, relationLimit = 16;
const nameOf = id => peopleById.get(id)?.display_name || peopleById.get(id)?.name || '未命名人物';
const tags = person => [...new Set([person.identity?.name, person.identity?.nickname,
    ...(person.identity?.titles || []), ...(person.aliases || [])].filter(Boolean))];
const kindOf = person => person.id === 'self' ? '自己' : person.kind === 'user' ? '聊天伙伴' : '提及的人物';
const statusOf = row => row.disputed ? 'disputed' : row.status === 'ended' ? 'ended' : 'current';
function textBlock(parent, title, value) {
    if (!value) return;
    const block = node('div', undefined, 'profile-field');
    block.append(node('small', title), node('p', value));
    parent.append(block);
}
function empty(parent, text) { parent.append(node('p', text, 'empty')); }
function grouped(rows) {
    const groups = new Map();
    for (const row of rows) {
        const key = JSON.stringify([row.source, row.target]);
        if (!groups.has(key)) groups.set(key, {source: row.source, target: row.target, rows: []});
        groups.get(key).rows.push(row);
    }
    return [...groups.values()];
}
function choose(id) {
    focus = focus === id ? '' : id;
    graphPage = 0;
    relationLimit = 16;
    render();
}
function renderGraph(people, rows) {
    const svg = $('graph');
    svg.replaceChildren();
    const ns = 'http://www.w3.org/2000/svg';
    const shape = (tag, attrs, text) => {
        const value = document.createElementNS(ns, tag);
        for (const [key, item] of Object.entries(attrs)) value.setAttribute(key, item);
        if (text !== undefined) value.textContent = text;
        return value;
    };
    const pinned = people.find(person => person.id === focus);
    const others = pinned ? people.filter(person => person.id !== focus) : people;
    const pageSize = pinned ? 17 : 18;
    const pages = Math.max(1, Math.ceil(others.length / pageSize));
    graphPage = Math.min(Math.max(0, graphPage), pages - 1);
    const visible = [...(pinned ? [pinned] : []), ...others.slice(graphPage * pageSize, (graphPage + 1) * pageSize)];
    $('graph-prev').disabled = graphPage === 0;
    $('graph-next').disabled = graphPage + 1 >= pages;
    $('graph-caption').textContent = focus ? `与 ${nameOf(focus)} 有关 · 图谱 ${graphPage + 1} / ${pages}`
        : `${people.length} 位人物 · 图谱 ${graphPage + 1} / ${pages}`;
    $('graph-coverage').textContent = '';
    if (!visible.length) {
        svg.append(shape('text', {x: 380, y: 240, 'text-anchor': 'middle'}, '还没有匹配的人物。'));
        return;
    }
    const defs = shape('defs', {});
    for (const [kind, color] of [['current', '#5a8174'], ['ended', '#9ba6a0'], ['disputed', '#b58b46']]) {
        const marker = shape('marker', {id: `arrow-${kind}`, viewBox: '0 0 10 10', refX: 9,
            refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse'});
        marker.append(shape('path', {d: 'M0,0 L10,5 L0,10 Z', fill: color}));
        defs.append(marker);
    }
    svg.append(defs);
    const positions = new Map(visible.map((person, index) => {
        const angle = -Math.PI / 2 + index * 2 * Math.PI / visible.length;
        return [person.id, visible.length === 1 ? {x: 380, y: 235}
            : visible.length === 2 ? {x: index ? 580 : 180, y: 235}
            : {x: 380 + 272 * Math.cos(angle), y: 235 + 170 * Math.sin(angle)}];
    }));
    const edges = grouped(rows).filter(edge => positions.has(edge.source) && positions.has(edge.target));
    const displayed = edges.reduce((count, edge) => count + edge.rows.length, 0);
    $('graph-coverage').textContent = rows.length ? `图中显示 ${displayed} / ${rows.length} 条筛选关系；完整记录见下方列表。` : '';
    edges.forEach(edge => {
        const a = positions.get(edge.source), b = positions.get(edge.target);
        const status = edge.rows.some(row => row.disputed) ? 'disputed'
            : edge.rows.every(row => row.status === 'ended') ? 'ended' : 'current';
        const group = shape('g', {class: `graph-edge ${status}`});
        const reverse = edges.some(other => other.source === edge.target && other.target === edge.source);
        const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
        const dx = (b.x - a.x) / length, dy = (b.y - a.y) / length;
        const bend = reverse ? 90 : 0;
        const cx = (a.x + b.x) / 2 - dy * bend, cy = (a.y + b.y) / 2 + dx * bend;
        const path = edge.source === edge.target
            ? `M${a.x - 15},${a.y - 22} C${a.x - 75},${a.y - 100} ${a.x + 75},${a.y - 100} ${a.x + 15},${a.y - 25}`
            : `M${a.x + dx * 32},${a.y + dy * 32} Q${cx},${cy} ${b.x - dx * 36},${b.y - dy * 36}`;
        group.append(shape('path', {d: path, 'marker-end': `url(#arrow-${status})`}));
        const label = edge.rows.map(row => (row.status === 'ended' ? '曾经·' : '') + row.label).join(' / ');
        const title = shape('title', {}, `${nameOf(edge.source)} → ${nameOf(edge.target)}：${label}`);
        group.append(title);
        // Dense graphs keep labels in the relation list rather than obscuring the nodes.
        if (edges.length <= 12) {
            const x = edge.source === edge.target ? a.x : (a.x + 2 * cx + b.x) / 4;
            const y = edge.source === edge.target ? a.y - 65 : (a.y + 2 * cy + b.y) / 4 - 8;
            group.append(shape('text', {x, y, 'text-anchor': 'middle', class: 'edge-label'},
                label.length > 18 ? label.slice(0, 17) + '…' : label));
        }
        svg.append(group);
    });
    visible.forEach(person => {
        const p = positions.get(person.id), label = nameOf(person.id);
        const group = shape('g', {class: `graph-person ${person.id === focus ? 'selected' : ''}`,
            role: 'button', tabindex: 0, 'aria-label': `查看${label}的画像`,
            'aria-pressed': String(person.id === focus), transform: `translate(${p.x} ${p.y})`});
        group.append(shape('circle', {r: 30}), shape('text', {y: 5, 'text-anchor': 'middle', class: 'node-initial'},
            label.slice(0, 2)), shape('text', {y: 51, 'text-anchor': 'middle', class: 'node-name'},
            label.length > 11 ? label.slice(0, 10) + '…' : label), shape('title', {}, `${label} · ${kindOf(person)}`));
        group.onclick = () => choose(person.id);
        group.onkeydown = event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(person.id); }
        };
        svg.append(group);
    });
    if (!rows.length) svg.append(shape('text', {x: 380, y: 465, 'text-anchor': 'middle', class: 'graph-note'},
        '尚未记下明确关系，人物可以先独立存在。'));
}
function renderPeople(people) {
    const parent = $('people');
    parent.replaceChildren();
    for (const person of people.slice(0, peopleLimit)) {
        const card = node('button', undefined, `person-card ${person.id === focus ? 'selected' : ''}`);
        card.type = 'button';
        card.setAttribute('aria-pressed', String(person.id === focus));
        const heading = node('div', undefined, 'person-heading');
        heading.append(node('span', nameOf(person.id).slice(0, 1), 'avatar'), node('strong', nameOf(person.id)),
            node('span', kindOf(person), 'person-kind'));
        card.append(heading);
        const names = tags(person).filter(value => value !== nameOf(person.id));
        if (names.length) card.append(node('div', names.join(' · '), 'person-aliases'));
        const portrait = world.portraits?.[person.id] || {};
        card.append(node('p', portrait.impression || (person.id === 'self' ? '在经历中逐渐认识自己。'
            : '已经记住这个人，更多认识会在相处中慢慢形成。')));
        const count = world.relations.filter(row => row.source === person.id || row.target === person.id).length;
        card.append(node('small', `${count} 条人物关系 · 查看画像与来处 ↗`));
        card.onclick = () => choose(person.id);
        parent.append(card);
    }
    if (!people.length) empty(parent, '没有匹配的人物。可以试试另一个名字或称号。');
    $('people-more').hidden = peopleLimit >= people.length;
}
function renderRelations(rows) {
    const parent = $('relations');
    parent.replaceChildren();
    for (const row of rows.slice(0, relationLimit)) {
        const card = node('div', undefined, 'relation-card');
        const head = node('div', undefined, 'relation-heading');
        for (const [index, id] of [row.source, row.target].entries()) {
            if (index) head.append(node('span', '→', 'relation-arrow'));
            const person = node('button', nameOf(id), 'text-button');
            person.onclick = () => choose(id);
            head.append(person);
        }
        head.append(node('span', (row.status === 'ended' ? '曾经 · ' : '') + row.label, `tag ${statusOf(row)}`));
        if (row.disputed) head.append(node('span', '看法有分歧', 'tag disputed'));
        card.append(head);
        if (row.description) card.append(node('p', row.description));
        const evidence = node('details');
        evidence.append(node('summary', `${row.reporter ? nameOf(row.reporter) + ' 提到' : '聊天中记下'} · ${date(row.updated)} · 原话与变化`));
        evidence.append(node('p', row.evidence || '没有保留原话。', 'quote'));
        const perspectives = row.perspectives || [];
        for (const opinion of perspectives) evidence.append(node('p',
            `${nameOf(opinion.reporter)} 的看法：${opinion.status === 'ended' ? '已结束' : '仍成立'} · ${opinion.description || opinion.evidence}`));
        const history = (world.relation_history || []).filter(item => item.source === row.source
            && item.target === row.target && item.label === row.label);
        for (const item of history) evidence.append(node('small', `${date(item.created)} · ${item.status === 'ended' ? '结束' : '确认'} · ${item.evidence}`));
        card.append(evidence);
        parent.append(card);
    }
    if (!rows.length) empty(parent, focus ? '这个人物在当前筛选下还没有关系记录。' : '没有符合筛选的关系。后台只记录有原话依据的连接。');
    $('relations-more').hidden = relationLimit >= rows.length;
}
function renderDetail() {
    const parent = $('person-detail');
    parent.closest('.people-pane').classList.toggle('has-selection', Boolean(focus));
    parent.replaceChildren();
    parent.hidden = !focus;
    if (!focus) return;
    const person = peopleById.get(focus);
    if (!person) return;
    const identity = person.identity || {}, portrait = world.portraits?.[focus] || {};
    const heading = node('div', undefined, 'section-head');
    const title = node('div');
    title.append(node('span', kindOf(person), 'eyebrow'), node('h2', nameOf(focus)));
    const close = node('button', '取消聚焦');
    close.onclick = () => choose(focus);
    heading.append(title, close);
    parent.append(heading);
    const columns = node('div', undefined, 'profile-columns'), identityPane = node('div'), portraitPane = node('div');
    for (const [key, label] of [['name', '记住的姓名'], ['nickname', '昵称'], ['preferred_name', '本人希望的称呼']])
        textBlock(identityPane, label, identity[key]);
    textBlock(identityPane, '称号', (identity.titles || []).join(' · '));
    textBlock(identityPane, '其他记住的叫法', (person.aliases || []).filter(value => value !== nameOf(focus)).join(' · '));
    textBlock(identityPane, '平台显示名', person.name);
    for (const [key, label] of [['impression', '眼中的这个人'], ['shared_story', '共同经历'],
        ['style', '相处方式'], ['unfinished', '还挂心的事']]) textBlock(portraitPane, label, portrait[key]);
    if (!portraitPane.children.length) empty(portraitPane, '还没有形成完整画像，不需要为此刻意提问。');
    const source = node('details');
    source.append(node('summary', '名字与称号的原话依据'));
    const fieldNames = {name: '姓名', nickname: '昵称', titles: '称号', preferred_name: '称呼偏好'};
    for (const [field, value] of Object.entries(identity.evidence || {})) {
        if (field === 'titles' && Object.keys(identity.title_evidence || {}).length) continue;
        source.append(node('p', `${fieldNames[field] || field} · ${value.quote}`, 'quote'),
            node('small', `${date(value.updated)} · 记忆来源 ${value.source}`));
    }
    for (const [title, value] of Object.entries(identity.title_evidence || {})) {
        source.append(node('p', `称号「${title}」 · ${value.quote}`, 'quote'),
            node('small', `${date(value.updated)} · 记忆来源 ${value.source}`));
    }
    if (!Object.keys(identity.evidence || {}).length && !Object.keys(identity.title_evidence || {}).length) empty(source, '目前只有平台显示名或旧版别称，没有新增的姓名证据。');
    identityPane.append(source);
    const technical = node('details');
    technical.append(node('summary', '身份与累计记录'), node('small', `稳定身份 ${person.id}`));
    if (person.kind === 'user') technical.append(node('small', `长期好感 ${Number(person.affection || 0).toFixed(1)} · 信任 ${Number(person.trust || 0).toFixed(2)}`));
    portraitPane.append(technical);
    columns.append(identityPane, portraitPane);
    parent.append(columns);
}
function render() {
    if (!world) return;
    const query = $('person-search').value.trim().toLocaleLowerCase();
    const people = world.entities.filter(person => !query || [nameOf(person.id), person.name, ...tags(person)]
        .some(value => String(value || '').toLocaleLowerCase().includes(query)));
    const matches = new Set(people.map(person => person.id));
    const status = $('relation-state').value;
    const rows = world.relations.filter(row => (status === 'all' || statusOf(row) === status
        || status === 'current' && row.status !== 'ended')
        && (!query || matches.has(row.source) || matches.has(row.target))
        && (!focus || row.source === focus || row.target === focus));
    const related = new Set(rows.flatMap(row => [row.source, row.target]));
    if (focus) related.add(focus);
    const graphPeople = focus || query ? world.entities.filter(person => related.has(person.id) || !focus && matches.has(person.id)) : people;
    graphPeople.sort((a, b) => Number(b.id === focus) - Number(a.id === focus));
    $('world-count').textContent = `${world.entities.length} 位人物 / ${world.relations.length} 条关系`;
    renderGraph(graphPeople, rows);
    renderPeople(people);
    renderRelations(rows);
    renderDetail();
}
export function renderWorld(data, scope) {
    world = data;
    peopleById = new Map(data.entities.map(person => [person.id, person]));
    if (currentScope !== scope) {
        currentScope = scope;
        focus = '';
        graphPage = 0;
        peopleLimit = 12;
        relationLimit = 16;
        $('person-search').value = '';
        $('relation-state').value = 'all';
    }
    if (focus && !world.entities.some(person => person.id === focus)) focus = '';
    render();
}
$('person-search').oninput = () => { focus = ''; graphPage = 0; peopleLimit = 12; relationLimit = 16; render(); };
$('relation-state').onchange = () => { graphPage = 0; relationLimit = 16; render(); };
$('world-reset').onclick = () => { focus = ''; graphPage = 0; $('person-search').value = ''; $('relation-state').value = 'all'; render(); };
$('graph-prev').onclick = () => { graphPage--; render(); };
$('graph-next').onclick = () => { graphPage++; render(); };
$('people-more').onclick = () => { peopleLimit += 12; render(); };
$('relations-more').onclick = () => { relationLimit += 16; render(); };

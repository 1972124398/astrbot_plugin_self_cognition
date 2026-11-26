/* Run with Node and Playwright available; uses synthetic data, never the live bot. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../views/cognition');
const output = process.env.PANEL_SCREENSHOTS;
const names = ['林夏', '陈然', '周宁', '阿禾', '许老师', '我'];
const entities = names.map((name, i) => ({id: i === 5 ? 'self' : `person-${i}`, name: i === 0 ? 'wxid_platform_01' : name,
    display_name: name, kind: i < 2 ? 'user' : 'character', affection: 3.2, trust: .72,
    aliases: i === 0 ? ['林夏', '小夏', '林工'] : [],
    identity: i === 0 ? {name: '林夏', nickname: '小夏', preferred_name: '林夏', titles: ['林工'],
        evidence: {name: {quote: '我叫林夏，大家叫我林工。', source: 'sample-1', updated: 1795600000}}} : {}}));
const rel = (source, target, label, extra = {}) => ({source: `person-${source}`, target: `person-${target}`,
    label, status: 'current', reporter: 'person-0', description: '在聊天中了解到彼此的关系。',
    evidence: '陈然是我的同事，我们也一直是很好的朋友。', updated: 1795600000, confidence: .9, ...extra});
const data = {entities, relations: [rel(0, 1, '同事'), rel(0, 1, '朋友'), rel(1, 0, '信任'), rel(1, 2, '同学'),
    rel(2, 3, '合作伙伴', {status: 'ended'}), rel(3, 4, '师生', {disputed: true, perspectives: [
        {reporter: 'person-0', status: 'current', description: '曾提到向对方学画。'},
        {reporter: 'person-1', status: 'ended', description: '后来已经结课。'}]})],
    portraits: {'person-0': {impression: '细心，也有自己的节奏。愿意认真听别人说完，偶尔会用一个小玩笑接住话题。',
        shared_story: '聊起下班后的散步，也一起试过新的小游戏。', style: '自然、直接，熟悉后会互相打趣。'},
        'person-1': {impression: '喜欢分享新鲜事，做事认真，聊起老朋友时话会多一些。'}},
    all_questions: [], curiosity: [], memories: [
        {id: 1, kind: 'fact', content: '周末习惯散步', evidence: '周末常出去走走', source: 'sample-1', updated: 1795600000, confidence: .9},
        {id: 2, kind: 'preference', content: '喜欢无糖茶', evidence: '我爱喝无糖茶', source: 'sample-2', updated: 1795600000, confidence: .9}],
    relation_history: [], experiences: Array.from({length: 10}, (_, i) => ({title: `散步的片段 ${i + 1}`,
        story: '一起聊起河边的树和下班路上的见闻。', source_kind: 'dialogue', created: 1795600000})), character: {}, intentions: []};
const scope = id => ({id, origin: `test:FriendMessage:${id}`, display_name: id === 'a' ? '林夏的私聊' : '另一段会话',
    persona: '日常伙伴', stats: {pending: 0, memories: 0, relations: 6}});
(async () => {
    const server = http.createServer((req, res) => {
        const file = req.url === '/' ? 'index.html' : req.url.slice(1).split('?')[0];
        if (!['index.html', 'app.js', 'people.js', 'style.css'].includes(file)) { res.writeHead(404); res.end(); return; }
        res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
        res.end(fs.readFileSync(path.join(root, file)));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
    const browser = await chromium.launch({headless: true,
        ...(fs.existsSync(edge) ? {executablePath: edge} : {})});
    try {
        const page = await browser.newPage({viewport: {width: 1440, height: 1050}, deviceScaleFactor: 1});
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.addInitScript(({data, scopes}) => {
            window.testData = data;
            window.testScopes = scopes;
            window.writes = [];
            window.AstrBotPluginView = {ready: async () => {}, apiGet: async (route, args) => {
                if (route === 'overview') return {scopes: window.testScopes};
                if (route === 'scope') return args.scope === 'a' ? window.testData
                    : {...window.testData, entities: [], relations: [], portraits: {}};
                if (route === 'settings') return {schema: {}, values: {}};
                if (route === 'sessions') return {sessions: []};
                throw new Error(route);
            }, apiPost: async (...args) => { window.writes.push(args); return {}; }};
        }, {data, scopes: [scope('a'), scope('b')]});
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        await page.locator('#scope-details').waitFor({state: 'visible'});
        assert.equal(await page.locator('.person-card').count(), 6);
        assert.equal(await page.locator('.graph-person').count(), 6);
        assert.equal(await page.locator('.relation-card').count(), 6);
        await page.selectOption('#memory-kind', 'preference');
        assert.equal(await page.locator('.memory').count(), 1);
        assert.match(await page.locator('.memory').textContent(), /无糖茶/);
        await page.selectOption('#memory-kind', '');
        assert.equal(await page.locator('.memory').count(), 2);
        assert.equal(await page.locator('#experiences > div').count(), 8);
        await page.locator('#experiences-more').click();
        assert.equal(await page.locator('#experiences > div').count(), 10);
        assert.equal(await page.locator('#experiences-more').isVisible(), false);
        if (output) { fs.mkdirSync(output, {recursive: true}); await page.screenshot({path: path.join(output, '认知面板-桌面.png'), fullPage: true}); }
        await page.locator('#person-search').fill('林工');
        assert.equal(await page.locator('.person-card').count(), 1);
        await page.locator('.person-card').click();
        await page.locator('#person-detail').waitFor({state: 'visible'});
        assert.match(await page.locator('#person-detail').textContent(), /姓名[\s\S]*林夏/);
        await page.locator('#person-detail summary').first().click();
        assert.match(await page.locator('#person-detail').textContent(), /我叫林夏/);
        await page.locator('#world-reset').click();
        await page.selectOption('#relation-state', 'ended');
        assert.equal(await page.locator('.relation-card').count(), 1);
        await page.selectOption('#relation-state', 'disputed');
        assert.equal(await page.locator('.relation-card').count(), 1);
        assert.match(await page.locator('.relation-card').textContent(), /看法有分歧/);
        await page.locator('.relation-card summary').click();
        assert.match(await page.locator('.relation-card').textContent(), /后来已经结课/);
        await page.locator('#world-reset').click();
        await page.locator('.graph-person').first().focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#person-detail').isVisible(), true);
        if (output) await page.screenshot({path: path.join(output, '认知面板-人物详情.png'), fullPage: true});
        await page.selectOption('#scope', 'b');
        assert.equal(await page.locator('#person-detail').isVisible(), false);
        assert.equal(await page.locator('.person-card').count(), 0);
        await page.selectOption('#scope', 'a');
        await page.setViewportSize({width: 390, height: 844});
        if (output) await page.screenshot({path: path.join(output, '认知面板-手机.png'), fullPage: true});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile overflow');
        await page.locator('[data-tab="settings"]').click();
        assert.equal(await page.locator('#settings').isVisible(), true);
        await page.locator('[data-tab="memory"]').click();
        // Untrusted names/evidence remain plain text, including SVG labels.
        await page.evaluate(() => {
            window.testData.entities[0].display_name = '<img src=x onerror="window.pwned=1">';
            window.testData.relations[0].evidence = '<script>window.pwned=2</script>';
        });
        await page.locator('#refresh').click();
        assert.equal(await page.locator('#people img').count(), 0);
        assert.equal(await page.evaluate(() => window.pwned), undefined);
        // Dense graphs and lists paginate rather than silently dropping records.
        await page.evaluate(() => {
            window.testData.entities = Array.from({length: 42}, (_, i) => ({id: `p${i}`, name: `人物${i}`, kind: 'character'}));
            window.testData.relations = Array.from({length: 41}, (_, i) => ({source: 'p0', target: `p${i + 1}`, label: '朋友', status: 'current'}));
        });
        await page.locator('#refresh').click();
        assert.equal(await page.locator('.graph-person').count(), 18);
        await page.locator('#graph-next').click();
        assert.match(await page.locator('#graph-caption').textContent(), /2 \/ 3/);
        await page.locator('#people-more').click();
        assert.equal(await page.locator('.person-card').count(), 24);
        await page.locator('#relations-more').click();
        assert.equal(await page.locator('.relation-card').count(), 32);
        await page.locator('#graph-prev').click();
        await page.locator('.graph-person').first().focus();
        await page.keyboard.press('Enter');
        await page.locator('#graph-next').click();
        assert.equal(await page.locator('.graph-person.selected').count(), 1, 'Focused person must stay on page 2');
        assert.match(await page.locator('.graph-person.selected').textContent(), /人物0/);
        await page.locator('#person-search').fill('人物41');
        assert.equal(await page.locator('#person-detail').isVisible(), false, 'New name search clears previous focus');
        assert.equal(await page.locator('.person-card').count(), 1);
        await page.locator('#world-reset').click();
        // Overlapping refreshes must not apply stale overview responses.
        await page.evaluate(() => {
            window.originalGet = window.AstrBotPluginView.apiGet;
            window.pendingOverview = [];
            window.AstrBotPluginView.apiGet = (route, args) => route === 'overview'
                ? new Promise(resolve => window.pendingOverview.push(resolve)) : window.originalGet(route, args);
        });
        await page.locator('#refresh').click();
        await page.locator('#refresh').click();
        await page.waitForFunction(() => window.pendingOverview.length === 2);
        await page.evaluate(() => window.pendingOverview[1]({scopes: [{...window.testScopes[0], display_name: '最新会话'}]}));
        await page.locator('#scope-details').waitFor({state: 'visible'});
        await page.evaluate(() => window.pendingOverview[0]({scopes: [{...window.testScopes[1], display_name: '过期会话'}]}));
        assert.match(await page.locator('#scope option').textContent(), /最新会话/);
        await page.evaluate(() => { window.AstrBotPluginView.apiGet = window.originalGet; });
        await page.locator('#refresh').click();
        // Slow previous scope requests must not replace the current scope.
        await page.evaluate(() => {
            window.pendingScope = [];
            window.AstrBotPluginView.apiGet = (route, args) => route === 'scope'
                ? new Promise(resolve => window.pendingScope.push({scope: args.scope, resolve})) : window.originalGet(route, args);
        });
        await page.selectOption('#scope', 'b');
        await page.selectOption('#scope', 'a');
        await page.waitForFunction(() => window.pendingScope.length === 2);
        await page.evaluate(() => window.pendingScope[1].resolve(window.testData));
        await page.locator('#scope-details').waitFor({state: 'visible'});
        await page.evaluate(() => window.pendingScope[0].resolve({...window.testData, entities: [], relations: []}));
        assert.equal(await page.locator('.person-card').count(), 12);
        assert.equal(await page.locator('#scope').inputValue(), 'a');
        // Failures remain visible and are recoverable by refreshing.
        await page.evaluate(() => {
            window.AstrBotPluginView.apiGet = (route, args) => route === 'scope'
                ? Promise.reject(new Error('模拟读取失败')) : window.originalGet(route, args);
        });
        await page.locator('#refresh').click();
        await page.waitForFunction(() => document.getElementById('notice').textContent.includes('模拟读取失败'));
        assert.equal(await page.locator('#scope-loading').isVisible(), true);
        assert.equal(await page.locator('#scope-details').isVisible(), false);
        await page.evaluate(() => { window.AstrBotPluginView.apiGet = window.originalGet; });
        await page.locator('#refresh').click();
        await page.locator('#scope-details').waitFor({state: 'visible'});
        await page.evaluate(() => { window.testScopes = []; });
        await page.locator('#refresh').click();
        assert.equal(await page.locator('#scope-empty').isVisible(), true);
        assert.deepEqual(await page.evaluate(() => window.writes), []);
        await page.evaluate(scopes => { window.testScopes = scopes; }, [scope('a'), scope('b')]);
        await page.locator('#refresh').click();
        await page.locator('#scope-details').waitFor({state: 'visible'});
        await page.locator('.maintenance > summary').click();
        const dialog = page.getByRole('dialog');
        await page.getByRole('button', {name: '清空长期记忆', exact: true}).click();
        assert.match(await dialog.textContent(), /林夏的私聊.*日常伙伴/s);
        await dialog.getByRole('button', {name: '取消'}).click();
        assert.deepEqual(await page.evaluate(() => window.writes), []);

        for (const [label, operation, scope] of [
            ['清空长期记忆', 'clear_memory', 'a'],
            ['清空短期上下文', 'clear_context', 'b'],
            ['清空聊天记录', 'clear_records', 'b']
        ]) {
            await page.selectOption('#scope', scope);
            await page.getByRole('button', {name: label, exact: true}).click();
            assert.match(await dialog.textContent(), /无法撤销/);
            await dialog.getByRole('button', {name: '确认删除'}).click();
            await page.waitForFunction(label => document.getElementById('notice').textContent.includes(`已${label}`), label);
        }
        assert.deepEqual(await page.evaluate(() => window.writes), [
            ['control', {scope: 'a', action: 'clear_memory'}],
            ['control', {scope: 'b', action: 'clear_context'}],
            ['control', {scope: 'b', action: 'clear_records'}]
        ]);

        await page.locator('.companion-cleanup > summary').click();
        assert.equal(await page.locator('#companion-cleanup-empty').isVisible(), true);
        await page.evaluate(() => {
            window.testData.companion_cleanup = [
                {id: 'daily_routine', label: '日常调查', description: '所选人格的日常画像'},
                {id: 'play_together', label: '一起玩', description: '整个会话的游戏记录'}
            ];
            window.testData.companion_cleanup_origin = 'test:FriendMessage:a';
            const post = window.AstrBotPluginView.apiPost;
            window.AstrBotPluginView.apiPost = async (...args) => {
                await post(...args);
                return {results: [{label: '日常调查', status: 'cleared'}, {label: '一起玩', status: 'busy'}]};
            };
        });
        await page.selectOption('#scope', 'a');
        const clearPeers = page.locator('#companion-cleanup-submit');
        await clearPeers.waitFor({state: 'visible'});
        assert.equal(await clearPeers.isDisabled(), true);
        await page.getByRole('checkbox', {name: /日常调查/}).check();
        await page.getByRole('checkbox', {name: /一起玩/}).check();
        await clearPeers.click();
        assert.match(await dialog.textContent(), /林夏的私聊.*日常调查.*一起玩/s);
        await dialog.getByRole('button', {name: '取消'}).click();
        assert.equal(await page.evaluate(() => window.writes.length), 3);
        await clearPeers.click();
        await dialog.getByRole('button', {name: '确认删除'}).click();
        await page.waitForFunction(() => document.getElementById('notice').textContent.includes('一起玩 · 正在发送'));
        assert.match(await page.locator('#notice').textContent(), /日常调查 · 已清理/);
        assert.deepEqual(await page.evaluate(() => window.writes.at(-1)), ['companion_cleanup', {
            scope: 'a', origin: 'test:FriendMessage:a', plugins: ['daily_routine', 'play_together']
        }]);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        assert.deepEqual(errors, []);
        console.log('PASS: loading races, failure recovery, memory filtering, graph, scope isolation, mobile, settings, XSS and confirmed cleanup');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

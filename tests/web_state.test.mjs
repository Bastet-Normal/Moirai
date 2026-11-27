// Unit tests execute the browser application scripts with a minimal DOM.
// They deliberately do not use a real browser, user data, or any model provider.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import vm from 'node:vm';

const cytoscape = createRequire(import.meta.url)('../web/assets/cytoscape.min.js');

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(code => code.includes('function selectNovel'));
assert.ok(script);
const graphScript = readFileSync(new URL('../web/assets/entity-graph.js', import.meta.url), 'utf8');

class Element {
    constructor(id = '') {
        this.id = id;
        this.value = '';
        this.textContent = '';
        this.innerText = '';
        this.innerHTML = '';
        this.style = {};
        this.disabled = false;
        this.checked = false;
        this.scrollTop = this.scrollLeft = 0;
        this.scrollHeight = this.clientHeight = 100;
        this.attributes = new Map();
        this.classes = new Set();
        this.classList = {
            add: (...names) => names.forEach(n => this.classes.add(n)),
            remove: (...names) => names.forEach(n => this.classes.delete(n)),
            contains: name => this.classes.has(name),
            toggle: (name, force) => {
                if (force ?? !this.classes.has(name)) this.classes.add(name);
                else this.classes.delete(name);
            },
        };
    }
    setAttribute(name, value) { this.attributes.set(name, value); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    hasAttribute(name) { return this.attributes.has(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    replaceChildren() { this.innerHTML = ''; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    scrollIntoView(options) { this.scrollOptions = options; }
    focus() {}
}

function setup({workspace = false} = {}) {
    const elements = new Map();
    const timers = [];
    let nextTimerId = 1;
    const document = {
        handlers: new Map(),
        getElementById(id) {
            if (!elements.has(id)) {
                const el = new Element(id);
                el.focus = () => { document.activeElement = el; };
                elements.set(id, el);
            }
            return elements.get(id);
        },
        querySelectorAll: () => [], querySelector: () => null,
        addEventListener(type, handler) {
            if (!this.handlers.has(type)) this.handlers.set(type, []);
            this.handlers.get(type).push(handler);
        },
        activeElement: new Element(),
        documentElement: new Element('html'),
        hidden: false,
    };
    const requests = [];
    const stored = new Map();
    const context = vm.createContext({
        document,
        localStorage: {
            getItem: key => stored.get(key) ?? null,
            setItem: (key, value) => stored.set(key, String(value)),
        },
        TextEncoder,
        window: {addEventListener() {}, matchMedia: () => ({matches: false})},
        requestAnimationFrame: callback => callback(),
        getComputedStyle: element => ({visibility: element.style.visibility ?? "visible"}),
        setTimeout(callback, delay) {
            const timer = {id: nextTimerId++, callback, delay, cancelled: false};
            timers.push(timer);
            return timer.id;
        },
        setInterval() {},
        clearTimeout(id) {
            const timer = timers.find(item => item.id === id);
            if (timer) timer.cancelled = true;
        },
        fetch: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    });
    const run = code => vm.runInContext(code, context);
    run(graphScript);
    run(script);
    // Renderers are outside the race tests' scope; fetch/lifecycle/save functions
    // remain the exact production implementations.
    run(`
        var toasts = [], rendered = [];
        var productionNovelList = renderNovelList, productionCharacters = renderCharacters, productionChapterNav = renderChapterNav;
        var productionShortDramaAndAudio = renderShortDramaAndAudio;
        showToast = (message, type) => toasts.push({message, type});
        renderNovelList = () => {};
        renderReaderSimSidebar = renderComplianceRail = updateChapterNav = resetReaderProgress = renderShortDramaAndAudio = () => {};
    `);
    if (workspace) run('renderBranching = renderForeshadowing = renderOutlineTab = renderCharacters = renderWorld = renderPacing = renderDelivery = renderCytoscapeGraph = () => {};');
    else run('renderNovelWorkspace = () => rendered.push(novelData.novel_id);');
    const respond = (request, data, status = 200) => request.resolve({ok: status < 400, status, json: async () => data});
    const bind = (id = 'book', revision = 'a'.repeat(64)) => run(`
        currentNovelId = ${JSON.stringify(id)};
        novelData = {novel_id: currentNovelId, revision: ${JSON.stringify(revision)}, chapters: []};
        novelLoadToken++;
    `);
    return {run, requests, respond, bind, document, timers, el: id => document.getElementById(id)};
}

function detail(id, revision = id.repeat(64).slice(0, 64)) {
    return {novel_id: id, revision, chapters: [], concept: {title: id}, summary: {total_words: 0}};
}

function chapters(numbers, loaded = true) {
    return numbers.map(n => ({chapter_number:n, title:`章节${n}`, word_count:3, volume_number:1, ...(loaded ? {full_text:`正文${n}`} : {})}));
}

test('refresh preserves the current tab, chapter identity, expanded directory and scroll; explicit selection resets', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    s.run("selectedChapterIdx=1; selectedChapterNum=3; window._allChaptersShown=true;");
    s.el('reader-scroll').scrollTop = 240;
    s.run("switchTab('characters')");
    s.el('tab-characters').scrollTop = 80;
    s.el('workspace-tabs').scrollLeft = 130;
    const refresh = s.run('refreshProjectContext(captureProjectContext())');
    s.respond(s.requests[0], {...detail('a'), chapters:chapters([1,2,3])});
    assert.equal(await refresh, true);
    assert.equal(s.requests[1].url, '/api/novel/a/compliance');
    s.respond(s.requests[1], {revision:'a'.repeat(64), chapters:[]});
    assert.equal(s.run('activeTab'), 'characters');
    assert.equal(s.run('selectedChapterNum'), 3);
    assert.equal(s.run('selectedChapterIdx'), 2);
    assert.equal(s.run('window._allChaptersShown'), true);
    assert.equal(s.el('tab-characters').scrollTop, 80);
    assert.equal(s.el('workspace-tabs').scrollLeft, 130);
    s.run("switchTab('chapters')");
    assert.equal(s.el('reader-scroll').scrollTop, 240);
    const select = s.run("selectNovel('a')");
    s.respond(s.requests[2], {...detail('a'), chapters:chapters([1,2,3])});
    await select;
    assert.equal(s.run('activeTab'), 'chapters');
    assert.equal(s.run('selectedChapterNum'), 1);
    assert.equal(s.el('reader-scroll').scrollTop, 0);
});

test('workspace and compliance navigation expose the selected context', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    s.run(`novelData={...novelData, concept:{title:'雾港来信', genre:'悬疑', logline:'测试'}, summary:{total_words:0}, chapters:[]}`);
    await s.run('renderNovelWorkspace()');
    assert.equal(s.el('workspace-tabs').getAttribute('aria-label'), '雾港来信 · 作品工作区');

    const first = new Element('first-risk');
    const second = new Element('second-risk');
    s.document.querySelectorAll = () => [first, second];
    s.run(`
        selectedChapterIdx=0; selectedChapterNum=1;
        sortedChapters=[{chapter_number:1, full_text:'第一项风险\\n\\n第二项风险'}];
        window._complianceMap={1:[{word:'第一项'}, {word:'第二项'}]};
        scrollToHit(1);
    `);
    assert.equal(first.scrollOptions, undefined);
    assert.equal(second.scrollOptions.block, 'center');
});

test('compliance summary opens its analysis panel and exposes the findings', () => {
    const s = setup({workspace:true});
    s.bind('a');
    const firstFinding = new Element('first-finding');
    firstFinding.focus = options => { firstFinding.focusOptions = options; };
    s.el('compliance-box').querySelector = selector => selector === '.compliance-hit' ? firstFinding : null;
    s.run(`
        selectedChapterNum=1;
        window._complianceMap={1:[{word:'风险'}]};
        openCompliancePanel(document.getElementById('compliance-chip'));
    `);
    assert.equal(s.el('reader-rail').classes.has('open'), true);
    assert.equal(s.el('reader-rail').getAttribute('aria-hidden'), 'false');
    assert.equal(s.el('reader-rail').hasAttribute('inert'), false);
    assert.equal(s.el('compliance-chip').getAttribute('aria-expanded'), 'true');
    assert.equal(s.el('btn-rail-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(s.el('compliance-box').scrollOptions.block, 'start');
    assert.equal(firstFinding.focusOptions.preventScroll, true);

    s.run('toggleReaderRail(false)');
    assert.equal(s.el('reader-rail').getAttribute('aria-hidden'), 'true');
    assert.equal(s.el('reader-rail').hasAttribute('inert'), true);
    assert.equal(s.el('compliance-chip').getAttribute('aria-expanded'), 'false');
});

test('duplicate compliance words retain a deterministic result index', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    const first = new Element('first-risk');
    const second = new Element('second-risk');
    s.document.querySelectorAll = () => [first, second];
    s.run(`
        selectedChapterIdx=0; selectedChapterNum=1;
        sortedChapters=[{chapter_number:1, full_text:'同词风险一\\n\\n同词风险二'}];
        window._complianceMap={1:[{word:'同词', category:'A', first_pos:0}, {word:'同词', category:'B', first_pos:7}]};
        scrollToHit(1);
    `);
    assert.equal(first.scrollOptions, undefined);
    assert.equal(second.scrollOptions.block, 'center');
});

test('lazy text restores reading position only after loading, and a later chapter wins', async () => {
    for (const changeChapter of [false, true]) {
        const s = setup({workspace:true});
        s.bind('a');
        s.run('selectedChapterNum=1');
        s.el('reader-scroll').scrollTop = 700;
        const refresh = s.run('refreshProjectContext(captureProjectContext())');
        s.respond(s.requests[0], {...detail('a'), chapters:chapters([1,2], false)});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(s.requests.length, 2);
        s.el('reader-scroll').scrollTop = 0; // Browser clamps while loading placeholder is short.
        if (changeChapter) {
            const later = s.run('showChapter(1)');
            s.respond(s.requests[2], {novel_id:'a', revision:'a'.repeat(64), chapter:chapters([2])[0]});
            await later;
        }
        s.respond(s.requests[1], {novel_id:'a', revision:'a'.repeat(64), chapter:chapters([1])[0]});
        await refresh;
        assert.equal(s.el('reader-scroll').scrollTop, changeChapter ? 0 : 700);
        assert.equal(s.run('selectedChapterNum'), changeChapter ? 2 : 1);
        if (changeChapter) assert.match(s.el('current-chapter-body').innerHTML, /正文2/);
    }
});

test('a removed chapter falls back without restoring another chapter scroll; early selections remain visible', async () => {
    for (const oldChapter of [3, 30]) {
        const s = setup({workspace:true});
        s.bind('a');
        s.run(`selectedChapterIdx=2; selectedChapterNum=${oldChapter}`);
        s.el('reader-scroll').scrollTop = 220;
        const refresh = s.run('refreshProjectContext(captureProjectContext())');
        s.respond(s.requests[0], {...detail('a'), chapters:chapters(Array.from({length:12}, (_,i)=>i+1))});
        await refresh;
        assert.equal(s.run('selectedChapterNum'), 3);
        assert.equal(s.run('window._allChaptersShown'), true);
        assert.equal(s.el('reader-scroll').scrollTop, oldChapter === 3 ? 220 : 0);
        assert.match(s.el('chapters-nav-list').innerHTML, /aria-current="page"/);
    }
});

test('a refresh superseded by reopening the same book cannot restore old navigation', async () => {
    const s=setup({workspace:true});
    s.bind('a');
    s.run("selectedChapterNum=3; switchTab('characters')");
    const old=s.run('refreshProjectContext(captureProjectContext())');
    const latest=s.run("selectNovel('a')");
    s.respond(s.requests[1], {...detail('a', 'b'.repeat(64)), chapters:chapters([1,2])});
    await latest;
    s.respond(s.requests[0], {...detail('a'), chapters:chapters([1,2,3])});
    assert.equal(Boolean(await old),false);
    assert.equal(s.run('activeTab'),'chapters');
    assert.equal(s.run('selectedChapterNum'),1);
    assert.equal(s.run('novelData.revision'),'b'.repeat(64));
});

test('task completion refresh keeps the author on the editing tab', async () => {
    const s=setup({workspace:true});
    s.bind('a');
    s.run("selectedChapterNum=2; switchTab('characters'); wasRunning=true; loadNovels=async()=>{};");
    const poll=s.run('refreshTaskStatus()');
    s.respond(s.requests[0], {is_running:false,current_novel:'a',current_step:'完成',task_id:'finished'});
    await poll;
    assert.equal(s.requests.length,2);
    s.respond(s.requests[1], {...detail('a'),chapters:chapters([1,2,3])});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(s.run('activeTab'),'characters');
    assert.equal(s.run('selectedChapterNum'),2);
});

test('tab keyboard navigation wraps, supports Home/End and maintains one selected tab', () => {
    const s = setup();
    for (const [from,key,to] of [['chapters','ArrowLeft','world'], ['world','ArrowRight','chapters'], ['characters','Home','chapters'], ['chapters','End','world']]) {
        let prevented=false, stopped=false;
        s.run(`switchTab('${from}')`);
        const handler=s.run('tabKeydown');
        handler({key, target:s.el(`tab-btn-${from}`), preventDefault(){ prevented=true; }, stopPropagation(){ stopped=true; }});
        assert.equal(prevented,true);
        assert.equal(stopped,true);
        assert.equal(s.run('activeTab'),to);
        assert.equal(s.document.activeElement,s.el(`tab-btn-${to}`));
        assert.equal(s.el(`tab-btn-${to}`).scrollOptions.block, 'nearest');
        assert.equal(s.el(`tab-btn-${to}`).scrollOptions.inline, 'nearest');
        for(const t of Array.from(s.run('ALL_TABS'))) {
            assert.equal(s.el(`tab-btn-${t}`).getAttribute('aria-selected'),String(t===to));
            assert.equal(s.el(`tab-btn-${t}`).getAttribute('tabindex'),t===to?'0':'-1');
            assert.equal(s.el(`tab-${t}`).classList.contains('hidden'), t!==to);
        }
    }
    s.run("switchTab('not-a-tab')");
    assert.equal(s.run('activeTab'),'world');
});

test('tab arrows never bubble into chapter navigation; reader shortcuts respect focus and modifiers', () => {
    const s=setup();
    s.bind('a');
    s.run('sortedChapters=[{},{}]; var chapterMoves=[]; navChapter=delta=>chapterMoves.push(delta);');
    const globalKeydown=s.document.handlers.get('keydown')[1];
    const event={key:'ArrowRight',target:s.el('tab-btn-chapters'),defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};
    s.run('tabKeydown')(event);
    globalKeydown(event);
    assert.equal(s.run('activeTab'),'graph');
    assert.equal(s.run('chapterMoves.length'),0);
    globalKeydown({...event,defaultPrevented:false});
    assert.equal(s.run('chapterMoves.length'),0);
    s.run("switchTab('chapters')");
    globalKeydown({...event,defaultPrevented:false,ctrlKey:true});
    s.document.activeElement.isContentEditable=true;
    globalKeydown({...event,defaultPrevented:false});
    s.document.activeElement.isContentEditable=false;
    s.el('novel-workspace').setAttribute('inert','');
    globalKeydown({...event,defaultPrevented:false});
    assert.equal(s.run('chapterMoves.length'),0);
    s.el('novel-workspace').removeAttribute('inert');
    globalKeydown({...event,defaultPrevented:false});
    assert.deepEqual(Array.from(s.run('chapterMoves')),[1]);
});

test('dynamic actions use native buttons and escape contextual accessible names', () => {
    const s=setup();
    s.bind('a');
    s.run(`window._novels=[{novel_id:'a', title:'书"<script>', genre:'测试', total_generated_chapters:1, total_words:3}]; productionNovelList();
        novelData.characters=[{name:'角色"<script>',role:'主角',personality_tags:[], current_state:{level:'境界',equipment_and_items:[]}}]; productionCharacters();`);
    assert.match(s.el('novels-list').innerHTML, /<button\b[^>]*\bonclick="selectNovel/);
    assert.match(s.el('novels-list').innerHTML, /aria-label="打开作品：书&quot;&lt;script&gt;"/);
    assert.match(s.el('character-list').innerHTML, /aria-label="编辑角色：角色&quot;&lt;script&gt;"/);
    assert.doesNotMatch(s.el('novels-list').innerHTML, /<div onclick=/);
});

test('unplanned bookshelf entries remain distinguishable by project id', () => {
    const s=setup();
    s.bind('qa_error_boundary');
    s.run(`window._novels=[
        {novel_id:'qa_error_boundary', title:'未策划', genre:'', total_generated_chapters:0, total_words:0},
        {novel_id:'second_partial', title:'', genre:'', total_generated_chapters:0, total_words:0}
    ]; productionNovelList();`);
    const html = s.el('novels-list').innerHTML;
    assert.match(html, /打开作品：未策划 · qa_error_boundary/);
    assert.match(html, /<strong>未策划 · second_partial<\/strong>/);
});

test('graph symbols distinguish entity types and preserve their identity when inactive', () => {
    const s = setup();
    s.bind('a');
    const roles = ['CHARACTER', 'ITEM', 'FACTION', 'LOCATION', 'SECRET', 'EVENT', 'unknown'];
    const elements = roles.map(type => ({data:{id:type, label:type, type}}));
    elements.push({data:{id:'lost', label:'旧钥匙', type:'ITEM', status:'DESTROYED'}});
    elements.push({data:{id:'edge', source:'CHARACTER', target:'ITEM', label:'保管'}});
    s.document.createTestGraph = serialized => cytoscape({headless:true, elements:JSON.parse(serialized), layout:{name:'preset'}});
    s.run(`var graphOptions; cytoscape = options => {
        graphOptions = options;
        return document.createTestGraph(JSON.stringify(options.elements));
    };
        novelData.cytoscape_data = {elements:${JSON.stringify(elements)}};`);
    for (const dark of [false, true]) {
        s.document.documentElement.classList.toggle('dark', dark);
        s.run('renderCytoscapeGraph()');
        assert.equal(s.run("cyInstance.nodes(':selected').id()"), 'CHARACTER');
        assert.equal(s.run("cyInstance.edges('.inspected').length"), 1);
        assert.equal(s.run("cyInstance.$id('ITEM').hasClass('muted')"), false);
        assert.equal(s.run("cyInstance.$id('LOCATION').hasClass('muted')"), true);
        const {style, elements:rendered} = JSON.parse(s.run('JSON.stringify(graphOptions)'));
        const cy = cytoscape({headless:true, styleEnabled:true, layout:{name:'preset'}, style, elements:rendered});
        try {
            const models = {ITEM:'item', FACTION:'faction', LOCATION:'location', SECRET:'secret', EVENT:'event', unknown:'other'};
            for (const node of cy.nodes()) {
                const path = node.data('symbolImage');
                const type = node.data('type');
                if (type === 'CHARACTER') assert.match(path, /^\/assets\/graph-robots\/(character|scout)\.png$/);
                else assert.equal(path, '/assets/graph-symbols/' + (node.id() === 'lost' ? 'key' : models[type]) + '.png');
                assert.equal(readFileSync(new URL('../web' + path, import.meta.url)).toString('ascii',1,4), 'PNG');
                assert.ok(node.style('background-image').includes(path));
            }
            assert.equal(cy.$id('lost').data('displayLabel'), '旧钥匙 · 已毁灭');
            assert.equal(Number(cy.$id('lost').style('background-image-opacity')), 0.65);
            assert.match(s.el('graph-legend').innerHTML, /graph-symbols\/location.png[^>]*>地点/);
            assert.match(s.el('graph-entity').innerHTML, /旧钥匙 · 已毁灭（物品）/);
            assert.deepEqual(cy.edges()[0].data(), elements.at(-1).data);
            assert.equal(cy.edges().style('target-arrow-color'), dark ? 'rgb(126,147,140)' : 'rgb(147,165,155)');
        } finally { cy.destroy(); }
    }
    s.run("inspectGraphEntity('LOCATION')");
    assert.equal(s.run("cyInstance.edges('.muted').length"), 1);
    assert.equal(s.run("cyInstance.$id('ITEM').hasClass('muted')"), true);
    s.run("inspectGraphEntity('')");
    assert.equal(s.run("cyInstance.elements('.muted, .inspected, :selected').length"), 0);
    s.run('cyInstance.destroy()');
});

test('generation counts are validated instead of silently defaulted or rounded', async () => {
    const s = setup();
    s.el('new-name').value = 'new';
    s.el('new-idea').value = 'idea';
    for (const raw of ['', '0', '-1', '2.5', '21', '3x']) {
        s.el('new-chapters').value = raw;
        s.el('new-volumes').value = '3';
        await assert.rejects(s.run('startAutoGenerate()'), /首批章节数必须/);
    }
    assert.equal(s.requests.length, 0);
    s.el('new-chapters').value = '20';
    s.el('new-volumes').value = '10';
    const valid = s.run('startAutoGenerate()');
    assert.equal(JSON.parse(s.requests[0].options.body).chapters, 20);
    assert.equal(JSON.parse(s.requests[0].options.body).volumes, 10);
    s.respond(s.requests[0], {status: 'started'});
    await valid;
});

test('dialog focus loops from the root and at both ends without escaping', () => {
    const s = setup();
    const mask = s.el('mask'), root = s.el('root'), first = s.el('first'), last = s.el('last');
    first.offsetParent = last.offsetParent = mask;
    first.focus = () => { s.document.activeElement = first; };
    last.focus = () => { s.document.activeElement = last; };
    mask.querySelectorAll = () => [first, last];
    s.document.querySelectorAll = () => [mask];
    const handler = s.document.handlers.get('keydown')[0];
    for (const [active, shiftKey, expected] of [[root,true,last], [last,false,first], [first,true,last]]) {
        let prevented = false;
        s.document.activeElement = active;
        handler({key: 'Tab', shiftKey, preventDefault() { prevented = true; }});
        assert.equal(prevented, true);
        assert.equal(s.document.activeElement, expected);
    }
    assert.match(s.run('FOCUSABLE'), /summary/);
    assert.match(s.run('FOCUSABLE'), /input:enabled/);
});

test('config cannot save before loading and a late response cannot overwrite the reopened form', async () => {
    const s = setup();
    const old = s.run('openConfigModal()');
    assert.equal(s.el('config-submit').disabled, true);
    await s.run('saveConfig()');
    assert.equal(s.requests.length, 1);
    s.run("closeModal('modal-config')");
    const latest = s.run('openConfigModal()');
    s.respond(s.requests[1], {model: 'new', base_url: 'https://new.example/v1'});
    await latest;
    s.el('cfg-model').value = 'unsaved choice';
    s.respond(s.requests[0], {model: 'old', base_url: 'https://old.example/v1'});
    await old;
    assert.equal(s.el('cfg-model').value, 'unsaved choice');
    assert.equal(s.el('config-submit').disabled, false);
});

test('a failed config read keeps saving disabled, and a late save cannot close a new form', async () => {
    const s = setup();
    const failed = s.run('openConfigModal()');
    s.respond(s.requests[0], {detail: 'unavailable'}, 503);
    await failed;
    assert.equal(s.el('config-submit').disabled, true);
    s.run("closeModal('modal-config')");
    const opened = s.run('openConfigModal()');
    s.respond(s.requests[1], {model: 'demo', base_url: 'https://demo.example/v1'});
    await opened;
    const saving = s.run('saveConfig()');
    s.run("closeModal('modal-config')");
    const reopened = s.run('openConfigModal()');
    s.respond(s.requests[3], {model: 'second', base_url: 'https://demo.example/v1'});
    await reopened;
    s.respond(s.requests[2], {status: 'success'});
    await saving;
    assert.equal(s.el('modal-config').classList.contains('open'), true);
    assert.equal(s.el('cfg-model').value, 'second');
});

test('late selection responses cannot paint another book or the same book from an older request', async () => {
    for (const secondId of ['b', 'a']) {
        const s = setup();
        const old = s.run("selectNovel('a')");
        const latest = s.run(`selectNovel('${secondId}')`);
        s.respond(s.requests[1], detail(secondId, 'b'.repeat(64)));
        await latest;
        s.respond(s.requests[0], detail('a', 'a'.repeat(64)));
        await old;
        assert.equal(s.run('novelData.revision'), 'b'.repeat(64));
        assert.equal(s.run('currentNovelId'), secondId);
        assert.deepEqual(Array.from(s.run('rendered')), [secondId]);
    }
});

test('late selection errors cannot hide the newly selected book', async () => {
    const s = setup();
    const old = s.run("selectNovel('a')");
    const latest = s.run("selectNovel('b')");
    s.respond(s.requests[1], detail('b'));
    await latest;
    s.requests[0].reject(new Error('old request disconnected'));
    await old;
    assert.equal(s.run('currentNovelId'), 'b');
    assert.equal(s.run('toasts.length'), 0);
});

test('the reader opens and switches books while a previous compliance scan is pending', async () => {
    const s = setup({workspace:true});
    const first = s.run("selectNovel('a')");
    s.respond(s.requests[0], {...detail('a'), chapters:chapters([1])});
    assert.equal(await first, true);
    assert.equal(s.el('novel-workspace').hasAttribute('inert'), false);
    assert.equal(s.requests[1].url, '/api/novel/a/compliance');

    const second = s.run("selectNovel('b')");
    s.respond(s.requests[2], {...detail('b'), chapters:chapters([1])});
    assert.equal(await second, true);
    s.respond(s.requests[1], {revision:'a'.repeat(64), chapters:[{chapter_number:1,hits:[{word:'旧稿'}]}]});
    s.respond(s.requests[3], {revision:'b'.repeat(64), chapters:[{chapter_number:1,hits:[{word:'新稿'}]}]});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(s.run('window._complianceMap[1][0].word'), '新稿');
    assert.equal(s.run('window._compliancePending'), false);
});

test('chapter responses require the exact directory revision', async () => {
    const s = setup();
    s.run("var chapter = {_novelId:'a', _revision:'old', chapter_number:1}");
    const pending = s.run('ensureChapterLoaded(chapter)');
    s.respond(s.requests[0], {novel_id: 'a', revision: 'new', chapter: {chapter_number: 1, full_text: 'new'}});
    await assert.rejects(pending, /作品已更新/);
    assert.equal(s.run('chapter.full_text'), undefined);
    assert.equal(s.run('chapter._loadPromise'), undefined);
});

test('late chapter failures cannot replace the current chapter body', async () => {
    const s = setup();
    s.bind();
    s.run("sortedChapters = [1,2].map(n => ({_novelId:'book', _revision:'a'.repeat(64), chapter_number:n,title:'章节',word_count:2}))");
    const old = s.run('showChapter(0)');
    const latest = s.run('showChapter(1)');
    assert.equal(s.el('btn-chapter-edit').disabled, true);
    s.respond(s.requests[1], {novel_id:'book',revision:'a'.repeat(64),chapter:{chapter_number:2,full_text:'最新正文'}});
    await latest;
    const currentHtml = s.el('current-chapter-body').innerHTML;
    s.requests[0].reject(new Error('late failure'));
    await old;
    assert.equal(s.el('btn-chapter-edit').disabled, false);
    assert.match(currentHtml, /最新正文/);
    assert.equal(s.el('current-chapter-body').innerHTML, currentHtml);
});

test('a chapter editor stays bound to its opening project and version', async () => {
    const s = setup();
    s.bind('a');
    s.run("sortedChapters = [{chapter_number:1,title:'A',full_text:'旧文',word_count:2}]; openChapterEditModal()");
    s.el('ce-text').value = '编辑中的 A 文本';
    s.bind('b', 'b'.repeat(64));
    const save = s.run('saveChapterEdit()');
    assert.equal(s.requests[0].url, '/api/novel/a/chapter_text');
    assert.deepEqual(JSON.parse(s.requests[0].options.body), {
        chapter: 1, text: '编辑中的 A 文本', expected_revision: 'a'.repeat(64),
    });
    s.respond(s.requests[0], {status:'success'});
    await save;
    assert.equal(s.run('currentNovelId'), 'b');
    assert.equal(s.requests.length, 1); // no stolen navigation after the late save
});

test('an old recovered draft cannot write over new chapter text without an explicit comparison decision', async () => {
    const s = setup();
    s.bind('book', 'a'.repeat(64));
    s.run("sortedChapters = [{chapter_number:1,title:'旧标题',full_text:'原始正文'}]; openChapterEditModal()");
    s.el('ce-text').value = '未提交的草稿';
    s.run('stashChapterDraft()');
    s.bind('book', 'b'.repeat(64));
    s.run("sortedChapters = [{chapter_number:1,title:'新标题',full_text:'另一处保存的正式稿'}]; openChapterEditModal()");
    const blocked = s.run('saveChapterEdit()');
    assert.equal(s.requests.length, 0);
    await blocked;
    assert.equal(s.el('ce-conflict-base').textContent, '旧标题\n\n原始正文');
    assert.equal(s.el('ce-conflict-current').textContent, '新标题\n\n另一处保存的正式稿');
    assert.equal(s.el('ce-text').value, '未提交的草稿');
    s.run('acceptRecoveredDraft()');
    s.run('refreshProjectContext = async () => false');
    const save = s.run('saveChapterEdit()');
    assert.equal(JSON.parse(s.requests[0].options.body).expected_revision, 'b'.repeat(64));
    s.respond(s.requests[0], {});
    await save;
});

test('a project-only revision change does not block a draft on the same chapter baseline', async () => {
    const s = setup();
    const open = "sortedChapters = [{chapter_number:1,title:'A',full_text:'正式稿'}]; openChapterEditModal()";
    s.bind('book', 'a'.repeat(64));
    s.run(open);
    s.el('ce-text').value = '继续写';
    s.run('stashChapterDraft()');
    s.bind('book', 'b'.repeat(64));
    s.run(open);
    s.run('refreshProjectContext = async () => false');
    const save = s.run('saveChapterEdit()');
    assert.equal(s.requests.length, 1);
    assert.equal(JSON.parse(s.requests[0].options.body).text, '继续写');
    s.respond(s.requests[0], {});
    await save;
});

test('an old save acknowledgement does not close a newly opened editor', async () => {
    const s = setup();
    s.bind('a');
    s.run("sortedChapters = [{chapter_number:1,title:'A',full_text:'旧文',word_count:2}]; openChapterEditModal()");
    s.el('ce-text').value = '旧弹窗提交';
    const save = s.run('saveChapterEdit()');
    s.bind('b');
    s.run("sortedChapters = [{chapter_number:2,title:'B',full_text:'B文',word_count:2}]; openChapterEditModal()");
    s.respond(s.requests[0], {});
    await save;
    assert.equal(s.el('modal-chapter').classList.contains('open'), true);
    assert.equal(s.el('ce-text').value, 'B文');
});

test('saving a fallback tab draft does not delete a newer draft held by another tab', async () => {
    const s = setup();
    s.bind('book');
    s.run(`globalThis.sessionStorage={values:new Map(),getItem(key){return this.values.get(key)},
        setItem(key,value){this.values.set(key,value)},removeItem(key){this.values.delete(key)}};
        var removedPersistent=0; localStorage.removeItem=()=>removedPersistent++;
        sortedChapters=[{chapter_number:1,title:'A',full_text:'正式稿'}];
        refreshProjectContext=async()=>false; openChapterEditModal();
        var storageKey=chapterDraftKey(captureProjectContext(),1);
        localStorage.setItem(storageKey, JSON.stringify({text:'另一标签页的草稿'}));`);
    s.el('ce-text').value = '当前标签页的草稿';
    s.run('stashChapterDraft()');
    const saving = s.run('saveChapterEdit()');
    s.respond(s.requests[0], {});
    await saving;
    assert.equal(s.run('removedPersistent'), 0);
    assert.equal(s.run('sessionStorage.values.has(storageKey)'), false);
});

test('opening a dialog does not steal focus from an input already selected before the next frame', () => {
    const s = setup();
    s.run(`var openingFrame;
        requestAnimationFrame = run => { openingFrame=run };
        const dialog = document.getElementById('test-dialog');
        const mask = document.getElementById('test-mask');
        const input = document.getElementById('test-input');
        dialog.contains = element => element === input;
        mask.querySelector = () => dialog;
        openModal('test-mask'); input.focus(); openingFrame();`);
    assert.equal(s.document.activeElement, s.el('test-input'));
});

test('a conflict preserves unsaved text and keeps the editor open', async () => {
    const s = setup();
    s.bind();
    s.run("sortedChapters = [{chapter_number:1,title:'A',full_text:'旧文',word_count:2}]; openChapterEditModal()");
    s.el('ce-text').value = '不能丢失的输入';
    const save = s.run('saveChapterEdit()');
    s.respond(s.requests[0], {detail:'作品已更新'}, 409);
    await save;
    assert.equal(s.el('ce-text').value, '不能丢失的输入');
    assert.equal(s.el('modal-chapter').classList.contains('open'), true);
    assert.equal(s.requests.length, 1);
});

test('after a rejected save the editor can load the latest official text without dropping the draft', async () => {
    const s = setup();
    s.bind('book');
    s.run(`sortedChapters=[{chapter_number:1,title:'A',full_text:'原正文'}]; openChapterEditModal();
        ensureChapterLoaded=async()=>{};
        refreshProjectContext=async()=>{ novelData.revision='b'.repeat(64); novelLoadToken++;
            sortedChapters=[{chapter_number:1,title:'A',full_text:'新正文'}]; return true; };`);
    s.el('ce-text').value = '尚未保存的输入';
    const save = s.run('saveChapterEdit()');
    s.respond(s.requests[0], {detail:'作品已更新'}, 409);
    await save;
    assert.equal(s.el('ce-reload').classList.contains('hidden'), false);
    await s.run('compareLatestChapterDraft()');
    assert.equal(s.el('ce-text').value, '尚未保存的输入');
    assert.match(s.el('ce-conflict-current').textContent, /新正文/);
    assert.equal(s.el('ce-save').disabled, true);
    assert.equal(s.requests.length, 1);
});

test('character edits surface the server effective-chapter decision', async () => {
    const s = setup();
    s.bind('a');
    s.run(`
        novelData.characters=[{name:'角色',role:'主角',background:'',personality_tags:[],
            current_state:{level:'旧境界',location:'旧地',hp_status:'健康',current_mood:'平静'}}];
        refreshProjectContext=async () => {};
        openCharacterModal('角色');
    `);
    s.el('ch-level').value = '人工新境界';
    const saving = s.run('saveCharacter()');
    assert.deepEqual(JSON.parse(s.requests[0].options.body), {
        name:'角色', role:'主角', level:'人工新境界', location:'旧地', hp_status:'健康',
        current_mood:'平静', personality_tags:[], background:'', expected_revision:'a'.repeat(64),
    });
    s.respond(s.requests[0], {
        message:'角色「角色」已更新；动态状态从第 2 章结束后生效',
        effective_after_chapter:2, revision:'b'.repeat(64),
    });
    await saving;
    assert.match(s.run('toasts.at(-1).message'), /第 2 章结束后生效/);
    assert.equal(s.el('modal-character').classList.contains('open'), false);
});

test('history repair preview stays version-bound and submits only explicitly checked character state', async () => {
    const s=setup();
    s.bind('a');
    s.run('refreshProjectContext=async()=>true');
    const opening=s.run('openHistoryRepairModal()');
    s.respond(s.requests[0], {
        novel_id:'a', revision:'a'.repeat(64), eligible:true,
        confirmation:'我已核对第1章开始前状态', warnings:['不倒推历史'], load_warnings:[],
        timeline_status:{mode:'legacy_snapshots',baseline_chapter:null},
        characters:[{name:'角色<script>',role:'主角',initial_state:{
            level:'凡人',location:'山村',hp_status:'健康',equipment_and_items:['玉佩'],
            skills_and_abilities:[],current_mood:'困惑',relations:{师父:'敬重'},is_alive:true,
        }}],
    });
    await opening;
    assert.equal(s.el('history-repair-fields').disabled,false);
    assert.equal(s.el('history-repair-submit').disabled,false);
    assert.match(s.el('history-repair-characters').innerHTML,/角色&lt;script&gt;/);
    s.el('history-use-0').checked=true;
    s.el('history-level-0').value='练气';
    s.el('history-location-0').value='山门';
    s.el('history-hp-0').value='轻伤';
    s.el('history-mood-0').value='警惕';
    s.el('history-items-0').value='玉佩，木剑';
    s.el('history-skills-0').value='吐纳术';
    s.el('history-alive-0').value='true';
    s.el('history-relations-0').value='{"师父":"敬重"}';
    s.el('history-repair-note').value='已对照原稿逐项核对';
    s.el('history-repair-confirmation').value='我已核对第1章开始前状态';
    const saving=s.run('submitHistoryRepair()');
    assert.deepEqual(JSON.parse(s.requests[1].options.body),{
        confirmation:'我已核对第1章开始前状态',author_note:'已对照原稿逐项核对',
        characters:[{name:'角色<script>',level:'练气',location:'山门',hp_status:'轻伤',
            equipment_and_items:['玉佩','木剑'],skills_and_abilities:['吐纳术'],current_mood:'警惕',
            relations:{师父:'敬重'},is_alive:true}],expected_revision:'a'.repeat(64),
    });
    s.respond(s.requests[1],{novel_id:'a',revision:'b'.repeat(64),message:'修复已开始'});
    await saving;
    assert.equal(s.el('modal-history-repair').classList.contains('open'),false);
    assert.equal(s.run('toasts.at(-1).message'),'修复已开始');
});

test('late history preview cannot replace a reopened dialog and malformed relations never submit', async () => {
    const s=setup();
    s.bind('a');
    const old=s.run('openHistoryRepairModal()');
    s.run("closeModal('modal-history-repair')");
    const latest=s.run('openHistoryRepairModal()');
    const preview={novel_id:'a',revision:'a'.repeat(64),eligible:true,confirmation:'我已核对第1章开始前状态',
        warnings:[],load_warnings:[],timeline_status:{mode:'legacy_snapshots'},characters:[]};
    s.respond(s.requests[1],preview);
    await latest;
    s.el('history-repair-note').value='用户正在填写';
    s.respond(s.requests[0],{...preview,eligible:false,timeline_status:{mode:'invalid'}});
    await old;
    assert.equal(s.el('history-repair-note').value,'用户正在填写');

    s.run("document.getElementById('modal-history-repair')._historyPreview={..." + JSON.stringify(preview) + ",characters:[{name:'角色'}]}");
    s.el('history-use-0').checked=true;
    for (const [id,value] of [['history-level-0','凡人'],['history-location-0','山村'],['history-hp-0','健康'],['history-mood-0','平静'],['history-alive-0','true']]) s.el(id).value=value;
    s.el('history-relations-0').value='[]';
    s.el('history-repair-confirmation').value='我已核对第1章开始前状态';
    await assert.rejects(s.run('submitHistoryRepair()'),/态度映射/);
    assert.equal(s.requests.length,2);
});

test('an initialized history repair points to the next chapter and exposes state-only extraction', async () => {
    const s=setup();
    s.bind('a');
    s.run(`
        sortedChapters=${JSON.stringify(chapters([1,2]))};
        novelData.timeline_status={baseline_chapter:0,pending_chapters:[1,2]};
        novelData.state_reconciliation={required:true,chapters:[1,2]};
        selectedChapterNum=2;
        updateStateReconcileButton();
    `);
    assert.equal(s.el('btn-state-reconcile').classList.contains('hidden'),true);
    const opening=s.run('openHistoryRepairModal()');
    s.respond(s.requests[0],{
        novel_id:'a',revision:'a'.repeat(64),eligible:false,confirmation:'我已核对第1章开始前状态',
        warnings:[],load_warnings:[],characters:[],
        timeline_status:{mode:'event_timeline',baseline_chapter:0,pending_chapters:[1,2],event_count:0,current_chapter:2},
    });
    await opening;
    assert.equal(s.el('history-repair-next').classList.contains('hidden'),false);
    assert.match(s.el('history-repair-next').innerHTML,/第 1 章/);
    assert.equal(s.el('history-repair-submit').classList.contains('hidden'),true);
    await s.run('goToNextHistoryRepairChapter()');
    assert.equal(s.run('selectedChapterNum'),1);
    assert.equal(s.el('btn-state-reconcile').classList.contains('hidden'),false);
    assert.match(s.run('toasts.at(-1).message'),/提取本章状态/);
});

test('state-only extraction confirmation remains bound to its opening project and chapter', async () => {
    const s=setup();
    s.bind('a');
    s.run(`
        novelData.timeline_status={baseline_chapter:0};
        novelData.state_reconciliation={required:true,chapters:[1]};
        selectedChapterNum=1;
        confirmStateReconciliation();
    `);
    s.bind('b');
    s.run('selectedChapterNum=9');
    const confirmation=s.run('doConfirm()');
    assert.equal(s.requests[0].url,'/api/novel/a/history_repair/1/extract');
    assert.deepEqual(JSON.parse(s.requests[0].options.body),{expected_revision:'a'.repeat(64)});
    s.respond(s.requests[0],{message:'状态提取已启动'});
    await confirmation;
    assert.equal(s.run('toasts.some(item=>item.message===\'状态提取已启动\')'),false);
});

test('late paragraph candidates never attach to a newer rewrite dialog', async () => {
    const s = setup();
    s.bind();
    s.run("sortedChapters = [{chapter_number:1,title:'A',full_text:'第一段。\\n\\n第二段。'}]; openRewriteModal(1,0)");
    s.el('rw-instruction').value = '缩短';
    const rewrite = s.run('doParaRewrite()');
    s.run('openRewriteModal(1,1)');
    s.respond(s.requests[0], {original:'第一段。',rewritten:'旧候选',revision:'a'.repeat(64)});
    await rewrite;
    assert.equal(s.run('rewritingPara.candidate'), undefined);
    assert.equal(s.el('rw-apply').disabled, true);
});

test('continuation setup and chapter rewrite review bind their opening project revision', () => {
    const s = setup();
    s.bind('sample');
    s.run("openModal('modal-continuation-plan'); openModal('modal-chapter-rewrite')");
    for (const id of ['modal-continuation-plan', 'modal-chapter-rewrite']) {
        assert.equal(s.el(id)._projectContext.novelId, 'sample');
        assert.equal(s.el(id)._projectContext.revision, 'a'.repeat(64));
    }
});

test('regeneration confirmation captures chapter and project before selection changes', async () => {
    const s = setup();
    s.bind('a');
    const prepare = s.run('selectedChapterNum=3; confirmRegenerate()');
    s.respond(s.requests[0], { candidate: null });
    await prepare;
    s.bind('b');
    s.run('selectedChapterNum=8');
    const confirm = s.run('doConfirm()');
    assert.deepEqual(JSON.parse(s.requests[1].options.body), {name:'a',chapter:3,preview:true,expected_revision:'a'.repeat(64)});
    s.respond(s.requests[1], {});
    await confirm;
});

test('late bookshelf failures cannot erase a successful refresh', async () => {
    const s = setup();
    const old = s.run('loadNovels()');
    const latest = s.run('loadNovels()');
    s.respond(s.requests[1], {novels:[{novel_id:'new'}]});
    await latest;
    s.requests[0].reject(new Error('older outage'));
    await old;
    assert.equal(s.run('window._novels[0].novel_id'), 'new');
    assert.equal(s.el('novels-list').innerHTML, '');
});

test('recovery diagnostics remain visible, scoped and HTML-escaped', async () => {
    const s = setup();
    const loading = s.run('loadNovels()');
    s.respond(s.requests[0], {
        novels: [], warnings: [{novel_id:'book', message:'项目状态无法读取'}],
        recovery: {
            requires_attention: true, blocks_all_projects: true,
            journals: [{
                journal_id:'a'.repeat(32), code:'corrupt_transaction_header', severity:'error',
                message:'<img src=x onerror=alert(1)>', project_ids:[], preserved:true,
            }],
            guidance: ['不要直接删除事务目录'],
        },
    });
    await loading;
    assert.equal(s.el('recovery-alert').classList.contains('hidden'), false);
    assert.equal(s.el('import-trigger').disabled, true);
    assert.match(s.el('recovery-alert-text').textContent, /1 个事务现场/);
    s.run("openModal('modal-create')");
    assert.equal(s.el('modal-create').classList.contains('open'), false);
    assert.equal(s.el('modal-recovery').classList.contains('open'), true);
    assert.match(s.run('toasts.at(-1).message'), /暂停新建/);
    s.run('openRecoveryModal()');
    assert.match(s.el('recovery-diagnostics').innerHTML, /&lt;img/);
    assert.doesNotMatch(s.el('recovery-diagnostics').innerHTML, /<img src=x/);
    assert.match(s.el('recovery-diagnostics').innerHTML, /归属未知/);
    assert.match(s.el('recovery-diagnostics').innerHTML, /不要直接删除事务目录/);
});

test('audit history is escaped and human review advances the bound revision', async () => {
    const s = setup();
    s.bind('a');
    s.run(`
        sortedChapters = [{
            chapter_number:1, title:'第一章', word_count:4, full_text:'当前正文', audit_status:'failed',
            final_audit:{text_sha256:'f'.repeat(64), consistency_score:6, service_error:false},
            audit_history:[{
                passed:false, consistency_score:6, pacing_score:5, cliffhanger_detected:false,
                critique:'<img src=x onerror=alert(1)>', logic_issues:['<b>漏洞</b>'],
                character_ooc_issues:[], revision_directives:['修复'], text_sha256:'f'.repeat(64),
                stage:'draft', attempt:1, service_error:false,
            }],
            human_review_history:[], current_human_review:null, human_review_status:'unreviewed',
            _novelId:'a', _revision:'a'.repeat(64),
        }];
        novelData.chapters = sortedChapters; selectedChapterIdx=0; selectedChapterNum=1;
    `);
    await s.run('openAuditHistoryModal()');
    assert.match(s.el('audit-history-list').innerHTML, /&lt;img/);
    assert.doesNotMatch(s.el('audit-history-list').innerHTML, /<img src=x/);
    assert.equal(s.run('humanReviewDecision()'), '');
    assert.equal(s.el('save-human-review').disabled, true);
    await assert.rejects(s.run('saveHumanReview()'), /请选择本次审阅结论/);
    s.el('review-changes').checked = true;
    s.el('human-review-note').value = '  ';
    s.run('syncHumanReviewForm()');
    assert.equal(s.el('human-review-note').required, true);
    assert.equal(s.el('save-human-review').disabled, true);
    await assert.rejects(s.run('saveHumanReview()'), /请填写具体意见/);
    assert.equal(s.requests.length, 0);
    s.el('review-changes').checked = false;
    s.el('review-approved').checked = true;
    s.el('human-review-note').value = '作者已通读';
    const saving = s.run('saveHumanReview()');
    assert.deepEqual(JSON.parse(s.requests[0].options.body), {
        decision:'approved', note:'作者已通读', expected_revision:'a'.repeat(64),
    });
    const review = {decision:'approved', note:'作者已通读', reviewed_at:'2026-08-30T12:00:00Z', text_sha256:'f'.repeat(64)};
    s.respond(s.requests[0], {
        status:'success', message:'已记录', review, human_review_status:'approved',
        revision:'b'.repeat(64), novel_id:'a', chapter_number:1,
    });
    await saving;
    assert.equal(s.run('novelData.revision'), 'b'.repeat(64));
    assert.equal(s.run('sortedChapters[0]._revision'), 'b'.repeat(64));
    assert.equal(s.run('sortedChapters[0].current_human_review.note'), '作者已通读');
    assert.match(s.el('current-chapter-meta').textContent, /人工已认可/);
    assert.equal(s.el('human-review-note').value, '');
    assert.equal(s.run('humanReviewDecision()'), '');
    assert.equal(s.el('save-human-review').disabled, true);
});

test('human review drafts are isolated by chapter and by its prose version', async () => {
    const s = setup();
    s.bind('book');
    s.run(`ensureChapterLoaded = async () => {};
        sortedChapters = [{chapter_number:1,title:'前章',full_text:'第一章',prose_sha256:'first'},
                          {chapter_number:2,title:'当前章',full_text:'第二章',prose_sha256:'second'}];
        selectedChapterIdx = 0; selectedChapterNum = 1;`);
    await s.run('openAuditHistoryModal()');
    s.el('human-review-note').value = '仅针对第一章';
    s.el('review-changes').checked = true;
    s.run("closeModal('modal-audit-history'); selectedChapterIdx = 1; selectedChapterNum = 2;");
    await s.run('openAuditHistoryModal()');
    assert.equal(s.el('human-review-note').value, '');
    assert.equal(s.run('humanReviewDecision()'), '');
    s.el('review-approved').checked = true;
    s.run("closeModal('modal-audit-history'); selectedChapterIdx = 0; selectedChapterNum = 1;");
    await s.run('openAuditHistoryModal()');
    assert.equal(s.el('human-review-note').value, '仅针对第一章');
    assert.equal(s.run('humanReviewDecision()'), 'changes_requested');
    s.run("closeModal('modal-audit-history'); sortedChapters[0].prose_sha256='changed'");
    await s.run('openAuditHistoryModal()');
    assert.equal(s.el('human-review-note').value, '');
    assert.equal(s.run('humanReviewDecision()'), '');
    s.run("closeModal('modal-audit-history'); selectedChapterIdx = 1; selectedChapterNum = 2;");
    await s.run('openAuditHistoryModal()');
    assert.equal(s.run('humanReviewDecision()'), 'approved');
    assert.equal(s.el('save-human-review').disabled, false);
});

test('opening source evidence keeps focus in the same review without changing the chapter', async () => {
    const s = setup();
    s.bind('book');
    s.run(`ensureChapterLoaded = async () => {};
        sortedChapters = [{chapter_number:1,title:'前章',full_text:'线索就在这里。\\n\\n上下文。'},
                          {chapter_number:2,title:'当前章',full_text:'第二章'}];
        selectedChapterIdx = 1; selectedChapterNum = 2;`);
    await s.run('openAuditHistoryModal()');
    s.el('human-review-note').value = '继续输入这条意见';
    await s.run("focusAuditQuote(1,'线索就在这里。')");
    assert.equal(s.run('selectedChapterNum'), 2);
    assert.equal(s.el('modal-audit-history').classList.contains('open'), true);
    assert.equal(s.el('human-review-note').value, '继续输入这条意见');
    assert.match(s.el('audit-source-text').innerHTML, /<mark>线索就在这里。<\/mark>/);
});

test('the audit summary distinguishes partial retrieval and an unavailable model from a clean book', () => {
    const s = setup();
    s.run(`var checked = {chapter_number:7,title:'夜读',final_audit:{stage:'manual',findings:[],evidence_chapters:[2],
        retrieval_coverage:{eligible_chapters:6,searched_chapters:4,fully_searched_chapters:3,query_truncated:true}}};
        renderAuditHistory(checked);`);
    assert.match(s.el('audit-coverage').textContent, /完整检索 3\/6 章/);
    assert.match(s.el('audit-coverage').textContent, /本章仅部分内容用于检索/);
    assert.match(s.el('audit-coverage').textContent, /这不是全书通过/);
    s.run('checked.final_audit.service_error=true; renderAuditHistory(checked)');
    assert.match(s.el('audit-coverage').textContent, /自动检查未完成/);
    assert.doesNotMatch(s.el('audit-coverage').textContent, /未发现/);
});

test('composition keys do not dismiss surfaces or execute palette commands', () => {
    const s = setup();
    s.run('setSidebarOpen(true)');
    let prevented = false;
    s.document.handlers.get('keydown')[0]({key:'Escape',isComposing:true,preventDefault(){ prevented = true; }});
    assert.equal(prevented, false);
    assert.equal(s.el('sidebar').classList.contains('open'), true);
    s.run("var compositionCommands=0; paletteItems=[{run:()=>compositionCommands++}]; paletteKeydown({key:'Enter',keyCode:229,preventDefault(){},stopPropagation(){}})");
    assert.equal(s.run('compositionCommands'), 0);
});

test('late human review acknowledgement never paints a newly selected project', async () => {
    const s = setup();
    s.bind('a');
    s.run(`
        sortedChapters=[{chapter_number:1,title:'A',word_count:1,full_text:'A',audit_history:[],
            human_review_history:[],human_review_status:'unreviewed',_novelId:'a',_revision:'a'.repeat(64)}];
        novelData.chapters=sortedChapters; selectedChapterIdx=0; selectedChapterNum=1;
    `);
    await s.run('openAuditHistoryModal()');
    s.el('review-approved').checked = true;
    const saving = s.run('saveHumanReview()');
    s.bind('b', 'b'.repeat(64));
    s.run("sortedChapters=[]; selectedChapterNum=0");
    s.respond(s.requests[0], {
        review:{decision:'approved',note:'old',reviewed_at:'2026-08-30T12:00:00Z',text_sha256:'f'.repeat(64)},
        human_review_status:'approved', revision:'c'.repeat(64), novel_id:'a', chapter_number:1,
    });
    await saving;
    assert.equal(s.run('currentNovelId'), 'b');
    assert.equal(s.run('novelData.revision'), 'b'.repeat(64));
    assert.equal(s.el('current-chapter-meta').textContent, '');
});

test('task polling never overlaps slow requests or steals the current book', async () => {
    const s = setup();
    s.bind('b');
    s.run("wasRunning=true; var refreshes=0; loadNovels=async () => {refreshes++}");
    const first = s.run('refreshTaskStatus()');
    await s.run('refreshTaskStatus()');
    assert.equal(s.requests.length, 1);
    s.respond(s.requests[0], {is_running:false,current_novel:'a',current_step:'已完成',task_id:'one'});
    await first;
    assert.equal(s.run('currentNovelId'), 'b');
    assert.equal(s.run('refreshes'), 1);
    assert.equal(s.run('pollInFlight'), false);
});

test('status polling backs off while idle and hidden but stays responsive during work', async () => {
    const s = setup();
    assert.equal(s.run('statusPollDelay()'), 8000);
    s.run("expectedTaskId='pending'");
    assert.equal(s.run('statusPollDelay()'), 1600);
    s.document.hidden = true;
    assert.equal(s.run('statusPollDelay()'), 30000);
    s.document.hidden = false;

    s.run("expectedTaskId=''; pollStatus()");
    s.respond(s.requests[0], {is_running:false,current_step:'待命',task_id:''});
    await new Promise(resolve => setImmediate(resolve));
    const activeTimers = s.timers.filter(timer => !timer.cancelled);
    assert.equal(activeTimers.at(-1).delay, 8000);
});

test('welcome resumes the last opened project and falls back to the library order', () => {
    const s = setup();
    s.run(`window._novels = [
        { novel_id: 'new', title: '新作品', total_generated_chapters: 4 },
        { novel_id: 'recent', title: '再次打开 <书>', total_generated_chapters: 12 },
    ]`);
    s.run("localStorage.setItem('moirai_recent_project', 'recent'); renderRecentWork()");
    assert.match(s.el('recent-work-content').innerHTML, /再次打开 &lt;书&gt;/);
    assert.match(s.el('recent-work-content').innerHTML, /12 章/);
    s.run("localStorage.setItem('moirai_recent_project', 'missing'); renderRecentWork()");
    assert.match(s.el('recent-work-content').innerHTML, /新作品/);
    s.run('window._novels = []; renderRecentWork()');
    assert.ok(s.el('recent-work').classList.contains('hidden'));
    assert.equal(s.el('recent-work-content').innerHTML, '');
});

test('a late desk excerpt cannot replace the newly featured manuscript', async () => {
    const s = setup();
    s.run("window._novels = [{novel_id:'a', current_chapter:2},{novel_id:'b', current_chapter:3}]");
    const old = s.run('renderRecentWork()');
    const current = s.run("localStorage.setItem('moirai_recent_project','b'); renderRecentWork()");
    s.respond(s.requests[1], {novel_id:'b', chapter:{chapter_number:3, title:'舟', excerpt:'乙'}});
    await current;
    s.respond(s.requests[0], {novel_id:'a', chapter:{chapter_number:2, title:'桥', excerpt:'甲'}});
    await old;
    assert.equal(s.el('desk-excerpt').textContent, '乙');
    assert.match(s.el('desk-chapter-title').textContent, /第 3 章.*舟/);
});

test('manuscript preview preserves ordered chapter prose and introductory matter', () => {
    const s = setup();
    const source = '卷首独白\r\n\r\n## 第一章 起雾\r\n\r\n长街上没有电。\r\n\r\n潮水还在涨。\r\n# Chapter 2 Crossing\r\n她提灯过桥。';
    const preview = s.run(`parseManuscript(${JSON.stringify(source)}).map(item => [item.title, item.text])`);
    assert.deepEqual(JSON.parse(JSON.stringify(preview)), [
        ['序章', '卷首独白'],
        ['起雾', '长街上没有电。\n\n潮水还在涨。'],
        ['Crossing', '她提灯过桥。'],
    ]);
    assert.equal(s.run("parseManuscript('单章正文。')[0].text"), '单章正文。');
});

test('starting task polling requests status immediately instead of waiting for the interval', async () => {
    const s = setup();
    s.run('pollStatus()');
    assert.equal(s.requests.length, 1);
    s.respond(s.requests[0], {is_running:false,current_step:'待命',task_id:''});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(s.run('observedTaskId'), '');
    assert.equal(s.run('pollInFlight'), false);
});

test('an expected task that finishes before the first poll still refreshes the workspace', async () => {
    const s = setup();
    s.bind('a');
    s.run("var shelfRefreshes=0,projectRefreshes=0; loadNovels=async()=>{shelfRefreshes++}; refreshProjectContext=async()=>{projectRefreshes++;return true}");
    s.run("pollStatus('fast-task')");
    s.respond(s.requests[0], {
        is_running:false, current_novel:'a', current_step:'读者推演完成',
        progress:100, error:'', task_id:'fast-task',
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(s.run('shelfRefreshes'), 1);
    assert.equal(s.run('projectRefreshes'), 1);
    assert.equal(s.run('expectedTaskId'), '');
});

test('a failed poll releases the in-flight guard for the next attempt', async () => {
    const s = setup();
    const first = s.run('refreshTaskStatus()');
    s.requests[0].reject(new Error('offline'));
    await first;
    const retry = s.run('refreshTaskStatus()');
    assert.equal(s.requests.length, 2);
    s.respond(s.requests[1], {is_running:false});
    await retry;
});

test('custom compliance load failures preserve local state and surface the server warning', async () => {
    const s = setup();
    s.run("window._customWords={保留:['旧词']}");
    const loading = s.run('reloadCustomWords()');
    s.respond(s.requests[0], {detail:'自定义合规词库不可用'}, 503);
    assert.equal(await loading, false);
    assert.equal(s.run("window._customWords['保留'][0]"), '旧词');
    assert.equal(s.run('window._customWordsAvailable'), false);
    assert.equal(s.run('toasts.at(-1).type'), 'error');
});

test('palette arrow keys move one option, update ARIA and stop duplicate bubbling', () => {
    const s = setup();
    s.run("paletteItems=[{},{},{}]; paletteIndex=0; var prevented=0, stopped=0; paletteKeydown({key:'ArrowDown',preventDefault(){prevented++},stopPropagation(){stopped++}})");
    assert.equal(s.run('paletteIndex'), 1);
    assert.equal(s.el('palette-input').getAttribute('aria-activedescendant'), 'palette-option-1');
    assert.equal(s.run('prevented'), 1);
    assert.equal(s.run('stopped'), 1);
});

test('version-bound full-book search adds escaped prose hits and jumps to the real chapter', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    s.el('palette-input').value = '银色潮汐';
    s.run(`
        sortedChapters=[{chapter_number:1,title:'潮声之前',full_text:'正文'}];
        var shownIndex=-1;
        showChapter=index => { shownIndex=index; };
        paletteSearchToken=7;
    `);
    const loaded = s.run("loadPaletteSearch('银色潮汐',7,captureProjectContext())");
    assert.equal(s.requests[0].url, '/api/novel/a/search?q=%E9%93%B6%E8%89%B2%E6%BD%AE%E6%B1%90&limit=40');
    s.respond(s.requests[0], {
        novel_id:'a', revision:'a'.repeat(64), query:'银色潮汐',
        results:[{
            kind:'chapter', key:'chapter:1', label:'第1章 <潮声之前>',
            snippet:'旧船票背面写着 <银色潮汐>', tab:'chapters', chapter_number:1,
        }],
    });
    assert.equal(await loaded, true);
    assert.match(s.el('palette-list').innerHTML, /&lt;潮声之前&gt;/);
    assert.match(s.el('palette-list').innerHTML, /&lt;银色潮汐&gt;/);
    assert.doesNotMatch(s.el('palette-list').innerHTML, /<银色潮汐>/);
    s.run('paletteItems[0].run()');
    assert.equal(s.run('shownIndex'), 0);
});

test('late full-book search results cannot cross a project revision', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    s.el('palette-input').value = '旧查询';
    s.run('paletteSearchToken=3');
    const loaded = s.run("loadPaletteSearch('旧查询',3,captureProjectContext())");
    s.run("novelData.revision='b'.repeat(64); paletteSearchToken=4");
    s.respond(s.requests[0], {
        novel_id:'a', revision:'a'.repeat(64), query:'旧查询',
        results:[{kind:'chapter',label:'过期结果',snippet:'不应出现',chapter_number:1,tab:'chapters'}],
    });
    assert.equal(await loaded, false);
    assert.equal(s.run('paletteRemoteItems.length'), 0);
    assert.doesNotMatch(s.el('palette-list').innerHTML, /过期结果/);
});

test('full-book search normalizes repeated whitespace consistently across the request and result filter', async () => {
    const s = setup({workspace:true});
    s.bind('a');
    s.el('palette-input').value = '  silver   tide  ';
    s.run('paletteSearchToken=9');
    const loaded = s.run("loadPaletteSearch('silver tide',9,captureProjectContext())");
    assert.equal(s.requests[0].url, '/api/novel/a/search?q=silver%20tide&limit=40');
    s.respond(s.requests[0], {
        novel_id:'a', revision:'a'.repeat(64), query:'silver tide',
        results:[{kind:'concept',key:'concept:main',label:'策划案',snippet:'a silver tide arrives',tab:'outline'}],
    });
    assert.equal(await loaded, true);
    assert.match(s.el('palette-list').innerHTML, /a silver tide arrives/);
});

test('all exports use a temporary native download link for the loaded project, never popups', () => {
    const s = setup();
    const links = [];
    const attached = new Set();
    s.document.body = {appendChild: link => attached.add(link)};
    s.document.createElement = tag => {
        assert.equal(tag, 'a');
        const link = {
            click() { assert.ok(attached.has(link)); link.clicked = true; },
            remove() { attached.delete(link); },
        };
        links.push(link);
        return link;
    };
    s.run("window.open=()=>{throw new Error('Downloads must not require popups')}");
    s.bind('作品');
    for (const [fn, route, filename] of [
        ['exportBackup', 'export_backup', '作品_backup.zip'],
        ['exportMarkdown', 'export', '作品_全本.md'],
        ['exportFullTxt', 'export_txt', '作品_全本发布稿.txt'],
        ['exportEpub', 'export_epub', '作品.epub'],
    ]) {
        s.run(`${fn}()`);
        const link = links.at(-1);
        assert.equal(link.href, `/api/novel/${encodeURIComponent('作品')}/${route}`);
        assert.equal(link.download, filename);
        assert.equal(link.hidden, true);
        assert.equal(link.clicked, true);
        assert.equal(attached.size, 0);
    }
    s.run("novelData=null; exportBackup(); downloadProjectExport('unknown')");
    assert.equal(links.length, 4, 'Unloaded projects must not start an export');
    assert.equal(s.requests.length, 0, 'Do not buffer full archive data in a JS fetch');
});

test('download links are removed even when activation fails', () => {
    const s = setup();
    let removed = false;
    s.document.body = {appendChild() {}};
    s.document.createElement = () => ({
        click() { throw new Error('activation failed'); },
        remove() { removed = true; },
    });
    s.bind();
    assert.throws(() => s.run('exportBackup()'), /activation failed/);
    assert.equal(removed, true);
});

test('model-backed projection actions carry the loaded revision and selected chapter', async () => {
    const s = setup();
    const revision = 'b'.repeat(64);
    s.bind('作品', revision);
    s.run("selectedChapterNum=2; var pollIds=[],tabs=[]; pollStatus=id=>pollIds.push(id); switchTab=tab=>tabs.push(tab)");
    const cases = [
        ['triggerGenerateShortDrama()', `/api/novel/${encodeURIComponent('作品')}/short_drama/2/generate`, 'multimodal', {expected_revision: revision}],
        ['triggerGenerateAudioPackage()', `/api/novel/${encodeURIComponent('作品')}/audio_package/generate`, 'multimodal', {expected_revision: revision}],
        ['triggerGenerateDelivery()', `/api/novel/${encodeURIComponent('作品')}/generate_delivery`, 'delivery', {expected_revision: revision}],
        ['triggerReaderSim()', '/api/novel/reader_simulate', null, {name:'作品',chapter:2,expected_revision:revision}],
        ['triggerBranchSim()', '/api/novel/branch_simulate', 'branching', {name:'作品',chapter:2,expected_revision:revision}],
    ];
    for (const [index, [action, url, tab, expectedBody]] of cases.entries()) {
        const pending = s.run(action);
        const request = s.requests.at(-1);
        assert.equal(request.url, url);
        assert.equal(request.options.method, 'POST');
        assert.deepEqual(JSON.parse(request.options.body), expectedBody);
        s.respond(request, {status:'started', task_id:`task-${index}`});
        await pending;
        if (tab) assert.equal(s.run('tabs.at(-1)'), tab);
    }
    assert.equal(s.run('JSON.stringify(pollIds)'), JSON.stringify(['task-0','task-1','task-2','task-3','task-4']));
});

test('the multimodal tab never presents another chapter short drama as current', () => {
    const s = setup();
    s.bind();
    s.run(`
        selectedChapterNum=2;
        novelData.short_dramas={1:{
            title:'第一章旧短剧',core_hook:'旧钩子',ending_cliffhanger:'旧悬念',
            scenes:[{scene_number:1,shot_type:'特写',visual_action:'旧画面',dialogue:'旧台词',sound_and_bgm:'旧声音'}]
        }};
        novelData.audio_package=null;
        productionShortDramaAndAudio();
    `);
    assert.match(s.el('short-drama-box').innerHTML, /本章暂无分镜剧本/);
    assert.doesNotMatch(s.el('short-drama-box').innerHTML, /第一章旧短剧/);
});

test('model configuration remains reachable from the palette with a book open', () => {
    const s = setup();
    s.bind();
    s.run("var configOpened=0; openConfigModal=()=>configOpened++; buildPaletteCommands().find(c=>c.label==='模型与 API 配置').run()");
    assert.equal(s.run('configOpened'), 1);
});

test('sidebar visibility and accessible expansion stay synchronized when selecting a book', async () => {
    const s = setup();
    s.run('toggleSidebar()');
    assert.equal(s.el('sidebar').classList.contains('open'), true);
    assert.equal(s.el('btn-side-toggle').getAttribute('aria-expanded'), 'true');
    const selected = s.run("selectNovel('a')");
    assert.equal(s.el('sidebar').classList.contains('open'), false);
    assert.equal(s.el('btn-side-toggle').getAttribute('aria-expanded'), 'false');
    s.respond(s.requests[0], detail('a'));
    await selected;
});

test('Escape closes an open sidebar and returns focus to its toggle', () => {
    const s = setup();
    s.run('setSidebarOpen(true)');
    let prevented = false;
    s.document.handlers.get('keydown')[0]({key:'Escape', preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(s.el('sidebar').classList.contains('open'), false);
    assert.equal(s.el('btn-side-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(s.document.activeElement, s.el('btn-side-toggle'));
});

test('a stop request targets only the task that showed the stop button', async () => {
    const s = setup();
    const stop = s.el('pipeline-stop');
    s.run('var polledTask = ""; pollStatus = id => { polledTask = id; }');
    stop._taskId = 'current-task';
    const pending = s.run('requestGenerationStop()');
    assert.equal(stop.disabled, true);
    assert.equal(s.requests[0].url, '/api/status/current-task/stop');
    assert.equal(s.requests[0].options.method, 'POST');
    stop._taskId = 'new-task';
    stop.textContent = '停止';
    s.respond(s.requests[0], {status:'stopping'});
    await pending;
    assert.equal(stop.textContent, '停止');
    assert.equal(s.run('polledTask'), '');
});

test('continuation discloses pending planning, model costs and the server acknowledgement', async () => {
    const s = setup();
    s.bind();
    s.run("novelData.concept={title:'<unsafe>'}; novelData.summary={current_chapter:0,planning_pending:true,planning_stage:'outline'}; openContinueModal()");
    assert.match(s.el('continue-info').innerHTML, /&lt;unsafe&gt;/);
    assert.match(s.el('continue-info').innerHTML, /先重试总纲再写正文/);
    assert.match(s.el('continue-info').innerHTML, /费用/);
    s.el('continue-count').value = '1';
    s.run('pollStatus=()=>{}');
    const request = s.run('startContinueGen()');
    assert.deepEqual(JSON.parse(s.requests[0].options.body), {
        name:'book', count:1, include_adaptations:false, expected_revision:'a'.repeat(64),
    });
    s.respond(s.requests[0], {message:'将先恢复未完成总纲'});
    await request;
    assert.equal(s.run('toasts.at(-1).message'), '将先恢复未完成总纲');
    s.run("novelData.concept=null; novelData.summary.planning_stage='<world>'; openContinueModal()");
    assert.match(s.el('continue-info').innerHTML, /未策划 · book/);
    assert.match(s.el('continue-info').innerHTML, /主策划尚未完成/);
    assert.match(s.el('continue-info').innerHTML, /&lt;world&gt;/);
    assert.match(s.el('continue-info').innerHTML, /不重复调用已提交阶段/);
    s.run("novelData.summary.planning_stage='promotion'; openContinueModal()");
    assert.match(s.el('continue-info').innerHTML, /不会重复调用主策划模型/);
    s.run('novelData.summary.planning_pending=false; openContinueModal()');
    assert.doesNotMatch(s.el('continue-info').innerHTML, /初始总纲尚未完成/);
});

test('a partial planner checkpoint opens as read-only and keeps recovery available', async () => {
    const s = setup({workspace:true});
    s.bind('partial');
    await s.run(`
        novelData = {
            novel_id:'partial', revision:'a'.repeat(64), concept:null, world_setting:null,
            chapters:[], characters:[], foreshadowings:[], branch_simulations:{},
            planning_checkpoint:{next_stage:'<world>'},
            summary:{current_chapter:0,total_words:0,planning_pending:true,planning_stage:'<world>'},
            timeline_status:{mode:'empty',complete:true}, state_reconciliation:{required:false},
            knowledge_graph:{}, cytoscape_data:{elements:[]}
        };
        renderNovelWorkspace()
    `);
    assert.equal(s.el('novel-title').innerText, '《主策划未完成 · partial》');
    assert.equal(s.el('novel-stats').innerText, '待恢复：<world>');
    assert.match(s.el('current-chapter-body').innerHTML, /&lt;world&gt;/);
    for (const id of ['btn-rollback','btn-export-txt','btn-export-md','btn-export-epub']) {
        assert.equal(s.el(id).disabled, true);
    }
    for (const id of ['btn-chapter-edit','btn-outline-edit','btn-regenerate','btn-reader-sim','btn-rail-toggle','btn-short-drama-generate']) {
        assert.equal(s.el(id).disabled, true);
        assert.equal(s.el(id).title, '请先完成策划并生成章节');
    }
    assert.equal(s.el('btn-audio-generate').disabled, true);
    assert.equal(s.el('btn-audio-generate').title, '请先完成策划案与人物卡');
    assert.match(html, /id="btn-regenerate"[^>]+aria-label="整章重新生成"/);
    assert.match(html, /id="btn-reader-sim"[^>]+aria-label="推演读者弹幕与留存"/);
    assert.match(html, /id="btn-short-drama-generate"[^>]+aria-label="重新生成当前章节短剧分镜"/);
    assert.match(html, /id="btn-audio-generate"[^>]+aria-label="重新生成有声演播配置"/);
    assert.equal(s.el('btn-export-zip').disabled, false);
    s.run(`
        novelData.concept={title:'已保存概念'};
        openConceptModal()
    `);
    assert.match(s.run('toasts.at(-1).message'), /只读检查点/);
});

test('the outline joins planned chapters and existing prose without inventing actions', () => {
    const s = setup();
    s.bind();
    s.run(`
        sortedChapters = [{chapter_number:1,title:'旧标题',volume_number:1},{chapter_number:2,title:'仅有正文',volume_number:2}];
        novelData.global_outline = {volumes:[{volume_number:1,chapters:[
            {chapter_number:1,title:'改稿计划'}, {chapter_number:3,title:'<未来章>'}
        ]}]};
    `);
    const render = filter => {
        s.el('outline-filter').value = filter;
        s.run('renderOutlineTab()');
        return s.el('outline-container').innerHTML;
    };
    const all = render('all');
    assert.equal((all.match(/class="outline-chapter"/g) || []).length, 3);
    assert.match(all, /openOutlineModal\(1\)/);
    assert.match(all, /readReferenceChapter\(1\)/);
    assert.match(all, /readReferenceChapter\(2\)/);
    assert.doesNotMatch(all, /openOutlineModal\(2\)|readReferenceChapter\(3\)/);
    assert.doesNotMatch(render('written'), /&lt;未来章&gt;/);
    const planned = render('planned');
    assert.match(planned, /&lt;未来章&gt;/);
    assert.doesNotMatch(planned, /仅有正文|改稿计划/);
    s.run('novelData.global_outline=null; novelData.imported_manuscript=true');
    assert.match(render('all'), /仅有正文/);
    assert.match(render('all'), /建立续写规划/);
});

test('hook filters distinguish unresolved records from payoffs and link only existing chapters', () => {
    const s = setup();
    s.bind();
    s.run(`
        sortedChapters=[{chapter_number:1}];
        novelData.foreshadowings=[
            {hook_id:'first',title:'起点',status:'DEVELOPING',planted_chapter:1,expected_resolution_volume:2},
            {hook_id:'later',title:'消失的信',status:'RESOLVED',planted_chapter:8,expected_resolution_volume:2,resolution_notes:'<原委>'}
        ];
    `);
    s.el('hooks-filter').value='open';
    s.run('renderForeshadowing()');
    assert.match(s.el('hooks-count').textContent, /1 条待回收 · 1 条已揭晓/);
    assert.match(s.el('foreshadowing-grid').innerHTML, /readReferenceChapter\(1\)/);
    assert.doesNotMatch(s.el('foreshadowing-grid').innerHTML, /消失的信/);
    s.el('hooks-filter').value='resolved';
    s.run('renderForeshadowing()');
    assert.match(s.el('foreshadowing-grid').innerHTML, /&lt;原委&gt;/);
    assert.doesNotMatch(s.el('foreshadowing-grid').innerHTML, /readReferenceChapter|起点/);
});

test('character search includes current location and keeps checkpoint portraits read only', () => {
    const s = setup();
    s.bind();
    s.run("novelData.characters=[{id:'a',name:'南薇',role:'校对员',current_state:{location:'天文台',level:'初始等级',hp_status:'健康良好',current_mood:'平静警惕'},background:'她能打开玻璃匣。灯塔来信。'},{id:'b',name:'泊岸',secrets_and_goals:['找回<旧信>'],current_state:{location:'新手村'}}]");
    s.el('characters-search').value = '天文台';
    s.run('productionCharacters()');
    const [front, details] = s.el('character-list').innerHTML.split('<details>');
    assert.match(front, /class="person-premise">她能打开玻璃匣。<\/p>/);
    assert.match(front, /天文台/);
    assert.doesNotMatch(front, /初始等级|健康良好|平静警惕|灯塔来信/);
    for (const value of ['初始等级','健康良好','平静警惕','灯塔来信']) assert.ok(details.includes(value));
    assert.match(s.el('character-list').innerHTML, /编辑角色：南薇/);
    assert.doesNotMatch(s.el('character-list').innerHTML, /泊岸/);
    s.run('novelData.planning_checkpoint=true; productionCharacters()');
    assert.doesNotMatch(s.el('character-list').innerHTML, /openCharacterModal/);
    s.el('characters-search').value = '不存在';
    s.run('productionCharacters()');
    assert.match(s.el('character-list').innerHTML, /清空搜索/);
    s.run('clearCharacterSearch()');
    assert.equal(s.el('characters-count').textContent, '2 人');
    const second = s.el('character-list').innerHTML.split('<article class="person-card">')[2];
    assert.match(second.split('<details>')[0], /找回&lt;旧信&gt;/);
    assert.doesNotMatch(second.split('<details>')[0], /新手村/);
    assert.match(second.split('<details>')[1], /新手村/);
});

"use strict";

let cyInstance = null;
let renderedGraph = null;

const graphEntityTypes = new Map([
    ['CHARACTER', ['character', '人物']], ['ITEM', ['item', '物品']],
    ['FACTION', ['faction', '势力']], ['LOCATION', ['location', '地点']],
    ['SECRET', ['secret', '秘辛']], ['EVENT', ['event', '事件']],
]);
const otherEntityType = ['other', '其他实体'];
function graphSymbol(model) {
    const folder = model === 'character' || model === 'scout' ? 'graph-robots' : 'graph-symbols';
    return `/assets/${folder}/${model}.png`;
}

function characterRobotImage(name) {
    return graphSymbol(String(name ?? '').codePointAt(0) % 2 ? 'scout' : 'character');
}

function entityImage(data, model) {
    if (data.type === 'CHARACTER') return characterRobotImage(data.label ?? data.id);
    if (data.type === 'ITEM') {
        const name = String(data.label || '').trim();
        if (/(?:钥匙|鑰匙|\bkey)$/iu.test(name)) return graphSymbol('key');
        if (/(?:星图|星圖|star chart)$/iu.test(name)) return graphSymbol('star-chart');
        if (/(?:玻璃[匣盒箱]|glass (?:case|box))$/iu.test(name)) return graphSymbol('glass-case');
    }
    return graphSymbol(model);
}

function scaleGraphAnnotations(cy) {
    // Allow dense overviews to shrink, then keep annotation sizes in screen pixels.
    const unit = 1 / Math.max(0.5, cy.zoom());
    cy.batch(() => {
        cy.nodes().style({'font-size':14 * unit, 'text-margin-y':5 * unit, 'text-background-padding':3 * unit});
        cy.edges().style({'font-size':12 * unit, 'arrow-scale':unit, width:1.65 * unit,
            'text-background-padding':2 * unit, 'source-distance-from-node':6 * unit, 'target-distance-from-node':6 * unit});
    });
    spaceRelationLabels(cy, unit);
}

function spaceRelationLabels(cy, unit) {
    const labelOnly = {includeEdges:false, includeNodes:false, includeOverlays:false, includeUnderlays:false};
    const boxes = new Map(cy.nodes().map(node => [node.id(), node.boundingBox()]));
    // On large graphs restrict collision checks to the two endpoints of each edge.
    const obstacles = cy.elements().length <= 100 ? [...boxes.values()] : null;
    const adjustments = [];
    for (const edge of cy.edges()) {
        if (!edge.data('label')) continue;
        const box = edge.boundingBox(labelOnly);
        const x = box.x1 - parseFloat(edge.style('text-margin-x'));
        const y = box.y1 - parseFloat(edge.style('text-margin-y'));
        const source = edge.source(), target = edge.target();
        const dx = target.position('x') - source.position('x'), dy = target.position('y') - source.position('y');
        const length = Math.hypot(dx, dy);
        const nx = length ? -dy / length : 0, ny = length ? dx / length : -1;
        const nearby = obstacles || [boxes.get(source.id()), boxes.get(target.id())];
        let bestScore = Infinity, best;
        for (const offset of [0, 6, -6, 12, -12, 18, -18, 24, -24, 32, -32, 42, -42, 54, -54]) {
            const mx = nx * offset * unit, my = ny * offset * unit;
            const rect = {x1:x+mx, y1:y+my, x2:x+mx+box.w, y2:y+my+box.h};
            let score = 0;
            for (const other of nearby) {
                const w = Math.min(rect.x2, other.x2) - Math.max(rect.x1, other.x1) + 2 * unit;
                const h = Math.min(rect.y2, other.y2) - Math.max(rect.y1, other.y1) + 2 * unit;
                if (w > 0 && h > 0) score += w * h;
            }
            if (score < bestScore) { bestScore = score; best = {edge, mx, my, rect}; }
            if (score === 0) break;
        }
        adjustments.push(best);
        obstacles?.push(best.rect);
    }
    cy.batch(() => {
        for (const {edge, mx, my} of adjustments) edge.style({'text-margin-x':mx, 'text-margin-y':my});
    });
}

// Give each branch of a small tree a sector proportional to its leaf count.
function treeGraphPositions(cy) {
    const nodes = cy.nodes();
    if (nodes.length < 3 || nodes.length > 24 || cy.edges().length !== nodes.length - 1 ||
        cy.elements().components().length !== 1) return null;
    const root = nodes.max(node => 2 * node.degree() + Number(node.data('type') === 'CHARACTER')).ele;
    const children = new Map();
    const leaves = new Map();
    function countLeaves(node, parent) {
        const branches = node.neighborhood('node').filter(next => next.id() !== parent);
        children.set(node.id(), branches);
        const count = branches.reduce((total, next) => total + countLeaves(next, node.id()), 0) || 1;
        leaves.set(node.id(), count);
        return count;
    }
    const leafCount = countLeaves(root);
    const radius = Math.max(126, leafCount * 17);
    const stretch = Math.min(1.5, Math.max(1, cy.width() / cy.height()));
    const positions = new Map();
    function place(node, depth, start, sweep) {
        const angle = start - sweep / 2;
        positions.set(node.id(), {x:depth * radius * stretch * Math.cos(angle), y:depth * radius * Math.sin(angle)});
        for (const child of children.get(node.id())) {
            const portion = sweep * leaves.get(child.id()) / leaves.get(node.id());
            place(child, depth + 1, start, portion);
            start -= portion;
        }
    }
    place(root, 0, 1.5 * Math.PI, 2 * Math.PI);
    return node => positions.get(node.id());
}

function closeEntityGraph() {
    cyInstance?.destroy();
    cyInstance = null;
    renderedGraph = null;
}

function zoomGraph(factor) {
    if (!cyInstance) return;
    const point = cyInstance.nodes(':selected').first().renderedPosition();
    const width = cyInstance.width(), height = cyInstance.height();
    const visible = point && point.x >= 0 && point.x <= width && point.y >= 0 && point.y <= height;
    cyInstance.zoom({level:cyInstance.zoom() * factor, renderedPosition:visible ? point : {x:width/2, y:height/2}});
}

function fitEntityGraph(cy) {
    if (cy.width() <= 1 || cy.height() <= 1) return;
    const nodes = cy.nodes();
    const bounds = nodes.boundingBox();
    if (nodes.length > 1 && (bounds.w > bounds.h) !== (cy.width() > cy.height())) {
        nodes.positions(node => ({x:-node.position('y'), y:node.position('x')}));
    }
    cy.fit(cy.elements(), 12);
    if (cy.zoom() > 1.15) {
        cy.zoom(1.15);
        cy.center();
    }
}

function inspectGraphEntity(id) {
    const select = document.getElementById('graph-entity');
    const detail = document.getElementById('graph-relations');
    const node = id && cyInstance?.getElementById(id);
    select.value = node?.length ? id : '';
    cyInstance?.batch(() => {
        cyInstance.elements(':selected').unselect();
        cyInstance.elements('.muted').removeClass('muted');
        cyInstance.edges('.inspected').removeClass('inspected');
        if (!node?.length) return;
        node.select();
        node.connectedEdges().addClass('inspected');
        cyInstance.elements().difference(node.closedNeighborhood()).addClass('muted');
    });
    if (!node?.length) { detail.textContent = ''; return; }
    const edges = node.connectedEdges();
    const links = edges.map(edge => `<li><b>${escapeHtml(edge.source().data('label'))}</b> → ${escapeHtml(edge.data('label'))} → <b>${escapeHtml(edge.target().data('label'))}</b></li>`);
    const name = node.data('label');
    const character = node.data('type') === 'CHARACTER' && novelData.characters?.find(ch => ch.name === name);
    const hook = node.data('type') === 'SECRET' && novelData.foreshadowings?.find(item => item.title === name);
    detail.innerHTML = links.length ? `<ul>${links.join('')}</ul>` : '<p>还没有与这个实体相连的关系。</p>';
    if (character || hook) {
        const target = character ? {tab:'characters', key:'character:' + character.id}
            : {tab:'foreshadowing', key:'foreshadowing:' + hook.hook_id};
        const button = document.createElement('button');
        button.className = 'btn btn-outline btn-sm';
        button.textContent = character ? '查看人物卡' : '查看伏笔记录';
        button.onclick = () => locateReference(target);
        detail.prepend(button);
    }
}

function renderCytoscapeGraph(forceLayout = false) {
    const dark = document.documentElement.classList.contains('dark');
    const data = novelData?.cytoscape_data;
    const book = novelData?.novel_id;
    const freshBook = renderedGraph?.book !== book;
    if (!forceLayout && cyInstance && !freshBook && renderedGraph.data === data && renderedGraph.dark === dark) return;
    renderedGraph = {book, data, dark};
    const elements = data?.elements || [];
    const legend = new Set();
    let nodes = 0;
    let firstName = '';
    const graph = elements.map(element => {
        const data = element.data;
        if (data.source !== undefined) return element;
        if (nodes === 0) firstName = data.label;
        nodes++;
        const role = graphEntityTypes.get(data.type) || otherEntityType;
        legend.add(role);
        const symbolImage = entityImage(data, role[0]);
        const state = data.status === 'DEAD' ? '已死亡' : data.status === 'DESTROYED' ? '已毁灭' : data.status === 'RESOLVED' ? '已解决' : '';
        const label = state ? `${data.label} · ${state}` : data.label;
        return {...element, data:{...data, symbolImage, displayLabel:label}};
    });
    const edges = elements.length - nodes;
    const canvas = document.getElementById('cy');
    canvas.classList.toggle('hidden', !nodes);
    canvas.classList.toggle('small-graph', nodes < 4);
    document.getElementById('graph-layout').disabled = nodes < 2;
    document.getElementById('graph-scale').classList.toggle('hidden', nodes < 2);
    document.getElementById('graph-summary').textContent = !nodes ? '还没有记录实体或关系。'
        : nodes === 1 && !edges ? `目前只有「${firstName}」，尚未记录关系。` : `${nodes} 个实体 · ${edges} 条关系`;
    document.getElementById('graph-legend').innerHTML = [...graphEntityTypes.values(), otherEntityType]
        .filter(role => legend.has(role))
        .map(([model, label]) => `<li><img src="${graphSymbol(model)}" width="23" height="23" alt="">${label}</li>`).join('');
    const picker = document.getElementById('graph-entity');
    const previous = picker.value;
    picker.innerHTML = '<option value="">选择实体</option>' + graph.filter(el => el.data.source === undefined).map(({data}) =>
        `<option value="${escapeHtml(data.id)}">${escapeHtml(data.displayLabel)}（${(graphEntityTypes.get(data.type) || otherEntityType)[1]}）</option>`).join('');
    document.getElementById('graph-inspector').classList.toggle('hidden', !nodes);
    document.getElementById('graph-relations').replaceChildren();
    if (cyInstance) { cyInstance.destroy(); cyInstance = null; }
    if (!nodes) return;
    const labelColor = dark ? '#f2ecdc' : '#283731';
    const edgeColor = dark ? '#7e938c' : '#93a59b';
    const paper = dark ? '#293028' : '#f8f5ee';
    cyInstance = cytoscape({
        container: canvas,
        maxZoom: 4,
        elements: graph,
        style: [
            { selector: 'node', style: {
                shape:'rectangle', width:52, height:52, 'background-opacity':0,
                'background-image':'data(symbolImage)', 'background-fit':'contain',
                label:'data(displayLabel)', color:labelColor, 'font-family':'sans-serif', 'font-size':14, 'font-weight':600,
                'text-valign':'bottom', 'text-margin-y':6, 'text-events':'yes', 'text-background-color':paper,
                'text-background-opacity':0.95, 'text-background-padding':2,
            }},
            { selector: 'node[status = "DEAD"], node[status = "DESTROYED"]', style: {
                'background-image-opacity':0.65,
            }},
            { selector: 'node:selected, node:active', style: {
                'overlay-opacity':0, color:dark ? '#e2f1e5' : '#214c39',
                'text-background-color':dark ? '#3d5849' : '#d9e7db', 'text-background-opacity':1,
                'text-background-shape':'roundrectangle', 'text-background-padding':3,
            }},
            { selector: 'edge', style: {
                width:1.6, 'line-color':edgeColor, 'target-arrow-color':edgeColor, 'target-arrow-shape':'triangle',
                'curve-style':'bezier', 'source-endpoint':'outside-to-node-or-label', 'target-endpoint':'outside-to-node-or-label',
                label:'data(label)', 'font-family':'sans-serif', 'font-size':12,
                color:dark ? '#cad4ca' : '#536960', 'text-background-color':paper,
                'text-background-opacity':1, 'text-background-padding':2,
            }},
            { selector:'edge.inspected', style:{'line-color':dark ? '#a7c4af' : '#47775f',
                'target-arrow-color':dark ? '#a7c4af' : '#47775f'} },
            { selector:'node.muted', style:{opacity:0.55} },
            { selector:'edge.muted', style:{opacity:0.48} },
        ],
        layout: {name:'preset', fit:false},
    });
    let scaleFrame = 0;
    cyInstance.on('zoom resize layoutstop dragfree select unselect', ({cy}) => {
        if (!cy.container()) return;
        if (scaleFrame) return;
        scaleFrame = requestAnimationFrame(() => {
            scaleFrame = 0;
            if (!cy.destroyed()) scaleGraphAnnotations(cy);
        });
    });
    const positions = treeGraphPositions(cyInstance);
    cyInstance.layout({
        name: (positions || nodes === 1) ? 'preset' : 'cose', positions, fit:false, animate:false,
        nodeDimensionsIncludeLabels:true, nodeRepulsion:3400, idealEdgeLength:85,
        stop: event => fitEntityGraph(event.cy),
    }).run();
    let shape = [canvas.clientWidth, canvas.clientHeight];
    cyInstance.on('resize', event => {
        const {clientWidth:w, clientHeight:h} = canvas;
        if (!w || !h || (shape[0] === w && shape[1] === h)) return;
        shape = [w,h];
        fitEntityGraph(event.cy);
    });
    cyInstance.on('tap', 'node', event => inspectGraphEntity(event.target.id()));
    cyInstance.on('tap', event => { if (event.target === event.cy) inspectGraphEntity(''); });
    const first = graph.find(el => el.data.type === 'CHARACTER') || graph.find(el => el.data.source === undefined);
    inspectGraphEntity(freshBook ? first.data.id : previous);
}

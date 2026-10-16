/* sheet_grid.js — Google Sheets-подібна сітка для Chatwave/Spiklio.
   SheetGrid.mount(el, {tid, snap:{columns,rows,cells,styles}, api, toast})
   Значення в БД зберігаються "сирими" (формула = рядок, що починається з "="),
   обчислення робиться на клієнті. */
(function () {
'use strict';
var SG = window.SheetGrid = {};

// ---------- адреси ----------
function L(i) { var s = ''; i++; while (i) { var m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); } return s; }
function LI(s) { var n = 0; for (var i = 0; i < s.length; i++) n = n * 26 + s.charCodeAt(i) - 64; return n - 1; }
function P(a) { var m = /^\$?([A-Z]+)\$?(\d+)$/.exec(a); return m ? [+m[2] - 1, LI(m[1])] : null; }
function A(r, c) { return L(c) + (r + 1); }
function shiftF(f, dr, dc) {
    return f.replace(/(\$?)([A-Z]{1,3})(\$?)(\d+)/g, function (_, d1, col, d2, row) {
        var c = d1 ? LI(col) : LI(col) + dc, r = d2 ? +row - 1 : +row - 1 + dr;
        return (c < 0 || r < 0) ? '#REF!' : d1 + L(c) + d2 + (r + 1);
    });
}

// ---------- формули ----------
function E(e) { this.e = e; }
function num(x) {
    if (x instanceof E) throw x;
    if (x === '' || x == null) return 0;
    if (typeof x === 'boolean') return x ? 1 : 0;
    if (typeof x === 'number') return x;
    var n = Number(String(x).trim().replace(',', '.'));
    if (isNaN(n)) throw new E('#VALUE!');
    return n;
}
function str(x) { if (x instanceof E) throw x; return x === true ? 'TRUE' : x === false ? 'FALSE' : x == null ? '' : String(x); }
function nums(args) {
    var o = [];
    args.forEach(function (a) {
        if (Array.isArray(a)) a.forEach(function (row) { row.forEach(function (v) { if (v instanceof E) throw v; if (typeof v === 'number') o.push(v); }); });
        else o.push(num(a));
    });
    return o;
}
function flat(a) { return Array.isArray(a) ? [].concat.apply([], a) : [a]; }
function crit(c) {
    var m = /^(<=|>=|<>|<|>|=)?(.*)$/.exec(String(c)), op = m[1] || '=', v = m[2], n = Number(v);
    return function (x) {
        var isN = v !== '' && !isNaN(n), xv = isN ? (typeof x === 'number' ? x : (x === '' ? NaN : Number(x))) : String(x).toLowerCase(), cv = isN ? n : v.toLowerCase();
        switch (op) { case '=': return xv === cv; case '<>': return xv !== cv; case '<': return xv < cv; case '>': return xv > cv; case '<=': return xv <= cv; default: return xv >= cv; }
    };
}
var FN = {
    SUM: function (a) { return nums(a).reduce(function (x, y) { return x + y; }, 0); },
    AVERAGE: function (a) { var n = nums(a); if (!n.length) throw new E('#DIV/0!'); return n.reduce(function (x, y) { return x + y; }, 0) / n.length; },
    MIN: function (a) { var n = nums(a); return n.length ? Math.min.apply(null, n) : 0; },
    MAX: function (a) { var n = nums(a); return n.length ? Math.max.apply(null, n) : 0; },
    MEDIAN: function (a) { var n = nums(a).sort(function (x, y) { return x - y; }), m = n.length >> 1; if (!n.length) throw new E('#NUM!'); return n.length % 2 ? n[m] : (n[m - 1] + n[m]) / 2; },
    COUNT: function (a) { return nums(a).length; },
    COUNTA: function (a) { return flat(a.length > 1 ? a : a[0]).length && [].concat.apply([], a.map(flat)).filter(function (v) { return v !== '' && v != null; }).length; },
    ROUND: function (a) { var p = Math.pow(10, a[1] == null ? 0 : num(a[1])); return Math.round(num(a[0]) * p) / p; },
    ABS: function (a) { return Math.abs(num(a[0])); },
    SQRT: function (a) { var n = num(a[0]); if (n < 0) throw new E('#NUM!'); return Math.sqrt(n); },
    POWER: function (a) { return Math.pow(num(a[0]), num(a[1])); },
    MOD: function (a) { var d = num(a[1]); if (!d) throw new E('#DIV/0!'); return num(a[0]) - d * Math.floor(num(a[0]) / d); },
    AND: function (a) { return [].concat.apply([], a.map(flat)).every(function (v) { return !!num(v); }); },
    OR: function (a) { return [].concat.apply([], a.map(flat)).some(function (v) { return !!num(v); }); },
    NOT: function (a) { return !num(a[0]); },
    LEN: function (a) { return str(a[0]).length; },
    LEFT: function (a) { return str(a[0]).slice(0, a[1] == null ? 1 : num(a[1])); },
    RIGHT: function (a) { var s = str(a[0]), n = a[1] == null ? 1 : num(a[1]); return n ? s.slice(-n) : ''; },
    MID: function (a) { return str(a[0]).substr(num(a[1]) - 1, num(a[2])); },
    UPPER: function (a) { return str(a[0]).toUpperCase(); },
    LOWER: function (a) { return str(a[0]).toLowerCase(); },
    TRIM: function (a) { return str(a[0]).trim().replace(/\s+/g, ' '); },
    CONCATENATE: function (a) { return a.map(function (x) { return flat(x).map(str).join(''); }).join(''); },
    SUMIF: function (a) { var f = crit(a[1]), r = flat(a[0]), s = flat(a[2] || a[0]), t = 0; r.forEach(function (v, i) { if (f(v) && typeof s[i] === 'number') t += s[i]; }); return t; },
    COUNTIF: function (a) { var f = crit(a[1]); return flat(a[0]).filter(f).length; },
    VLOOKUP: function (a) {
        var k = a[0], t = a[1], ci = num(a[2]) - 1, exact = a[3] !== undefined && !num(a[3]);
        for (var i = 0; i < t.length; i++) if (String(t[i][0]).toLowerCase() === String(k).toLowerCase()) return t[i][ci];
        throw new E('#N/A');
    },
    TODAY: function () { return new Date().toLocaleDateString('en-CA'); },
    NOW: function () { return new Date().toLocaleString('sv'); }
};
FN.CONCAT = FN.CONCATENATE;

function tok(s) {
    var re = /\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)|"((?:[^"]|"")*)"|(\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)|([A-Za-z_][A-Za-z0-9_.]*)|(<=|>=|<>|[-+*\/^&=<>(),%]))/y, out = [], m, last = 0;
    s = s.split('"').map(function (seg, k) { return k % 2 ? seg : seg.toUpperCase(); }).join('"');
    re.lastIndex = 0;
    while (re.lastIndex < s.length && (m = re.exec(s))) {
        if (m[1] != null) out.push({ k: 'n', v: parseFloat(m[1]) });
        else if (m[2] != null) out.push({ k: 's', v: m[2].replace(/""/g, '"') });
        else if (m[3]) out.push({ k: 'r', v: m[3] });
        else if (m[4]) out.push({ k: 'i', v: m[4].toUpperCase() });
        else if (m[5]) out.push({ k: 'o', v: m[5] });
        else break;
        last = re.lastIndex;
    }
    if (s.slice(last).trim()) throw new E('#ERROR!');
    return out;
}

function compile(src, get) {
    var t = tok(src), i = 0;
    function pk() { return t[i]; }
    function isO(v) { var p = t[i]; return p && p.k === 'o' && (Array.isArray(v) ? v.indexOf(p.v) >= 0 : p.v === v); }
    function eat(v) { if (!isO(v)) throw new E('#ERROR!'); i++; }
    function bin(o, l, r) {
        return function () {
            var a = l(), b = r();
            if (a instanceof E) throw a; if (b instanceof E) throw b;
            switch (o) {
                case '+': return num(a) + num(b); case '-': return num(a) - num(b); case '*': return num(a) * num(b);
                case '/': var d = num(b); if (!d) throw new E('#DIV/0!'); return num(a) / d;
                case '^': return Math.pow(num(a), num(b)); case '&': return str(a) + str(b);
            }
            var x = a, y = b;
            if (typeof x === 'string' && typeof y === 'string') { x = x.toLowerCase(); y = y.toLowerCase(); }
            else if (typeof x !== typeof y) { x = String(x); y = String(y); }
            switch (o) { case '=': return x === y; case '<>': return x !== y; case '<': return x < y; case '>': return x > y; case '<=': return x <= y; case '>=': return x >= y; }
        };
    }
    function cmp() { var l = cat(); while (isO(['=', '<>', '<=', '>=', '<', '>'])) { var o = t[i++].v; l = bin(o, l, cat()); } return l; }
    function cat() { var l = add(); while (isO('&')) { i++; l = bin('&', l, add()); } return l; }
    function add() { var l = mul(); while (isO(['+', '-'])) { var o = t[i++].v; l = bin(o, l, mul()); } return l; }
    function mul() { var l = pow(); while (isO(['*', '/'])) { var o = t[i++].v; l = bin(o, l, pow()); } return l; }
    function pow() { var l = un(); while (isO('^')) { i++; l = bin('^', l, un()); } return l; }
    function un() {
        if (isO('-')) { i++; var f = un(); return function () { return -num(f()); }; }
        if (isO('+')) { i++; return un(); }
        var p = prim();
        while (isO('%')) { i++; (function (q) { p = function () { return num(q()) / 100; }; })(p); }
        return p;
    }
    function prim() {
        var x = t[i++]; if (!x) throw new E('#ERROR!');
        if (x.k === 'n') return function () { return x.v; };
        if (x.k === 's') return function () { return x.v; };
        if (x.k === 'r') {
            var parts = x.v.split(':');
            if (parts.length === 1) return function () { var v = get(parts[0]); if (v instanceof E) throw v; return v; };
            return function () {
                var a = P(parts[0]), b = P(parts[1]), o = [];
                for (var r = Math.min(a[0], b[0]); r <= Math.max(a[0], b[0]); r++) { var row = []; for (var c = Math.min(a[1], b[1]); c <= Math.max(a[1], b[1]); c++) row.push(get(A(r, c))); o.push(row); }
                return o;
            };
        }
        if (x.k === 'o' && x.v === '(') { var e = cmp(); eat(')'); return e; }
        if (x.k === 'i') {
            if (x.v === 'TRUE') return function () { return true; };
            if (x.v === 'FALSE') return function () { return false; };
            eat('(');
            var args = [];
            if (!isO(')')) { args.push(cmp()); while (isO(',')) { i++; args.push(cmp()); } }
            eat(')');
            var name = x.v;
            if (name === 'IF') return function () { var c = num(args[0]()); return c ? (args[1] ? args[1]() : true) : (args[2] ? args[2]() : false); };
            if (name === 'IFERROR') return function () { try { var v = args[0](); if (v instanceof E) throw v; return v; } catch (e) { if (e instanceof E) return args[1] ? args[1]() : ''; throw e; } };
            if (!FN[name]) return function () { throw new E('#NAME?'); };
            return function () { return FN[name](args.map(function (f) { return f(); })); };
        }
        throw new E('#ERROR!');
    }
    var f = cmp();
    if (i < t.length) throw new E('#ERROR!');
    return f;
}

// ---------- відображення значень ----------
function fmtNum(v, nf) {
    if (nf === 'num') return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (nf === 'pct') return (v * 100).toFixed(2) + '%';
    if (nf === 'cur') return '₴' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return String(+v.toPrecision(12));
}

var CSS = '.sg{display:flex;flex-direction:column;height:100%;min-height:360px;background:#fff;border:1px solid var(--cw-line,#dfe3f5);border-radius:12px;overflow:hidden;font:13px Inter,sans-serif}' +
'.sg-tb{display:flex;gap:2px;align-items:center;padding:6px 8px;border-bottom:1px solid var(--cw-line,#dfe3f5);flex-wrap:wrap;background:#f9fbfd}' +
'.sg-tb button,.sg-tb select{background:none;border:0;border-radius:6px;padding:4px 8px;min-width:28px;height:28px;cursor:pointer;color:#14172b;font:inherit;width:auto;margin:0}' +
'.sg-tb button:hover,.sg-tb select:hover{background:#eef0ff}.sg-tb button.on{background:#dfe6ff}.sg-tb input[type=color]{width:26px;height:26px;padding:0;border:0;background:none;cursor:pointer;margin:0}' +
'.sg-sep{width:1px;height:20px;background:#dfe3f5;margin:0 4px}' +
'.sg-fb{display:flex;align-items:center;border-bottom:1px solid var(--cw-line,#dfe3f5)}' +
'.sg-fb input{border:0!important;border-radius:0!important;margin:0!important;padding:6px 10px!important;box-shadow:none!important;background:#fff!important;font:13px Inter,sans-serif!important}' +
'.sg-nb{width:90px!important;border-right:1px solid #dfe3f5!important;text-align:center}.sg-fx{flex:1;width:auto!important}.sg-fxl{color:#8a90a8;padding:0 6px;font-style:italic}' +
'.sg-sc{flex:1;overflow:auto;position:relative;outline:0;touch-action:pan-x pan-y;-webkit-touch-callout:none;overscroll-behavior:contain}' +
'.sg table{border-collapse:separate;border-spacing:0;table-layout:fixed;width:max-content}' +
'.sg td,.sg th{border-right:1px solid #e2e3e3;border-bottom:1px solid #e2e3e3;padding:0 4px;overflow:hidden;white-space:nowrap;text-overflow:clip;box-sizing:border-box;background:#fff;line-height:16px}' +
'.sg th{height:25px;text-transform:none;letter-spacing:0;background:#f8f9fa;color:#5f6368;font-weight:400;font-size:11px;text-align:center;position:sticky;top:0;z-index:3;user-select:none;position:sticky}' +
'.sg th.sg-rh{left:0;z-index:4}.sg td.sg-rh{background:#f8f9fa;color:#5f6368;font-size:11px;text-align:center;position:sticky;left:0;z-index:2;user-select:none}' +
'.sg th.hl,.sg td.sg-rh.hl{background:#e8eaed;color:#202124}' +
'.sg td.sel{background:#e8f0fe}.sg td.act{outline:2px solid #1a73e8;outline-offset:-2px;background:#fff;z-index:1;position:relative}' +
'.sg td.err{color:#d64545}.sg td.cut{outline:2px dashed #1a73e8;outline-offset:-2px}' +
'.sg td input{width:100%;height:100%;border:0;outline:2px solid #1a73e8;outline-offset:-2px;padding:0 4px;margin:0;font:inherit;background:#fff;border-radius:0;box-shadow:none}' +
'.sg-rs{position:absolute;top:0;right:-3px;width:7px;height:100%;cursor:col-resize;z-index:5;touch-action:none}.sg-rs:hover,.sg-rs.on{background:#1a73e8}' +
'.sg-rr{position:absolute;left:0;bottom:-3px;width:100%;height:7px;cursor:row-resize;z-index:5;touch-action:none}.sg-rr:hover,.sg-rr.on{background:#1a73e8}' +
'@media (pointer:coarse){.sg-rs{width:24px;right:-12px}.sg-rr{height:24px;bottom:-12px}.sg-rs.on,.sg-rr.on{background:rgba(26,115,232,.35)}}' +
'.sg-th{position:relative}';

SG.mount = function (el, o) {
    if (!document.getElementById('sg-css')) { var st = document.createElement('style'); st.id = 'sg-css'; st.textContent = CSS; document.head.appendChild(st); }
    var tid = o.tid, api = o.api, toast = o.toast || function () {};
    var S = o.snap, cells = S.cells || {}, styles = S.styles || {};
    var BASE_ROWS = 1000, BASE_COLS = 26, MAXR = 20000, MAXC = 702, HEAD = 25;
    var colW = [], rowH = {}, nRows = BASE_ROWS, nCols = BASE_COLS, rowTop = [], winA = -1, winB = -1, raf = 0;
    (S.columns || []).forEach(function (c) { colW[c.col_index] = (c.width && c.width !== 140) ? c.width : 100; });
    (S.rows || []).forEach(function (r) { var n = parseInt(r.row_id, 10); if (r.height && r.height !== 32) rowH[n - 1] = r.height; });
    var anchor = [0, 0], focus = [0, 0], editing = null, clip = null, undo = [], redo = [], cache = null;

    el.innerHTML = '<div class="sg"><div class="sg-tb"></div><div class="sg-fb"><input class="sg-nb" value="A1"><span class="sg-fxl">fx</span><input class="sg-fx"></div><div class="sg-sc" tabindex="0"></div></div>';
    var tb = el.querySelector('.sg-tb'), nb = el.querySelector('.sg-nb'), fx = el.querySelector('.sg-fx'), sc = el.querySelector('.sg-sc');

    // ---- обчислення ----
    function raw(a) { var v = cells[a]; return v == null ? '' : v; }
    function get(a) {
        if (cache.v.hasOwnProperty(a)) { var c = cache.v[a]; return c; }
        var r = raw(a), v;
        if (r === '') v = '';
        else if (r[0] === '=') {
            if (cache.stack[a]) { v = new E('#REF!'); }
            else {
                cache.stack[a] = 1;
                try { v = compile(r.slice(1), get)(); } catch (e) { v = e instanceof E ? e : new E('#ERROR!'); }
                delete cache.stack[a];
                if (Array.isArray(v)) v = v[0] && v[0][0] !== undefined ? v[0][0] : '';
            }
        } else if (r[0] === "'") v = r.slice(1);
        else if (/^-?(0|[1-9]\d*)(\.\d+)?(e[-+]?\d+)?$/i.test(r.trim())) v = parseFloat(r);
        else v = r;
        cache.v[a] = v; return v;
    }
    function show(a) {
        var v = get(a), st = styles[a] || {};
        if (v instanceof E) return v.e;
        if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
        if (typeof v === 'number') return fmtNum(v, st.nf);
        return v;
    }

    // ---- зміни (undo/redo + збереження) ----
    function commit(ch, noHist) {           // ch: {a1: {v?:string, s?:object}}
        var before = {}, cv = {}, cs = {};
        Object.keys(ch).forEach(function (a) {
            before[a] = { v: raw(a), s: styles[a] || null };
            if ('v' in ch[a]) { if (ch[a].v === '') delete cells[a]; else cells[a] = ch[a].v; cv[a] = ch[a].v; }
            if ('s' in ch[a]) { var s = ch[a].s; if (!s || !Object.keys(s).length) { delete styles[a]; cs[a] = null; } else { styles[a] = s; cs[a] = s; } }
        });
        if (!noHist) { undo.push({ before: before, after: ch }); redo = []; if (undo.length > 100) undo.shift(); }
        var keys = Object.keys(ch);
        for (var i = 0; i < keys.length; i += 3000) {
            var pc = {}, ps = {};
            keys.slice(i, i + 3000).forEach(function (a) { if (a in cv) pc[a] = cv[a]; if (a in cs) ps[a] = cs[a]; });
            api('/uadmin/table/api/cells/batch', { table_id: tid, cells: pc, styles: ps }).then(function (r) { if (!r.ok) toast(r.error || 'Failed to save'); }).catch(function () { toast('Server error'); });
        }
        draw();
    }
    function doUndo() {
        var h = undo.pop(); if (!h) return; redo.push(h);
        var ch = {}; Object.keys(h.before).forEach(function (a) { ch[a] = { v: h.before[a].v, s: h.before[a].s }; });
        commit(ch, true);
    }
    function doRedo() {
        var h = redo.pop(); if (!h) return; undo.push(h);
        commit(h.after, true);
    }

    // ---- виділення ----
    function rect() { return [Math.min(anchor[0], focus[0]), Math.min(anchor[1], focus[1]), Math.max(anchor[0], focus[0]), Math.max(anchor[1], focus[1])]; }
    function usedExtent() {
        var mr = 0, mc = 0;
        [cells, styles].forEach(function (m) { for (var a in m) { var p = P(a); if (p) { if (p[0] + 1 > mr) mr = p[0] + 1; if (p[1] + 1 > mc) mc = p[1] + 1; } } });
        return [mr, mc];
    }
    function each(fn) {
        var q = rect();
        if ((q[2] - q[0] + 1) * (q[3] - q[1] + 1) > 3000) { var ex = usedExtent(); q = [q[0], q[1], Math.min(q[2], Math.max(ex[0] - 1, q[0])), Math.min(q[3], Math.max(ex[1] - 1, q[1]))]; }
        for (var r = q[0]; r <= q[2]; r++) for (var c = q[1]; c <= q[3]; c++) fn(A(r, c), r, c);
    }
    function rh(r) { return rowH[r] || 25; }
    function setSel(r, c, ext) {
        r = Math.max(0, Math.min(MAXR - 1, r)); c = Math.max(0, Math.min(MAXC - 1, c));
        focus = [r, c]; if (!ext) anchor = [r, c];
        if (r >= nRows || c >= nCols) render(true);
        ensureVisible(r, c); render(false); paintSel();
    }
    function ensureVisible(r, c) {
        var t = rowTop[r] || 0, h = rh(r);
        if (t < sc.scrollTop) sc.scrollTop = t;
        else if (HEAD + t + h > sc.scrollTop + sc.clientHeight) sc.scrollTop = HEAD + t + h - sc.clientHeight;
        var l = 46, i; for (i = 0; i < c; i++) l += colW[i] || 100;
        var w = colW[c] || 100;
        if (l - 46 < sc.scrollLeft) sc.scrollLeft = l - 46;
        else if (l + w > sc.scrollLeft + sc.clientWidth) sc.scrollLeft = l + w - sc.clientWidth;
    }
    function tdAt(r, c) { return sc.querySelector('td[data-a="' + A(r, c) + '"]'); }
    function paintSel() {
        var q = rect();
        sc.querySelectorAll('.sel,.act,.hl').forEach(function (n) { n.classList.remove('sel', 'act', 'hl'); });
        sc.querySelectorAll('td[data-a]').forEach(function (td) {
            var r = +td.dataset.r, c = +td.dataset.c;
            if (r >= q[0] && r <= q[2] && c >= q[1] && c <= q[3]) td.classList.add(r === focus[0] && c === focus[1] ? 'act' : 'sel');
        });
        sc.querySelectorAll('th[data-c]').forEach(function (h) { var c = +h.dataset.c; if (c >= q[1] && c <= q[3]) h.classList.add('hl'); });
        sc.querySelectorAll('td[data-r].sg-rh').forEach(function (h) { var r = +h.dataset.r; if (r >= q[0] && r <= q[2]) h.classList.add('hl'); });
        var a = A(focus[0], focus[1]); nb.value = (q[0] === q[2] && q[1] === q[3]) ? a : A(q[0], q[1]) + ':' + A(q[2], q[3]);
        if (!editing) fx.value = raw(a);
        syncTb();
    }

    // ---- малювання (віртуалізація рядків, автопродовження/стискання) ----
    function calcTops() {
        rowTop = new Array(nRows + 1); var t = 0;
        for (var r = 0; r < nRows; r++) { rowTop[r] = t; t += rh(r); }
        rowTop[nRows] = t;
    }
    function rowAt(y) {
        var lo = 0, hi = nRows - 1;
        while (lo < hi) { var m = (lo + hi + 1) >> 1; if (rowTop[m] <= y) lo = m; else hi = m - 1; }
        return lo;
    }
    function fit() {                       // скільки рядків/колонок показувати
        var ex = usedExtent(), y = sc.scrollTop + sc.clientHeight, lastR;
        lastR = !rowTop.length ? Math.ceil((y - HEAD) / 25) : (y - HEAD < rowTop[nRows] ? rowAt(Math.max(0, y - HEAD)) : nRows + Math.ceil((y - HEAD - rowTop[nRows]) / 25));
        var lastC = 0, x = 46, xr = sc.scrollLeft + sc.clientWidth;
        while (lastC < MAXC && x < xr) { x += colW[lastC] || 100; lastC++; }
        var nr = Math.min(MAXR, Math.max(BASE_ROWS, ex[0], focus[0] + 1, anchor[0] + 1, lastR + 61));
        var nc = Math.min(MAXC, Math.max(BASE_COLS, ex[1], focus[1] + 1, anchor[1] + 1, lastC + 3));
        var ch = nr !== nRows || nc !== nCols; nRows = nr; nCols = nc; return ch;
    }
    function draw() { cache = { v: {}, stack: {} }; render(true); }
    function render(force) {
        if (editing) return;
        var changed = fit();
        if (changed || force || !rowTop.length || rowTop.length !== nRows + 1) calcTops();
        var vt = Math.max(0, sc.scrollTop - HEAD), vb = Math.max(0, sc.scrollTop + sc.clientHeight - HEAD);
        var va = rowAt(vt), vz = rowAt(vb);
        if (!force && !changed && winA >= 0 && va >= winA && vz <= winB) return;
        winA = Math.max(0, va - 15); winB = Math.min(nRows - 1, vz + 15);
        var h = '<table><colgroup><col style="width:46px">';
        for (var c = 0; c < nCols; c++) h += '<col data-cg="' + c + '" style="width:' + (colW[c] || 100) + 'px">';
        h += '</colgroup><thead><tr><th class="sg-rh"></th>';
        for (c = 0; c < nCols; c++) h += '<th data-c="' + c + '">' + L(c) + '<div class="sg-rs" data-rc="' + c + '"></div></th>';
        h += '</tr></thead><tbody>';
        if (rowTop[winA] > 0) h += '<tr style="height:' + rowTop[winA] + 'px"><td colspan="' + (nCols + 1) + '" style="height:0;padding:0;border:0"></td></tr>';
        for (var r = winA; r <= winB; r++) {
            h += '<tr style="height:' + rh(r) + 'px"><td class="sg-rh" data-r="' + r + '">' + (r + 1) + '<div class="sg-rr" data-rr="' + r + '"></div></td>';
            for (c = 0; c < nCols; c++) {
                var a = A(r, c), st = styles[a] || {}, v = get(a), sv = show(a), css = '';
                if (st.b) css += 'font-weight:700;'; if (st.i) css += 'font-style:italic;';
                if (st.u || st.st) css += 'text-decoration:' + (st.u ? 'underline ' : '') + (st.st ? 'line-through' : '') + ';';
                if (st.c) css += 'color:' + st.c + ';'; if (st.bg) css += 'background:' + st.bg + ';';
                css += 'text-align:' + (st.al || (typeof v === 'number' ? 'right' : 'left')) + ';';
                h += '<td data-a="' + a + '" data-r="' + r + '" data-c="' + c + '"' + (v instanceof E ? ' class="err"' : '') + ' style="' + css + '">' + o.esc(sv) + '</td>';
            }
            h += '</tr>';
        }
        var rest = rowTop[nRows] - rowTop[winB + 1];
        if (rest > 0) h += '<tr style="height:' + rest + 'px"><td colspan="' + (nCols + 1) + '" style="height:0;padding:0;border:0"></td></tr>';
        var st0 = sc.scrollTop, sl0 = sc.scrollLeft;
        sc.innerHTML = h + '</tbody></table>';
        sc.scrollTop = st0; sc.scrollLeft = sl0;
        paintSel();
    }
    sc.addEventListener('scroll', function () { if (raf) return; raf = requestAnimationFrame(function () { raf = 0; render(false); }); });

    // ---- редагування ----
    function edit(r, c, initial) {
        var td = tdAt(r, c); if (!td) return;
        var a = A(r, c), orig = raw(a), done = false;
        td.innerHTML = '<input>'; var inp = td.firstChild;
        inp.value = initial != null ? initial : orig; editing = a; inp.focus();
        if (initial == null) inp.select(); else inp.setSelectionRange(inp.value.length, inp.value.length);
        inp.oninput = function () { fx.value = inp.value; };
        function finish(save, dr, dc) {
            if (done) return; done = true; editing = null;
            if (save && inp.value !== orig) commit((function () { var x = {}; x[a] = { v: inp.value }; return x; })()); else draw();
            setSel(r + dr, c + dc, false); sc.focus();
        }
        inp.onkeydown = function (e) {
            if (e.key === 'Enter') { e.preventDefault(); finish(true, e.shiftKey ? -1 : 1, 0); }
            else if (e.key === 'Tab') { e.preventDefault(); finish(true, 0, e.shiftKey ? -1 : 1); }
            else if (e.key === 'Escape') { e.preventDefault(); finish(false, 0, 0); }
            e.stopPropagation();
        };
        inp.onblur = function () { finish(true, 0, 0); };
    }

    // ---- буфер обміну ----
    function tsv() { var q = rect(), rows = []; for (var r = q[0]; r <= q[2]; r++) { var row = []; for (var c = q[1]; c <= q[3]; c++) row.push(show(A(r, c))); rows.push(row.join('\t')); } return rows.join('\n'); }
    function doCopy(e, cut) {
        var q = rect(), data = {};
        each(function (a, r, c) { data[(r - q[0]) + ',' + (c - q[1])] = { v: raw(a), s: styles[a] || null }; });
        var t = tsv(); clip = { q: q, data: data, text: t };
        if (e && e.clipboardData) { e.clipboardData.setData('text/plain', t); e.preventDefault(); }
        if (cut) { var ch = {}; each(function (a) { ch[a] = { v: '' }; }); commit(ch); }
    }
    function doPaste(e) {
        e.preventDefault();
        var t = (e.clipboardData || window.clipboardData).getData('text/plain'), q = rect(), ch = {};
        if (clip && clip.text === t.replace(/\r/g, '')) {
            var h = clip.q[2] - clip.q[0] + 1, w = clip.q[3] - clip.q[1] + 1;
            for (var r = 0; r < h; r++) for (var c = 0; c < w; c++) {
                var d = clip.data[r + ',' + c], v = d.v;
                if (v && v[0] === '=') v = '=' + shiftF(v.slice(1), q[0] - clip.q[0], q[1] - clip.q[1]);
                ch[A(q[0] + r, q[1] + c)] = { v: v, s: d.s };
            }
        } else {
            t.replace(/\r/g, '').replace(/\n$/, '').split('\n').forEach(function (line, r) {
                line.split('\t').forEach(function (v, c) { ch[A(q[0] + r, q[1] + c)] = { v: v }; });
            });
        }
        if (Object.keys(ch).length) commit(ch);
    }

    // ---- форматування ----
    function fmt(key, val) {
        var ch = {}, first = styles[A(rect()[0], rect()[1])] || {}, on = (val === undefined) ? !first[key] : val;
        each(function (a) { var s = Object.assign({}, styles[a] || {}); if (on === false || on === '' || on == null) delete s[key]; else s[key] = on; ch[a] = { s: s }; });
        commit(ch);
    }
    function syncTb() {
        var s = styles[A(focus[0], focus[1])] || {};
        ['b', 'i', 'u', 'st'].forEach(function (k) { var b = tb.querySelector('[data-f="' + k + '"]'); if (b) b.classList.toggle('on', !!s[k]); });
        var sel = tb.querySelector('[data-nf]'); if (sel) sel.value = s.nf || '';
    }
    tb.innerHTML = '<button data-x="undo" title="Undo (Ctrl+Z)">↶</button><button data-x="redo" title="Redo (Ctrl+Y)">↷</button><span class="sg-sep"></span>' +
        '<select data-nf title="Format"><option value="">Auto</option><option value="num">Number 1,000.12</option><option value="pct">Percent</option><option value="cur">Currency ₴</option></select><span class="sg-sep"></span>' +
        '<button data-f="b" title="Bold (Ctrl+B)"><b>B</b></button><button data-f="i" title="Italic (Ctrl+I)"><i>I</i></button><button data-f="u" title="Underline (Ctrl+U)"><u>U</u></button><button data-f="st" title="Strikethrough"><s>S</s></button>' +
        '<input type="color" data-col="c" title="Text color" value="#000000"><input type="color" data-col="bg" title="Fill color" value="#ffff00"><span class="sg-sep"></span>' +
        '<button data-al="left" title="Left">⇤</button><button data-al="center" title="Center">↔</button><button data-al="right" title="Right">⇥</button><span class="sg-sep"></span>' +
        '<button data-x="clearfmt" title="Clear formatting">Tx</button>';
    tb.onmousedown = function (e) { if (e.target.tagName !== 'SELECT' && e.target.type !== 'color') e.preventDefault(); };
    tb.onclick = function (e) {
        var b = e.target.closest('button'); if (!b) return;
        if (b.dataset.f) fmt(b.dataset.f);
        else if (b.dataset.al) fmt('al', b.dataset.al);
        else if (b.dataset.x === 'undo') doUndo(); else if (b.dataset.x === 'redo') doRedo();
        else if (b.dataset.x === 'clearfmt') { var ch = {}; each(function (a) { ch[a] = { s: null }; }); commit(ch); }
        sc.focus();
    };
    tb.onchange = function (e) {
        if (e.target.dataset.nf !== undefined) fmt('nf', e.target.value);
        else if (e.target.dataset.col) fmt(e.target.dataset.col, e.target.value);
        sc.focus();
    };

    // ---- миша ----
    var dragging = false;
    sc.addEventListener('mousedown', function (e) {
        if (e.target.closest('.sg-rs,.sg-rr') || editing) return;
        var td = e.target.closest('td[data-a]'), th = e.target.closest('th[data-c]'), rh = e.target.closest('td[data-r]');
        if (td) { var p = P(td.dataset.a); setSel(p[0], p[1], e.shiftKey); dragging = true; }
        else if (th) { var c = +th.dataset.c; anchor = [0, c]; focus = [nRows - 1, c]; if (e.shiftKey) anchor = [0, Math.min(c, anchor[1])]; paintSel(); }
        else if (rh) { var r = +rh.dataset.r; anchor = [r, 0]; focus = [r, nCols - 1]; paintSel(); }
        else if (e.target.closest('th.sg-rh')) { anchor = [0, 0]; focus = [nRows - 1, nCols - 1]; paintSel(); }
        sc.focus();
    });
    sc.addEventListener('mouseover', function (e) { if (!dragging) return; var td = e.target.closest('td[data-a]'); if (td) { var p = P(td.dataset.a); focus = [p[0], p[1]]; paintSel(); } });
    document.addEventListener('mouseup', function () { dragging = false; });
    sc.addEventListener('dblclick', function (e) { var td = e.target.closest('td[data-a]'); if (td) { var p = P(td.dataset.a); edit(p[0], p[1]); } });

    // ресайз (мишa + дотик): pointer capture, щоб жест не віддавався скролу/зуму
    sc.addEventListener('pointerdown', function (e) {
        var hc = e.target.closest('.sg-rs'), hr = e.target.closest('.sg-rr'); if (!hc && !hr) return;
        e.preventDefault(); e.stopPropagation();
        var h = hc || hr, pid = e.pointerId, done = false;
        var sx = e.clientX, sy = e.clientY, ci = hc && +hc.dataset.rc, ri = hr && +hr.dataset.rr, w0 = colW[ci] || 100, h0 = rowH[ri] || 25, cur, colEl = hc && sc.querySelector('col[data-cg="' + ci + '"]'), trEl = hr && hr.closest('tr');
        try { h.setPointerCapture(pid); } catch (x) {}
        h.classList.add('on');
        var ov = sc.style.overflow; sc.style.overflow = 'hidden';   // поки тягнемо — таблиця не гортається
        function mv(ev) {
            if (ev.pointerId !== pid) return;
            if (hc) { cur = Math.max(40, Math.min(600, w0 + ev.clientX - sx)); colEl.style.width = cur + 'px'; }
            else { cur = Math.max(20, Math.min(400, h0 + ev.clientY - sy)); trEl.style.height = cur + 'px'; }
        }
        function end(ev, commitIt) {
            if (done || (ev && ev.pointerId !== pid)) return; done = true;
            h.removeEventListener('pointermove', mv); h.removeEventListener('pointerup', up);
            h.removeEventListener('pointercancel', cancel); h.removeEventListener('lostpointercapture', cancel);
            try { h.releasePointerCapture(pid); } catch (x) {}
            h.classList.remove('on'); sc.style.overflow = ov;
            if (!commitIt || cur == null) { if (hc) colEl.style.width = w0 + 'px'; else trEl.style.height = h0 + 'px'; return; }
            if (hc) { colW[ci] = cur; api('/uadmin/table/api/col/width', { table_id: tid, column_id: L(ci), width: cur }); }
            else { rowH[ri] = cur; api('/uadmin/table/api/row/height', { table_id: tid, row_id: String(ri + 1), height: cur }); render(true); }
        }
        function up(ev) { end(ev, true); }
        function cancel(ev) { end(ev, false); }
        h.addEventListener('pointermove', mv); h.addEventListener('pointerup', up);
        h.addEventListener('pointercancel', cancel); h.addEventListener('lostpointercapture', cancel);
    });

    // ---- клавіатура ----
    sc.addEventListener('keydown', function (e) {
        if (editing) return;
        var mod = e.ctrlKey || e.metaKey, k = e.key, f = focus;
        if (mod) {
            var l = k.toLowerCase();
            if (l === 'z') { e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); }
            else if (l === 'y') { e.preventDefault(); doRedo(); }
            else if (l === 'b') { e.preventDefault(); fmt('b'); } else if (l === 'i') { e.preventDefault(); fmt('i'); } else if (l === 'u') { e.preventDefault(); fmt('u'); }
            else if (l === 'a') { e.preventDefault(); anchor = [0, 0]; focus = [nRows - 1, nCols - 1]; paintSel(); }
            else if (l === 'd') { e.preventDefault(); var q = rect(), ch = {}; for (var c = q[1]; c <= q[3]; c++) { var src = raw(A(q[0], c)); for (var r = q[0] + 1; r <= q[2]; r++) ch[A(r, c)] = { v: src && src[0] === '=' ? '=' + shiftF(src.slice(1), r - q[0], 0) : src, s: styles[A(q[0], c)] || null }; } if (Object.keys(ch).length) commit(ch); }
            else if (l === 'r') { e.preventDefault(); var q2 = rect(), ch2 = {}; for (var r3 = q2[0]; r3 <= q2[2]; r3++) { var s2 = raw(A(r3, q2[1])); for (var c3 = q2[1] + 1; c3 <= q2[3]; c3++) ch2[A(r3, c3)] = { v: s2 && s2[0] === '=' ? '=' + shiftF(s2.slice(1), 0, c3 - q2[1]) : s2, s: styles[A(r3, q2[1])] || null }; } if (Object.keys(ch2).length) commit(ch2); }
            else if (k === 'Home') { e.preventDefault(); setSel(0, 0, e.shiftKey); }
            else if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'ArrowLeft' || k === 'ArrowRight') {
                e.preventDefault();
                var ex = usedExtent();
                setSel(k === 'ArrowDown' ? Math.max(ex[0] - 1, 0) : k === 'ArrowUp' ? 0 : f[0], k === 'ArrowRight' ? Math.max(ex[1] - 1, 0) : k === 'ArrowLeft' ? 0 : f[1], e.shiftKey);
            }
            return;
        }
        var mv = { ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[k];
        if (mv) { e.preventDefault(); setSel(f[0] + mv[0], f[1] + mv[1], e.shiftKey); }
        else if (k === 'Tab') { e.preventDefault(); setSel(f[0], f[1] + (e.shiftKey ? -1 : 1), false); }
        else if (k === 'Enter') { e.preventDefault(); edit(f[0], f[1]); }
        else if (k === 'F2') { e.preventDefault(); edit(f[0], f[1]); }
        else if (k === 'Delete' || k === 'Backspace') { e.preventDefault(); var ch3 = {}; each(function (a) { ch3[a] = { v: '' }; }); commit(ch3); }
        else if (k === 'Escape') { clip = null; }
        else if (k.length === 1) { e.preventDefault(); edit(f[0], f[1], k); }
    });
    sc.addEventListener('copy', function (e) { doCopy(e, false); });
    sc.addEventListener('cut', function (e) { doCopy(e, true); });
    sc.addEventListener('paste', doPaste);

    // ---- name box + рядок формул ----
    nb.onkeydown = function (e) {
        if (e.key !== 'Enter') return;
        var parts = nb.value.toUpperCase().split(':'), a = P(parts[0]), b = parts[1] ? P(parts[1]) : a;
        if (a && b) { anchor = a; focus = b; paintSel(); sc.focus(); }
    };
    fx.onkeydown = function (e) {
        if (e.key === 'Enter') { var a = A(focus[0], focus[1]), x = {}; x[a] = { v: fx.value }; commit(x); sc.focus(); }
        else if (e.key === 'Escape') { fx.value = raw(A(focus[0], focus[1])); sc.focus(); }
    };

    draw(); sc.focus();
};
})();

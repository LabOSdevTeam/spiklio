"""table_import_page.py — "Import table" page (one self-contained HTML page, styled like /uadmin)."""
import json

_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Import table — Spiklio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Poppins:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{--cw-ink:#14172b;--cw-primary:#4c5fd7;--cw-primary-dark:#3f50c4;--cw-soft:#eef0ff;--cw-line:#dfe3f5;--cw-bg:#f6f8ff;
--cw-mut:rgba(20,23,43,.58);--cw-ok:#15803d;--cw-ok-bg:#e3f8ea;--cw-bad:#d6333f;--cw-bad-bg:#fdecee;--r:14px;
--shadow:0 1px 2px rgba(20,23,43,.04),0 8px 24px -14px rgba(76,95,215,.25)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;background:var(--cw-bg);color:var(--cw-ink);-webkit-font-smoothing:antialiased}
h1{font-family:'Poppins','Inter',sans-serif;letter-spacing:-.01em;font-size:1.5rem;margin:14px 0 4px}
.wrap{max-width:820px;margin:0 auto;padding:24px 16px 64px}
.sub{color:var(--cw-mut);line-height:1.5;margin-bottom:18px}
.cx-card{background:#fff;border:1px solid var(--cw-line);border-radius:var(--r);box-shadow:var(--shadow);padding:18px;margin-bottom:14px}
input[type=text],select{width:100%;background:#fff;border:1px solid var(--cw-line);border-radius:10px;padding:.6rem .8rem;font:inherit;color:var(--cw-ink);outline:none;transition:border-color .2s,box-shadow .2s}
input:focus,select:focus{border-color:rgba(76,95,215,.6);box-shadow:0 0 0 4px rgba(76,95,215,.1)}
input::placeholder{color:rgba(20,23,43,.4)}
input[type=checkbox],input[type=radio]{accent-color:var(--cw-primary);width:15px;height:15px}
.btn-accent,.ghost{display:inline-flex;align-items:center;justify-content:center;gap:8px;border-radius:10px;padding:.6rem 1.1rem;font:inherit;font-weight:600;font-size:.88rem;cursor:pointer;border:1px solid transparent;text-decoration:none;white-space:nowrap;transition:background .15s,border-color .15s,color .15s}
.btn-accent{background:var(--cw-primary);color:#fff}.btn-accent:hover{background:var(--cw-primary-dark)}
.ghost{background:#fff;color:var(--cw-ink);border-color:var(--cw-line)}.ghost:hover{border-color:var(--cw-primary);color:var(--cw-primary)}
.ghost.on{background:var(--cw-soft);border-color:var(--cw-primary);color:var(--cw-primary)}
.small{padding:.4rem .8rem;font-size:.8rem;border-radius:8px}
button[disabled]{opacity:.5;cursor:default}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.mut{color:var(--cw-mut);font-size:.78rem}
.tabs{display:flex;gap:8px;margin-bottom:14px}
.drop{border:1.5px dashed var(--cw-line);border-radius:12px;padding:28px 16px;text-align:center;color:var(--cw-mut);cursor:pointer;transition:border-color .15s,background .15s}
.drop:hover,.drop.hot{border-color:var(--cw-primary);background:var(--cw-soft);color:var(--cw-primary)}
.error{background:var(--cw-bad-bg);color:var(--cw-bad);border-radius:10px;padding:.6rem .8rem;margin-top:12px;font-size:.86rem}
.error:empty{display:none}
.success{background:var(--cw-ok-bg);color:var(--cw-ok);border-radius:10px;padding:.6rem .8rem;margin-top:12px;font-size:.86rem}
h4{font-size:.72rem;text-transform:uppercase;letter-spacing:1px;color:var(--cw-mut);font-weight:600;margin:16px 0 8px}h4:first-child{margin-top:0}
.sheet{display:flex;gap:10px;align-items:center;padding:.5rem 0;border-bottom:1px solid var(--cw-line)}.sheet:last-child{border:0}
.sheet b{flex:1;font-weight:500}
.pv-wrap{overflow:auto;max-height:260px;margin-top:10px;border:1px solid var(--cw-line);border-radius:var(--r)}
table.pv{border-collapse:separate;border-spacing:0;width:100%;font-size:.82rem}
table.pv td{border-bottom:1px solid var(--cw-line);padding:.45rem .8rem;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}
table.pv tr:first-child td{background:#fafbff;font-weight:600}
table.pv tr:last-child td{border-bottom:none}
.opt{display:flex;gap:10px;align-items:flex-start;padding:.7rem .9rem;border:1px solid var(--cw-line);border-radius:10px;margin-bottom:8px;cursor:pointer;background:#fff}
.opt input{margin-top:3px}.opt.on{border-color:var(--cw-primary);background:var(--cw-soft)}
.opt span{display:block;font-weight:500}.opt small{display:block;color:var(--cw-mut);font-size:.78rem;margin-top:2px}
.check{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:.86rem}.check.bad{color:var(--cw-bad)}
.hide{display:none}
a{color:var(--cw-primary);text-decoration:none}a:hover{text-decoration:underline}
</style></head><body><div class="wrap">
<a class="ghost small" href="/uadmin/dashboard#table">&larr; Tables</a>
<h1>Import table</h1>
<p class="sub">Excel (.xlsx), OpenDocument (.ods), CSV/TSV or a Google Sheet. Formulas are imported as values; merged cells are kept.</p>

<div class="cx-card">
  <div class="tabs"><button class="ghost small on" data-t="file" type="button">File</button><button class="ghost small" data-t="url" type="button">Google Sheet</button></div>
  <div id="p-file"><div class="drop" id="drop">Drop a file here or click to choose<br><span class="mut" id="fname">.xlsx, .xlsm, .ods, .csv, .tsv, .txt &middot; up to 10 MB</span></div>
    <input type="file" id="file" class="hide" accept=".xlsx,.xlsm,.ods,.csv,.tsv,.txt,.xls"></div>
  <div id="p-url" class="hide"><input type="text" id="url" placeholder="https://docs.google.com/spreadsheets/d/…">
    <p class="mut" style="margin-top:8px">Easiest: in Google Sheets open Share and set general access to &ldquo;Anyone with the link&rdquo; (Viewer). Private sheets work only if Google is connected in Settings and the sheet was chosen there.</p></div>
  <div class="row" style="margin-top:14px"><button class="btn-accent" id="go" type="button">Preview</button><span id="msg1" class="mut"></span></div>
  <div class="error" id="err1"></div>
</div>

<div class="cx-card hide" id="step2">
  <h4>What to import</h4>
  <div id="sheets"></div>
  <div class="pv-wrap"><table class="pv" id="pv"></table></div>
  <p class="mut" id="warn" style="margin-top:8px"></p>
  <h4>Destination</h4>
  <label class="opt on" data-m="new"><input type="radio" name="mode" value="new" checked><div><span>New table</span><small>Each selected sheet becomes a separate new table. Nothing is overwritten.</small></div></label>
  <label class="opt" data-m="append"><input type="radio" name="mode" value="append"><div><span>Append to an existing table</span><small>First selected sheet only. Rows are added below the last filled row.</small></div></label>
  <label class="opt" data-m="replace"><input type="radio" name="mode" value="replace"><div><span>Replace an existing table</span><small>The table&rsquo;s current data will be deleted.</small></div></label>
  <div id="target" class="hide" style="margin-top:10px"><select id="tid"></select>
    <label id="hdrwrap" class="check hide"><input type="checkbox" id="skiph"> The file has a header row &mdash; don&rsquo;t add its first row</label>
    <label id="confwrap" class="check bad hide"><input type="checkbox" id="conf"> I understand the current data of this table will be deleted</label></div>
  <div class="row" style="margin-top:16px"><button class="btn-accent" id="do" type="button">Import</button><span id="msg2" class="mut"></span></div>
  <div class="error" id="err2"></div><div id="res"></div>
</div>
</div>
<script>
const CSRF = __CSRF__, DEFAULT_TID = __TID__, DEFAULT_MODE = __MODE__;
const $ = s => document.querySelector(s);
let kind = "file", picked = null, sheets = [];
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  kind = b.dataset.t; document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("#p-file").classList.toggle("hide", kind !== "file"); $("#p-url").classList.toggle("hide", kind !== "url");
});
const drop = $("#drop"), fin = $("#file");
drop.onclick = () => fin.click();
fin.onchange = () => setFile(fin.files[0]);
["dragover","dragenter"].forEach(e => drop.addEventListener(e, ev => {ev.preventDefault(); drop.classList.add("hot")}));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => {ev.preventDefault(); drop.classList.remove("hot")}));
drop.addEventListener("drop", ev => setFile(ev.dataTransfer.files[0]));
function setFile(f){ picked = f || null; $("#fname").textContent = f ? f.name + " (" + Math.max(1, Math.round(f.size/1024)) + " KB)" : ""; }
function fd(extra){
  const f = new FormData(); f.append("csrf_token", CSRF);
  if (kind === "file") { if (picked) f.append("file", picked); } else f.append("url", $("#url").value.trim());
  for (const k in (extra||{})) (Array.isArray(extra[k]) ? extra[k] : [extra[k]]).forEach(v => f.append(k, v));
  return f;
}
async function post(u, body){
  const r = await fetch(u, {method:"POST", body, headers:{"X-CSRF-Token":CSRF}});
  let j = {}; try { j = await r.json(); } catch(e) {}
  if (!r.ok || !j.ok) throw new Error(j.error || ("Error " + r.status));
  return j;
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
$("#go").onclick = async () => {
  $("#err1").textContent = ""; $("#msg1").textContent = "Reading…"; $("#go").disabled = true;
  try {
    const j = await post("/uadmin/table/api/import/preview", fd());
    sheets = j.sheets; renderSheets(); $("#step2").classList.remove("hide"); loadTables();
  } catch(e) { $("#err1").textContent = e.message; $("#step2").classList.add("hide"); }
  $("#msg1").textContent = ""; $("#go").disabled = false;
};
function renderSheets(){
  $("#sheets").innerHTML = sheets.map((s,i) =>
    `<div class="sheet"><input type="checkbox" class="sh" data-n="${esc(s.name)}" ${s.rows? "checked":"disabled"}>
     <b>${esc(s.name)}</b><span class="mut">${s.rows} &times; ${s.cols}</span><button class="ghost small" type="button" data-i="${i}">View</button></div>`).join("");
  document.querySelectorAll("#sheets button").forEach(b => b.onclick = () => showPv(+b.dataset.i));
  showPv(0);
  $("#warn").textContent = sheets.flatMap(s => s.warnings).join(" ");
}
function showPv(i){
  const s = sheets[i]; if (!s) return;
  $("#pv").innerHTML = s.preview.map(r => "<tr>" + r.map(c => `<td>${esc(c)}</td>`).join("") + "</tr>").join("") || "<tr><td>(empty)</td></tr>";
}
async function loadTables(){
  try {
    const r = await fetch("/uadmin/table/api/tables"); const j = await r.json();
    $("#tid").innerHTML = (j.tables||[]).map(t => `<option value="${esc(t.table_id)}" ${t.table_id===DEFAULT_TID?"selected":""}>${esc(t.name)}${t.is_default?" (default)":""}</option>`).join("");
  } catch(e) {}
}
document.querySelectorAll("input[name=mode]").forEach(r => r.onchange = updMode);
function updMode(){
  const m = document.querySelector("input[name=mode]:checked").value;
  document.querySelectorAll(".opt").forEach(o => o.classList.toggle("on", o.dataset.m === m));
  $("#target").classList.toggle("hide", m === "new");
  $("#hdrwrap").classList.toggle("hide", m !== "append"); $("#confwrap").classList.toggle("hide", m !== "replace");
}
$("#do").onclick = async () => {
  $("#err2").textContent = ""; $("#res").innerHTML = "";
  const mode = document.querySelector("input[name=mode]:checked").value;
  const chosen = [...document.querySelectorAll(".sh:checked")].map(x => x.dataset.n);
  if (!chosen.length) { $("#err2").textContent = "Select at least one sheet."; return; }
  if (mode === "replace" && !$("#conf").checked) { $("#err2").textContent = "Confirm the replacement first."; return; }
  $("#msg2").textContent = "Importing…"; $("#do").disabled = true;
  try {
    const j = await post("/uadmin/table/api/import/commit", fd({mode, sheet: chosen, table_id: $("#tid").value || "",
      skip_header: $("#skiph").checked ? "1" : "", confirm_replace: $("#conf").checked ? "1" : ""}));
    $("#res").innerHTML = "<div class='success'><b>Done.</b><br>" + j.results.map(r =>
      `${esc(r.name)}: ${r.rows} rows &times; ${r.cols} columns${r.merges? ", " + r.merges + " merged":""}`).join("<br>") + "</div>" +
      (j.warnings.length ? `<p class="mut" style="margin-top:8px">${esc(j.warnings.join(" "))}</p>` : "") +
      `<p style="margin-top:12px"><a href="/uadmin/dashboard#table">Open tables &rarr;</a></p>`;
  } catch(e) { $("#err2").textContent = e.message; }
  $("#msg2").textContent = ""; $("#do").disabled = false;
};
if (DEFAULT_MODE === "append") { document.querySelector("input[name=mode][value=append]").checked = true; updMode(); }
</script></body></html>"""


def render_page(csrf_token: str, default_table_id: str = "", mode: str = "") -> str:
    def js(v):
        return json.dumps(v or "").replace("</", "<\\/")
    return (_PAGE.replace("__CSRF__", js(csrf_token))
            .replace("__TID__", js(default_table_id))
            .replace("__MODE__", js(mode)))

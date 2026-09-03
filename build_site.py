#!/usr/bin/env python3
"""Build ben_jobs.html (local) and index.html (GitHub Pages, encrypted).

The Pages build encrypts the jobs payload with AES-256-GCM; the key is derived
from a password via PBKDF2-SHA256 (200k iters) in the browser (WebCrypto).
Without the password the page contains only ciphertext. Set the password with
the PAGES_PASSWORD env var or PAGES_PASSWORD_FILE (first line).

Usage:  python3 build_site.py [--pages]
"""
import base64
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "cached_jobs.json")
OUT = os.path.join(HERE, "ben_jobs.html")
def clean_desc(md):
    """Markdown blob -> readable plain text with \n\n paragraph breaks."""
    if not md: return ""
    t = str(md).replace("\\n", "\n")
    t = re.sub(r"\\([\-.*()#\[\]`_>+!&])", r"\1", t)          # unescape markdown
    t = re.sub(r"\\(?![a-zA-Z])", "", t)                       # stray backslashes
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)          # links/images -> text
    t = re.sub(r"https?://\S+", "", t)                        # bare URLs
    t = re.sub(r"[#*`_>|]{1,3}", "", t)                       # markdown chars
    t = re.sub(r"&[a-z]+;", " ", t)                           # html entities
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
    paras = [p for p in paras if p and not re.fullmatch(r"[\W_]+", p)]
    return "\n\n".join(paras)

_NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_SAL_RE = re.compile(r"£\s?" + _NUM + r"\s?(k)?(?:\s*(?:–|—|-|to)\s*£?\s?" + _NUM + r"\s?(k)?)?", re.I)
_UNIT_WORDS = [("hour", "hourly"), ("hr", "hourly"), ("day", "daily"), ("week", "weekly"),
               ("month", "monthly"), ("annum", "yearly"), ("year", "yearly"), ("annual", "yearly")]

def _interval_for(amount, context):
    for word, iv in _UNIT_WORDS:
        if re.search(rf"\b{word}", context, re.I): return iv  # prefix-boundary: \bmonth matches "monthly"
    return "hourly" if amount < 100 else "daily" if amount < 400 else "weekly" if amount < 1500 else "yearly"

# Price-like context: £figure preceded by these words is probably NOT a wage.
_PRICE_BEFORE = re.compile(r"\b(from|only|just|pay just|costs?|spend)\b[^.]{0,15}$", re.I)
# Wage-ish keywords that let an unlabelled small amount pass ("Up to £12.50 …").
_WAGE_BEFORE = re.compile(r"\b(salary|wage|pay|rate|earn|up to|offer(?:ing)?)\b[^.]{0,20}$", re.I)

def _amt(num, k):
    v = float(num.replace(",", ""))
    return v * 1000 if k else v

def _fmt_amount(v):
    return f"£{v:,.0f}" if v == int(v) else f"£{v:,.2f}"

def salary_from_text(text):
    """Extract '£X–£Y an hour' style salary from description text, or None."""
    t = clean_desc(text)[:1500]
    # Score every £-match; pick the most wage-like (skip bonuses/prices).
    best = None
    for m in _SAL_RE.finditer(t):
        after2 = t[m.end():m.end() + 3].lstrip()
        if after2.startswith("!"):               # "£50!" -> referral bonus, not pay
            continue
        before15 = t[max(0, m.start() - 15):m.start()]
        if re.search(r"\b(another|bonus|reward|prize)\b\s*$", before15, re.I):
            continue
        after60 = t[m.end():m.end() + 60]
        before40 = t[max(0, m.start() - 40):m.start()]
        has_unit = any(re.search(rf"\b{w}", after60, re.I) for w, _ in _UNIT_WORDS)
        pay_kw = re.search(r"\b(pay|salary|wage|rate)\b", before40, re.I)
        score = (3 if pay_kw else 0) + (2 if has_unit else 0)
        if best is None or score > best[0]:
            best = (score, m)
    if best is None:
        return None
    m = best[1]
    lo, lok, hi, hik = m.group(1), m.group(2), m.group(3), m.group(4)
    lo_v, hi_v = _amt(lo, lok), _amt(hi, hik) if hi else None
    ctx = t[max(0, m.start() - 60):m.end() + 60]
    has_unit = any(re.search(rf"\b{w}", ctx, re.I) for w, _ in _UNIT_WORDS)
    if lok or hik:
        iv = "yearly"
    elif has_unit:
        iv = _interval_for(lo_v, ctx)
    else:
        if lo_v >= 15000 or (hi_v or 0) >= 15000:
            iv = "yearly"
        elif lo_v >= 100:
            iv = "daily" if lo_v < 400 else "weekly" if lo_v < 1500 else "yearly"
        else:
            before = t[max(0, m.start() - 30):m.start()]
            if _PRICE_BEFORE.search(before) or not _WAGE_BEFORE.search(before):
                return None  # small amount, no unit, no salary framing -> a price
            iv = "hourly"
    f = _fmt_amount
    amt = f(lo_v) if hi_v is None or lo_v == hi_v else f"{f(lo_v)}–{f(hi_v)}"
    per = {"yearly": "a year", "monthly": "a month", "weekly": "a week", "daily": "a day", "hourly": "an hour"}[iv]
    return f"{amt} {per}"

def _snippet(clean, n=220):
    first = re.split(r"\n\n", clean)[0]
    if len(first) > n:
        first = first[:n].rsplit(" ", 1)[0] + "…"
    return first

def salary_str(j):
    lo, hi, iv, cur = j.get("min_amount"), j.get("max_amount"), j.get("interval"), j.get("currency") or ""
    if lo is None and hi is None: return None
    f = lambda v: f"{cur}{v:,.0f}"
    amt = f(lo) if lo == hi else (f"{f(lo)}–{f(hi)}" if lo and hi else f"{f(hi or lo)}+")
    per = {"yearly": "a year", "monthly": "a month", "weekly": "a week", "daily": "a day", "hourly": "an hour"}.get(iv, iv or "")
    return f"{amt} {per}".strip()

def age_days(j):
    d = j.get("date_posted")
    if not d: return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try: return (datetime.now() - datetime.strptime(d, fmt)).days
        except ValueError: pass
    return None

def _fmt_dt(s):
    if not s: return None
    s = str(s).replace("T", " ")[:16]
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            base = dt.strftime("%-d %b")
            return f"{base}, {dt.strftime('%H:%M')}" if fmt.endswith("%H:%M") else base
        except ValueError: pass
    return s

def main():
    with open(CACHE) as f: cache = json.load(f)
    all_jobs = cache.get("jobs", {})
    jobs, hidden = [], 0
    for k, j in all_jobs.items():
        if j.get("fit") == "stretch":
            hidden += 1
            continue
        jobs.append(dict(j, key=k, age=age_days(j),
                    posted=_fmt_dt(j.get("date_posted")),
                    desc_clean=clean_desc(j.get("description"))[:1200],
                    fit=j.get("fit") or "maybe", fit_reason=j.get("fit_reason") or ""))
    for j in jobs:
        j["salary"] = salary_str(j) or salary_from_text(j.get("description"))
        j["snippet"] = _snippet(j["desc_clean"])
    jobs.sort(key=lambda j: ({"good": 0, "maybe": 1}.get(j["fit"], 2), j["age"] if j["age"] is not None else 999))
    payload = {"updated": cache.get("meta", {}).get("last_run", ""), "jobs": jobs, "hidden": hidden}
    cats = {}
    for j in jobs:
        for c in j.get("categories", []): cats[c] = cats.get(c, 0) + 1
    payload["cats"] = sorted(cats.items(), key=lambda x: -x[1])
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = (SHELL.replace("</body></html>",
            "<script>\nconst D=" + data + ";\n" + APP_JS + "\nstartApp();</script></body></html>"))
    with open(OUT, "w") as f: f.write(html)
    print(f"{OUT}: {len(jobs)} jobs shown, {hidden} stretch hidden")
    return payload, hidden

# ---------- GitHub Pages build (encrypted) ----------

def _pages_password():
    pf = os.environ.get("PAGES_PASSWORD_FILE")
    if pf:
        return open(pf).readline().strip()
    return os.environ.get("PAGES_PASSWORD", "")

def _encrypt_b64(password, plaintext_bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000, dklen=32)
    ct = AESGCM(key).encrypt(iv, plaintext_bytes, None)
    return (base64.b64encode(salt + iv + ct)).decode()

def build_pages(payload):
    pw = _pages_password()
    if not pw:
        print("! no PAGES_PASSWORD set -> skipped index.html build")
        return
    blob = _encrypt_b64(pw, json.dumps(payload, ensure_ascii=False).encode())
    html = (PAGES_TEMPLATE
            .replace("__APP_CSS_PLACEHOLDER__", APP_CSS)
            .replace("__APP_BODY__", APP_BODY)
            .replace("__APP_JS__", APP_JS)
            .replace("__BLOB__", blob))
    out = os.path.join(HERE, "index.html")
    with open(out, "w") as f: f.write(html)
    print(f"{out}: encrypted payload ({len(blob)//1024} KB, {len(payload['jobs'])} jobs)")

PAGES_TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Jobs for Ben — Sheffield</title>
<style>
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#f4f5f7}
#gate{display:flex;justify-content:center;padding-top:18vh}
.box{background:#fff;border:1px solid #ddd;border-radius:14px;padding:28px 22px;width:min(360px,90vw);text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.box h1{font-size:20px;margin-bottom:6px}
.box p{color:#666;font-size:13px;margin-bottom:16px}
.box input{width:100%;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin-bottom:10px}
.box button{width:100%;padding:12px;background:#14532d;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
#err{color:#b91c1c;font-size:13px;min-height:18px;margin-top:8px}
#app{display:none}
__APP_CSS_PLACEHOLDER__
</style></head><body>
<div id="gate"><div class="box"><h1>🔨 Jobs for Ben</h1><p>This page is password protected</p>
<input id="pw" type="password" autocapitalize="none" placeholder="Password" autofocus>
<button id="go">Unlock</button><div id="err"></div></div></div>
<div id="app">__APP_BODY__</div>
<script>
let D=null;
const BLOB="__BLOB__";
const te=new TextEncoder(),td=new TextDecoder();
async function decrypt(pw){
  const raw=Uint8Array.from(atob(BLOB),c=>c.charCodeAt(0));
  const salt=raw.slice(0,16),iv=raw.slice(16,28),ct=raw.slice(28);
  const km=await crypto.subtle.importKey("raw",te.encode(pw),"PBKDF2",false,["deriveKey"]);
  const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt,iterations:200000,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
  return JSON.parse(td.decode(await crypto.subtle.decrypt({name:"AES-GCM",iv},key,ct)));
}
async function unlock(){
  try{
    D=await decrypt(document.getElementById("pw").value);
    document.getElementById("gate").style.display="none";
    document.getElementById("app").style.display="block";
    startApp();
  }catch(e){document.getElementById("err").textContent="Wrong password";document.getElementById("pw").value="";}
}
document.getElementById("go").addEventListener("click",unlock);
document.getElementById("pw").addEventListener("keydown",e=>{if(e.key==="Enter")unlock();});
__APP_JS__
</script></body></html>
"""

SHELL = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Jobs for Ben — Sheffield</title>
<style>
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#f4f5f7;color:#1a1a1a;padding-bottom:calc(24px + env(safe-area-inset-bottom))}
header{background:#14532d;color:#fff;padding:calc(14px + env(safe-area-inset-top)) 16px 12px}
header h1{font-size:20px}
header p{opacity:.85;font-size:13px;margin-top:4px}
.controls{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 12px;z-index:5}
.controls input{width:100%;padding:11px 12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin-bottom:8px;-webkit-appearance:none}
.ctrlrow{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
.sortsel{font-size:13px;padding:7px 12px;background:#eef;border:1px solid #ccd;border-radius:8px;cursor:pointer;flex-shrink:0}
.hint{font-size:12px;color:#777}
.chips{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;border:1px solid #ccc;background:#fff;border-radius:18px;padding:8px 14px;font-size:14px;cursor:pointer}
.chip.on{background:#14532d;color:#fff;border-color:#14532d}

main{max-width:760px;margin:0 auto;padding:10px 12px}
.card{background:#fff;border:1px solid #e2e2e2;border-radius:12px;padding:14px;margin:10px 0}
.card h2{font-size:17px;line-height:1.3;margin-bottom:2px}
.card .co{color:#555;font-size:14px}
.dates{font-size:12px;color:#777;margin-top:3px}
.meta{margin:8px 0;font-size:13px;color:#444;display:flex;flex-wrap:wrap;gap:6px}
.tag{background:#e8f0e8;color:#14532d;border-radius:5px;padding:3px 8px;font-size:12px}
.tag.new{background:#fde68a;color:#713f12;font-weight:600}
.tag.fitgood{background:#dcfce7;color:#166534;font-weight:600}
.tag.fitmaybe{background:#ffedd5;color:#9a3412;font-weight:600}
.seclabel{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#888;margin:12px 0 4px;font-weight:600}
.desc{font-size:14px;line-height:1.45;color:#333;overflow-wrap:anywhere}
.more{display:none;margin-top:6px}
.open .more{display:block}
.toggler{background:none;border:none;color:#1d4ed8;font-size:14px;padding:8px 0;cursor:pointer}
.btn{display:block;width:100%;text-align:center;background:#14532d;color:#fff;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:600;margin-top:10px}
.btn:active{opacity:.8}
.empty{text-align:center;color:#777;padding:40px 0}
#status{font-size:12px;color:#666;margin:6px 0 0 12px}
@media(min-width:600px){.btn{display:inline-block;width:auto}}
</style></head><body>
<header><h1>🔨 Jobs for Ben — Sheffield</h1><p id="updated"></p></header>
<div class="controls">
  <input id="q" type="search" placeholder="Search title, company, description…">
  <div class="ctrlrow">
    <span class="hint" id="status"></span>
    <button class="sortsel" id="sortBtn" onclick="toggleSort()">Sort: Newest</button>
  </div>
  <div class="chips" id="chips"></div>
</div>
<main id="list"></main>
</body></html>
"""

APP_JS = r"""function startApp(){const NEW_DAYS=3;
const FIT_ORDER={good:0,maybe:1,stretch:2};
let cat='All',sortMode='fit';const MODES=['fit','new','az'],MODE_NAME={fit:'Best fit',new:'Newest',az:'A–Z'};
const esc=s=>(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function chips(){
  const el=document.getElementById('chips');el.innerHTML='';
  for(const [name,n] of [['All',D.jobs.length],...D.cats]){
    const b=document.createElement('button');b.className='chip'+(cat===name?' on':'');
    b.textContent=`${name} (${n})`;b.onclick=()=>{cat=name;chips();render()};el.appendChild(b);
  }
}
function fmtAge(j){
  if(j.age==null)return'';
  if(j.age===0)return'Today';if(j.age===1)return'Yesterday';return j.age+'d ago';
}
function render(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  let jobs=D.jobs.filter(j=>(cat==='All'||j.categories?.includes(cat)));
  if(q)jobs=jobs.filter(j=>((j.title||'')+(j.company||'')+' '+(j.summary||'')+' '+(j.description||'')).toLowerCase().includes(q));
  jobs.sort((a,b)=>{
    if(sortMode==='new')return (a.age??999)-(b.age??999);
    if(sortMode==='az')return (a.title||'').localeCompare(b.title||'');
    return (FIT_ORDER[a.fit]??1)-(FIT_ORDER[b.fit]??1)||(a.age??999)-(b.age??999);
  });
  document.getElementById('status').textContent=`${jobs.length} job${jobs.length===1?'':'s'}`;
  const el=document.getElementById('list');
  el.innerHTML=jobs.map(j=>{
    const isNew=(j.age??999)<=NEW_DAYS;
    const fitTag=`<span class="tag ${j.fit==='good'?'fitgood':'fitmaybe'}" ${j.fit_reason?`title="${esc(j.fit_reason)}"`:''}>${j.fit==='good'?'✓ Good fit':'~ Maybe'}</span>`;
    const meta=[j.location,j.job_type,j.is_remote?'Remote':'',j.salary].filter(Boolean)
      .map(t=>`<span class="tag">${esc(t)}</span>`).join('');
    const cats=(j.categories||[]).map(c=>`<span class="tag" style="background:#eef">${esc(c)}</span>`).join('');
    const dates=j.posted?`Posted ${esc(j.posted)}`:'';
    const desc=esc(j.summary?j.summary.replace(/\n/g,' '):(j.snippet||''));
    const full=j.desc_clean&&j.desc_clean.length>(j.snippet||'').length?esc(j.desc_clean).replace(/\n\n/g,'</p><p>'):'';
    const tog=full?`<button class="toggler" onclick="this.closest('.card').classList.toggle('open');this.textContent=this.textContent.includes('more')?'Show less':'Show more'">Show more</button>`:'';
    return `<div class="card">
      <h2>${esc(j.title)}</h2><div class="co">${esc(j.company)}</div>
      ${dates?`<div class="dates">${dates}</div>`:''}
      <div class="meta">${isNew?'<span class="tag new">NEW</span>':''}${fitTag}${cats}${meta}
        <span class="tag" style="background:#eee;color:#555">${fmtAge(j)}</span></div>
      <div class="seclabel" style="margin-top:10px">AI summary</div>
      <div class="desc"><p>${desc}</p></div>
      ${full?`<div class="more"><div class="seclabel">Full description — from Indeed</div><div class="desc"><p>${full}</p></div></div>`:''}${tog}
      <a class="btn" href="${esc(j.job_url)}" target="_blank">View &amp; Apply ↗</a>
    </div>`;
  }).join('')||'<div class="empty">No jobs match. Try clearing the search or filters.</div>';
}
function toggleSort(){sortMode=MODES[(MODES.indexOf(sortMode)+1)%MODES.length];
  document.getElementById('sortBtn').textContent='Sort: '+MODE_NAME[sortMode];render();}
document.getElementById('q').addEventListener('input',render);
document.getElementById('updated').textContent='Last updated '+new Date(D.updated).toLocaleString('en-GB')+' • '+D.jobs.length+' jobs'+(D.hidden?` (${D.hidden} hidden as poor matches)`:'');
chips();render();
window.toggleSort=toggleSort;
}"""

APP_CSS = SHELL.split("<style>")[1].split("</style>")[0]
APP_BODY = SHELL.split("<body>\n")[1].split("</body>")[0]

if __name__ == "__main__":
    payload, hidden = main()
    build_pages(payload)

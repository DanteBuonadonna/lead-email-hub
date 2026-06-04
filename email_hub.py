#!/usr/bin/env python3
"""
Lead Email Hub
==============
A small web app that lets a business owner send personalized cold emails to a
list of leads -- sent FROM their own Gmail / Google Workspace account, so every
email genuinely comes from them and replies come back to them.

It runs two ways from the SAME file:

  * LOCALLY  -- just run `python3 email_hub.py` and it opens in your browser.
  * HOSTED   -- deploy it to a free host (Replit / Render). When a PORT
               environment variable is present it binds to 0.0.0.0:$PORT so a
               client can visit it at a public URL. (See DEPLOY guide.)

The client connects their Gmail with a one-time "App Password" (no risky
password sharing, no Google app-verification process). The hub uses it only
in memory to send -- it is never written to disk or logged.

Pure Python standard library -- no pip installs, no requirements.txt.
"""

import csv
import io
import json
import os
import random
import smtplib
import ssl
import string
import threading
import time
import webbrowser
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# When hosted, the platform sets $PORT. Locally we default to 8765.
ENV_PORT = os.environ.get("PORT")
PORT = int(ENV_PORT) if ENV_PORT else 8765
HOSTED = ENV_PORT is not None
BIND = "0.0.0.0" if HOSTED else "127.0.0.1"

# Optional shared access code so a public URL can't be used by strangers.
# Set an ACCESS_CODE env var on your host to turn this on. Empty = no gate.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()

# ---------------------------------------------------------------------------
# Send jobs, keyed by a per-browser session id so two clients using the same
# hosted URL at once don't clobber each other's progress.
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()


def new_job():
    return {
        "running": False, "total": 0, "sent": 0, "failed": 0, "skipped": 0,
        "log": [], "done": False, "results_csv": "", "started": "",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_header(h):
    return h.strip().lower().replace(" ", "_").replace("-", "_")


def parse_csv(raw_bytes):
    """Parse uploaded CSV bytes -> (columns, rows). Rows are dicts keyed by
    normalized headers. Adds a derived first_name if a name column exists."""
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []

    headers = [normalize_header(h) for h in rows[0]]
    data = []
    for r in rows[1:]:
        if not any(cell.strip() for cell in r):
            continue
        record = {}
        for i, h in enumerate(headers):
            record[h] = r[i].strip() if i < len(r) else ""
        data.append(record)

    name_source = None
    for cand in ("first_name", "firstname", "name", "full_name", "contact", "contact_name"):
        if cand in headers:
            name_source = cand
            break
    if "first_name" not in headers and name_source:
        for rec in data:
            rec["first_name"] = (rec.get(name_source, "").split() or [""])[0]
        headers.append("first_name")

    seen, columns = set(), []
    for h in headers:
        if h and h not in seen:
            seen.add(h)
            columns.append(h)
    return columns, data


def code_ok(cfg):
    """True if access is allowed (gate off, or correct code supplied)."""
    if not ACCESS_CODE:
        return True
    return str(cfg.get("access_code", "")).strip() == ACCESS_CODE


def send_worker(sid, cfg):
    """Background thread: send each prepared message through the owner's Gmail."""
    emails = cfg["emails"]
    gmail = cfg["gmail"].strip()
    app_password = cfg["app_password"].replace(" ", "")
    from_name = cfg.get("from_name", "")
    delay = max(5, int(cfg.get("delay", 60)))
    cap = int(cfg.get("cap", 50))

    to_send = emails[:cap]
    skipped = emails[cap:]

    with JOBS_LOCK:
        job = JOBS.setdefault(sid, new_job())
        job.update({
            "running": True, "done": False, "total": len(emails),
            "sent": 0, "failed": 0, "skipped": len(skipped), "log": [],
            "results_csv": "", "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    def log(to, status, detail=""):
        with JOBS_LOCK:
            if status == "sent":
                JOBS[sid]["sent"] += 1
            elif status in ("failed", "fatal"):
                JOBS[sid]["failed"] += 1
            JOBS[sid]["log"].append({"to": to, "status": status, "detail": detail})

    try:
        context = ssl.create_default_context()
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30)
        server.login(gmail, app_password)
    except smtplib.SMTPAuthenticationError:
        log("(login)", "fatal", "Gmail rejected the login. Check the address and the 16-char App Password.")
        with JOBS_LOCK:
            JOBS[sid]["running"], JOBS[sid]["done"] = False, True
        return
    except Exception as e:
        log("(connect)", "fatal", f"Could not connect to Gmail: {e}")
        with JOBS_LOCK:
            JOBS[sid]["running"], JOBS[sid]["done"] = False, True
        return

    for i, item in enumerate(to_send):
        to_addr = (item.get("to") or "").strip()
        try:
            if not to_addr or "@" not in to_addr:
                raise ValueError("missing or invalid email address")
            msg = MIMEText(item["body"], "plain", "utf-8")
            msg["Subject"] = item["subject"]
            msg["From"] = formataddr((from_name, gmail))
            msg["To"] = to_addr
            server.sendmail(gmail, [to_addr], msg.as_string())
            log(to_addr, "sent")
        except Exception as e:
            log(to_addr or "(blank)", "failed", str(e))
        if i < len(to_send) - 1:
            time.sleep(delay + random.uniform(0, delay * 0.4))

    try:
        server.quit()
    except Exception:
        pass

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["email", "status", "detail"])
    with JOBS_LOCK:
        for row in JOBS[sid]["log"]:
            w.writerow([row["to"], row["status"], row["detail"]])
        for item in skipped:
            w.writerow([item.get("to", ""), "skipped_daily_cap", "not sent this run -- run again tomorrow"])
        JOBS[sid]["results_csv"] = out.getvalue()
        JOBS[sid]["running"], JOBS[sid]["done"] = False, True


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/index"):
            html = HTML.replace("__REQUIRE_CODE__", "true" if ACCESS_CODE else "false")
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/status":
            sid = (self._query().get("sid", [""])[0])
            with JOBS_LOCK:
                job = JOBS.get(sid, new_job())
                self._send(200, json.dumps(job))
        elif path == "/results.csv":
            sid = (self._query().get("sid", [""])[0])
            with JOBS_LOCK:
                csv_data = JOBS.get(sid, {}).get("results_csv", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=send_results.csv")
            self.end_headers()
            self.wfile.write(csv_data.encode("utf-8"))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        path = urlparse(self.path).path

        if path == "/upload":
            try:
                columns, rows = parse_csv(raw)
                self._send(200, json.dumps({"columns": columns, "rows": rows}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return

        # The rest expect JSON.
        try:
            cfg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, json.dumps({"error": f"bad request: {e}"}))
            return

        if not code_ok(cfg):
            self._send(403, json.dumps({"error": "Wrong or missing access code."}))
            return

        if path == "/send":
            sid = cfg.get("sid") or "default"
            with JOBS_LOCK:
                if JOBS.get(sid, {}).get("running"):
                    self._send(409, json.dumps({"error": "a send is already running"}))
                    return
            threading.Thread(target=send_worker, args=(sid, cfg), daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        elif path == "/test":
            try:
                gmail = cfg["gmail"].strip()
                pw = cfg["app_password"].replace(" ", "")
                context = ssl.create_default_context()
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30)
                s.login(gmail, pw)
                msg = MIMEText(cfg.get("body", "This is a test from your Lead Email Hub."), "plain", "utf-8")
                msg["Subject"] = cfg.get("subject", "Test email")
                msg["From"] = formataddr((cfg.get("from_name", ""), gmail))
                msg["To"] = gmail
                s.sendmail(gmail, [gmail], msg.as_string())
                s.quit()
                self._send(200, json.dumps({"ok": True}))
            except smtplib.SMTPAuthenticationError:
                self._send(200, json.dumps({"ok": False, "error": "Gmail rejected the login. Check the address and App Password."}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


# ---------------------------------------------------------------------------
# The single-page UI (HTML + CSS + JS, all inline).
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Email Hub</title>
<style>
  :root { --b:#2563eb; --bg:#f6f7f9; --line:#e3e6ea; --ok:#16a34a; --bad:#dc2626; --muted:#6b7280; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         margin:0; background:var(--bg); color:#111827; }
  header { background:#111827; color:#fff; padding:18px 24px; }
  header h1 { margin:0; font-size:20px; }
  header p { margin:4px 0 0; color:#9ca3af; font-size:13px; }
  .wrap { max-width:860px; margin:0 auto; padding:24px 20px 80px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:20px 22px; margin-bottom:18px; }
  .card h2 { margin:0 0 4px; font-size:16px; }
  .step { display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px;
          background:var(--b); color:#fff; border-radius:50%; font-size:13px; margin-right:8px; }
  .hint { color:var(--muted); font-size:13px; margin:4px 0 14px; }
  label { display:block; font-size:13px; font-weight:600; margin:12px 0 4px; }
  input[type=text], input[type=email], input[type=password], input[type=number], textarea, select {
    width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px; font-size:14px; font-family:inherit; }
  textarea { min-height:150px; resize:vertical; }
  .row { display:flex; gap:14px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:200px; }
  button { background:var(--b); color:#fff; border:0; border-radius:8px; padding:10px 18px;
           font-size:14px; font-weight:600; cursor:pointer; }
  button.secondary { background:#fff; color:#111827; border:1px solid var(--line); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .tags { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
  .tag { background:#eef2ff; color:#3730a3; border:1px solid #c7d2fe; border-radius:6px;
         padding:3px 8px; font-size:12px; cursor:pointer; font-family:monospace; }
  .pill { font-size:12px; color:var(--muted); }
  .preview { background:#fafafa; border:1px solid var(--line); border-radius:8px; padding:14px; white-space:pre-wrap;
             font-size:14px; min-height:120px; }
  .navrow { display:flex; align-items:center; gap:10px; margin:10px 0; flex-wrap:wrap; }
  .bar { height:10px; background:#e5e7eb; border-radius:6px; overflow:hidden; }
  .bar > span { display:block; height:100%; background:var(--ok); width:0%; transition:width .3s; }
  .logline { font-size:12px; padding:3px 0; border-bottom:1px solid #f0f0f0; }
  .logline.sent { color:var(--ok); }
  .logline.failed, .logline.fatal { color:var(--bad); }
  .warn { background:#fffbeb; border:1px solid #fde68a; color:#92400e; padding:10px 12px;
          border-radius:8px; font-size:13px; margin-top:10px; }
  a { color:var(--b); }
  .ok-msg { color:var(--ok); font-size:13px; }
  .bad-msg { color:var(--bad); font-size:13px; }
</style>
</head>
<body>
<header>
  <h1>Lead Email Hub</h1>
  <p>Send personalized emails to your leads &mdash; straight from your own Gmail.</p>
</header>
<div class="wrap">

  <div class="card" id="codeCard" style="display:none">
    <h2>Access code</h2>
    <p class="hint">Enter the access code you were given.</p>
    <input id="accessCode" type="text" placeholder="access code">
  </div>

  <div class="card">
    <h2><span class="step">1</span>Your Gmail</h2>
    <p class="hint">Emails are sent from this account, so replies come back to you.
      You need a Gmail <b>App Password</b> (not your normal password) &mdash; see the guide you were sent.
      It's used only to send and is never saved.</p>
    <div class="row">
      <div><label>Your name (shown as the sender)</label>
        <input id="fromName" type="text" placeholder="Jane Smith"></div>
      <div><label>Gmail address</label>
        <input id="gmail" type="email" placeholder="jane@yourbusiness.com"></div>
    </div>
    <label>App Password (16 characters)</label>
    <input id="appPw" type="password" placeholder="xxxx xxxx xxxx xxxx">
  </div>

  <div class="card">
    <h2><span class="step">2</span>Upload your leads</h2>
    <p class="hint">The CSV list you were sent. Pick which column holds the email address.</p>
    <input id="file" type="file" accept=".csv">
    <div id="uploadMsg" class="hint"></div>
    <label style="margin-top:14px">Email address column</label>
    <select id="emailCol"></select>
  </div>

  <div class="card">
    <h2><span class="step">3</span>Write your email</h2>
    <p class="hint">Click a tag to drop it in. Each tag is auto-replaced per lead &mdash; e.g.
      <span class="tag">{{first_name}}</span> becomes the lead's actual first name.</p>
    <div class="pill">Available tags:</div>
    <div id="tags" class="tags"></div>
    <label>Subject</label>
    <input id="subject" type="text" placeholder="Quick question, {{first_name}}">
    <label>Body</label>
    <textarea id="body" placeholder="Hi {{first_name}},

I saw {{company}} and wanted to reach out...

Best,
Jane"></textarea>
    <label>Signature / footer (business name + mailing address)</label>
    <textarea id="footer" style="min-height:70px" placeholder="Jane Smith — Acme Co.
123 Main St, Anytown, NJ 08000
Reply STOP to opt out."></textarea>
    <div class="warn">A real business name + mailing address and an opt-out line are legally required for
      cold email in the US (CAN-SPAM). The footer is added to every email.</div>
  </div>

  <div class="card">
    <h2><span class="step">4</span>Preview each email</h2>
    <p class="hint">Step through your leads to see exactly what each gets. You can hand-edit any one here.</p>
    <div class="navrow">
      <button class="secondary" onclick="prev()">&larr;</button>
      <span id="counter" class="pill">No leads loaded</span>
      <button class="secondary" onclick="next()">&rarr;</button>
      <span id="editedFlag" class="pill"></span>
    </div>
    <div class="pill">Subject preview:</div>
    <div id="subjPrev" class="preview" style="min-height:auto; margin-bottom:10px"></div>
    <div class="pill">Body preview (editable):</div>
    <textarea id="bodyPrev" oninput="markEdited()" style="min-height:200px"></textarea>
  </div>

  <div class="card">
    <h2><span class="step">5</span>Send</h2>
    <p class="hint">Send slowly to protect your sender reputation. New senders should start at
      <b>20&ndash;40 per day</b> and build up over a couple of weeks.</p>
    <div class="row">
      <div><label>Seconds between emails</label>
        <input id="delay" type="number" value="60" min="5"></div>
      <div><label>Max to send this run (daily cap)</label>
        <input id="cap" type="number" value="40" min="1"></div>
    </div>
    <div class="warn" id="capWarn" style="display:none"></div>
    <div class="navrow" style="margin-top:16px">
      <button class="secondary" onclick="testSend()">Send a test to myself</button>
      <button id="sendBtn" onclick="startSend()">Send to all leads</button>
    </div>
    <div id="testMsg" class="hint"></div>
  </div>

  <div class="card" id="progressCard" style="display:none">
    <h2>Sending…</h2>
    <div class="bar"><span id="barFill"></span></div>
    <p id="progressText" class="hint" style="margin-top:10px"></p>
    <div id="logBox" style="max-height:260px; overflow:auto; margin-top:10px"></div>
    <a id="resultsLink" href="#" style="display:none">Download results CSV</a>
  </div>

</div>

<script>
const REQUIRE_CODE = __REQUIRE_CODE__;
const SID = Math.random().toString(36).slice(2) + Date.now().toString(36);
let LEADS = [], COLUMNS = [], idx = 0, overrides = {};
function el(id){ return document.getElementById(id); }
if (REQUIRE_CODE) el('codeCard').style.display = 'block';

el('file').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const buf = await f.arrayBuffer();
  const res = await fetch('/upload', { method:'POST', body: buf });
  const data = await res.json();
  if (data.error){ el('uploadMsg').innerHTML = '<span class="bad-msg">'+data.error+'</span>'; return; }
  LEADS = data.rows; COLUMNS = data.columns;
  el('uploadMsg').innerHTML = '<span class="ok-msg">Loaded '+LEADS.length+' leads — columns: '+COLUMNS.join(', ')+'</span>';
  const sel = el('emailCol'); sel.innerHTML = '';
  COLUMNS.forEach(c => { const o=document.createElement('option'); o.value=c; o.textContent=c;
    if (c==='email'||c.includes('email')) o.selected=true; sel.appendChild(o); });
  const tags = el('tags'); tags.innerHTML='';
  COLUMNS.forEach(c => { const t=document.createElement('span'); t.className='tag'; t.textContent='{{'+c+'}}';
    t.onclick=()=>insertTag('{{'+c+'}}'); tags.appendChild(t); });
  idx=0; overrides={}; renderPreview();
});

function insertTag(tag){ const b=el('body'); const s=b.selectionStart||b.value.length;
  b.value=b.value.slice(0,s)+tag+b.value.slice(s); b.focus(); renderPreview(); }

function merge(t, lead){ return (t||'').replace(/\{\{\s*([\w]+)\s*\}\}/g,(m,k)=>{
  const v=lead[k.toLowerCase()]; return (v===undefined||v===null)?'':v; }); }
function fullBody(i){ if (overrides[i]!==undefined) return overrides[i];
  let body=merge(el('body').value, LEADS[i]||{}); const foot=el('footer').value.trim();
  if (foot) body+="\n\n"+foot; return body; }

['subject','body','footer'].forEach(id=>el(id).addEventListener('input', renderPreview));

function renderPreview(){
  if (!LEADS.length){ el('counter').textContent='No leads loaded'; el('subjPrev').textContent=''; el('bodyPrev').value=''; return; }
  if (idx<0) idx=0; if (idx>=LEADS.length) idx=LEADS.length-1;
  const lead=LEADS[idx];
  el('counter').textContent='Lead '+(idx+1)+' of '+LEADS.length+'  ('+(lead[el('emailCol').value]||'no email')+')';
  el('subjPrev').textContent=merge(el('subject').value, lead);
  el('bodyPrev').value=fullBody(idx);
  el('editedFlag').textContent= overrides[idx]!==undefined ? '✏️ hand-edited' : '';
}
function prev(){ if(idx>0){idx--; renderPreview();} }
function next(){ if(idx<LEADS.length-1){idx++; renderPreview();} }
function markEdited(){ overrides[idx]=el('bodyPrev').value; el('editedFlag').textContent='✏️ hand-edited'; }

el('cap').addEventListener('input', checkCap);
function checkCap(){ const cap=parseInt(el('cap').value||'0'); const w=el('capWarn');
  if (cap>400){ w.style.display='block'; w.textContent='Gmail free accounts cap around 500 recipients/day and Workspace around 2,000. Sending this many at once risks getting your account flagged. Spread it across several days.'; }
  else if (cap>50){ w.style.display='block'; w.textContent='Heads up: if this inbox is new to cold outreach, '+cap+' in one day is aggressive. Consider ramping up gradually.'; }
  else { w.style.display='none'; } }

function creds(extra){ return Object.assign({
  sid: SID, access_code: el('accessCode') ? el('accessCode').value.trim() : '',
  from_name: el('fromName').value.trim(), gmail: el('gmail').value.trim(),
  app_password: el('appPw').value.trim() }, extra||{}); }

async function testSend(){
  if (!el('gmail').value || !el('appPw').value){ el('testMsg').innerHTML='<span class="bad-msg">Enter your Gmail and App Password first.</span>'; return; }
  el('testMsg').textContent='Sending test…';
  const res=await fetch('/test',{method:'POST',body:JSON.stringify(creds({
    subject: merge(el('subject').value, LEADS[idx]||{}), body: el('bodyPrev').value||'Test from your Lead Email Hub.' }))});
  const d=await res.json();
  el('testMsg').innerHTML = d.ok ? '<span class="ok-msg">Test sent — check your inbox.</span>'
                                 : '<span class="bad-msg">'+(d.error||'failed')+'</span>';
}

async function startSend(){
  if (!LEADS.length){ alert('Upload your leads first.'); return; }
  if (!el('gmail').value || !el('appPw').value){ alert('Enter your Gmail and App Password.'); return; }
  const emailCol=el('emailCol').value;
  const emails=LEADS.map((lead,i)=>({ to: lead[emailCol]||'',
    subject: merge(el('subject').value, lead), body: fullBody(i) }));
  const willSend=Math.min(emails.length, parseInt(el('cap').value));
  if (!confirm('Send to up to '+willSend+' leads now?')) return;
  el('sendBtn').disabled=true; el('progressCard').style.display='block'; el('resultsLink').style.display='none';
  await fetch('/send',{method:'POST',body:JSON.stringify(creds({
    delay: parseInt(el('delay').value||'60'), cap: parseInt(el('cap').value||'40'), emails }))});
  poll();
}

async function poll(){
  const res=await fetch('/status?sid='+SID); const s=await res.json();
  const handled=s.sent+s.failed;
  const target=Math.min(s.total, parseInt(el('cap').value||'40'))||1;
  el('barFill').style.width=Math.round(100*handled/target)+'%';
  el('progressText').textContent='Sent '+s.sent+'  •  Failed '+s.failed+'  •  Skipped (over cap) '+s.skipped+'  of '+s.total+' total';
  const box=el('logBox'); box.innerHTML='';
  s.log.slice(-40).forEach(l=>{ const d=document.createElement('div'); d.className='logline '+l.status;
    d.textContent=(l.status==='sent'?'✓ ':'✕ ')+l.to+(l.detail?(' — '+l.detail):''); box.appendChild(d); });
  if (s.done){ el('sendBtn').disabled=false; el('progressText').textContent+='  —  Finished.';
    const a=el('resultsLink'); a.href='/results.csv?sid='+SID; a.style.display='inline-block'; }
  else { setTimeout(poll, 1500); }
}
</script>
</body>
</html>
"""


def main():
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    if HOSTED:
        print(f"Lead Email Hub listening on {BIND}:{PORT} (hosted mode)"
              + ("  [access code ON]" if ACCESS_CODE else ""))
    else:
        url = f"http://127.0.0.1:{PORT}"
        print("=" * 60)
        print("  Lead Email Hub is running.")
        print(f"  Open this in your browser:  {url}")
        print("  (Press Ctrl+C in this window to stop.)")
        print("=" * 60)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

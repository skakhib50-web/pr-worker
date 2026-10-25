#!/usr/bin/env python3
"""
Password Reset Worker  (browser-automation half of the Password Reset Portal)
==========================================================================
The Google Apps Script web app (apps-script/) is the portal: login, users, instances, L2 credentials,
queue, live run console and activity log.  Apps Script cannot drive a browser, so THIS worker does the
clicking.  It polls the web app for queued jobs, runs them with Playwright + Microsoft Edge exactly like
the original portal did, and reports progress / results back.

  1. L2-Sites login reset : sign in ONCE with the stored L2 login, Menu Search > "Password Reset"
        Unlock = [Locked Users] radio -> Username filter -> "Unlock"
        Reset  = [All Users]    radio -> Username filter -> "Reset Password"
  2. Report login reset   : log in as each user, profile > Change Password > Existing/New/Confirm > Save > Logout
        (expired-password screen is filled directly)

Settings (environment variables or worker.env next to this file):
  GAS_URL        Web-app URL ending in /exec            (required)
  WORKER_TOKEN   Shared secret printed by setup()       (required)
  DEMO_MODE=1    simulate (no browser, no IVY access)   HEADLESS=0 show the browser   EDGE_PATH=...
  IGNORE_SSL=1   MAX_BROWSERS=1   SAVE_SHOTS=1   POLL_SECONDS=2
"""
import json, os, re, sys, threading, time, traceback, urllib.error, urllib.request, uuid
from datetime import datetime
from pathlib import Path

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _load_env():
    f = BASE / "worker.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env()
GAS_URL = os.environ.get("GAS_URL", "").strip()
TOKEN = os.environ.get("WORKER_TOKEN", "").strip()
DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"
HEADLESS = os.environ.get("HEADLESS", "1") == "1"
EDGE_PATH = os.environ.get("EDGE_PATH", "")
IGNORE_SSL = os.environ.get("IGNORE_SSL", "0") == "1"
SAVE_SHOTS = os.environ.get("SAVE_SHOTS", "1") == "1"
MAX_BROWSERS = int(os.environ.get("MAX_BROWSERS", "1"))
POLL = float(os.environ.get("POLL_SECONDS", "2"))
IDLE_EXIT = float(os.environ.get("IDLE_EXIT_SECONDS", "0"))   # >0: exit when no job for this long (used by the GitHub Actions runner)
WORKER_ID = os.environ.get("WORKER_ID") or uuid.uuid4().hex[:8]
CFG_PATH = BASE / "selectors.json"
SHOT_DIR = BASE / "screenshots"
CFG_VERSION = 5

DEFAULT_CFG = json.loads(r"""{
  "_version": 5,
  "nav_timeout_ms": 60000,
  "step_timeout_ms": 20000,
  "outcome_timeout_s": 20,
  "login_user": "#UserName, input[name='UserName'], input[id*='user' i], input[placeholder*='user' i], input[type='text']",
  "login_pass": "#Password, input[type='password']",
  "login_btn": "#Login, button[type='submit'], button:has-text('Login'), button:has-text('Log in'), button:has-text('Sign in'), input[type='submit']",
  "login_error_regex": "invalid|incorrect|wrong|locked|disabled|failed|not authori[sz]ed|expired user",
  "menu_search": "#MenuSearchBox, input[placeholder*='search' i], input[type='search'], input[aria-label*='search' i]",
  "form_marker_regex": "confirm password",
  "reset_old": "",
  "reset_new": "",
  "reset_confirm": "",
  "save_btn": "button:has-text('Save'), input[value='Save'], input[type='button'][value*='Save' i], a:has-text('Save')",
  "success_regex": "success|has been (changed|updated|reset)|password (changed|updated|reset)|reset done|unlocked",
  "failure_regex": "invalid|incorrect|failed|not match|cannot|can't|already used|previous password|policy|error|must contain|at least",
  "confirm_btn": "button:has-text('Yes'), button:has-text('OK'), button:has-text('Confirm')",
  "logout_btn": "text=/log ?out|sign ?out/i",
  "l2_menu": "Password Reset",
  "l2_radio_locked": "Locked Users",
  "l2_radio_all": "All Users",
  "l2_username_header": "Username",
  "l2_unlock_link": "Unlock",
  "l2_reset_link": "Reset Password"
}""")

# ---- JavaScript evaluated inside the target application (recovered verbatim from the original build) ----
JS_WATCH = r"""([okSrc, badSrc]) => {
  try { if (window.__wobs) window.__wobs.disconnect(); } catch (e) {}
  window.__wmsgs = [];
  const ok = new RegExp(okSrc, 'i'), bad = new RegExp(badSrc, 'i');
  const push = (t, n) => {
    if (n && /^(SCRIPT|STYLE)$/.test(n.nodeName || '')) return;
    t = (t || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 200 && (ok.test(t) || bad.test(t))) window.__wmsgs.push(t);
  };
  const obs = new MutationObserver(ms => { for (const m of ms) {
    if (m.type === 'childList') m.addedNodes.forEach(n => push(n.textContent, n));
    else if (m.type === 'characterData') push(m.target.parentElement ? m.target.parentElement.textContent : m.target.textContent, m.target);
    else if (m.type === 'attributes') push(m.target.textContent, m.target);
  } });
  obs.observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true});
  window.__wobs = obs;
  return true; }"""

JS_FORM_INPUTS = r"""() => { const out=[]; document.querySelectorAll('input,textarea').forEach((el,i)=>{
  const r=el.getBoundingClientRect(); const cs=getComputedStyle(el);
  if(r.width<5||r.height<5||cs.visibility==='hidden'||cs.display==='none') return;
  if(el.readOnly||el.disabled) return;
  const t=(el.type||'text').toLowerCase();
  if(['hidden','checkbox','radio','button','submit','search','file','image','reset'].indexOf(t)>=0) return;
  if(el.id==='MenuSearchBox') return;
  let label='';
  if(el.id){ const l=document.querySelector('label[for="'+el.id+'"]'); if(l) label=l.textContent||''; }
  if(!label){ const p=el.closest('.input-field,.form-group,.form-element'); if(p){ const l=p.querySelector('label'); if(l) label=l.textContent||''; } }
  out.push({i:i,type:t,id:el.id||'',name:el.name||'',ph:el.placeholder||'',label:(label||'').trim().slice(0,60),x:r.left,y:r.top});
}); return out; }"""

JS_OPEN_CHANGE_PW = r"""() => {
  let a = document.querySelector("#profile-dropdown a[title='Change Password']");
  if(!a){ a = Array.from(document.querySelectorAll('#profile-dropdown a, .dropdown-content a'))
              .find(x => /change\s*password/i.test(x.textContent||'')); }
  if(a){ a.click(); return true; }
  return false; }"""

JS_LOGOUT = r"""() => {
  const a = document.querySelector("#profile-dropdown a[title='Logout']");
  if(a){ a.click(); return true; }
  if(typeof fnLogout === 'function'){ fnLogout(); return true; }
  return false; }"""

JS_TAG_FILTER = r"""(hdr) => {
  document.querySelectorAll('[data-l2f]').forEach(e => e.removeAttribute('data-l2f'));
  const want = hdr.trim().toLowerCase();
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                     return r.width > 4 && r.height > 4 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const cells = Array.from(document.querySelectorAll('th,td,div,span,label,a'))
     .filter(e => e.children.length <= 1 && (e.textContent || '').trim().toLowerCase() === want && vis(e));
  if (!cells.length) return 0;
  cells.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);   // the header = highest match
  const hc = cells[0].closest('th,td') || cells[0];
  const h = hc.getBoundingClientRect();
  const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
     const t = (el.type || 'text').toLowerCase();
     if (['hidden','checkbox','radio','button','submit','file','image','reset'].indexOf(t) >= 0) return false;
     if (el.readOnly || el.disabled || el.id === 'MenuSearchBox' || !vis(el)) return false;
     const r = el.getBoundingClientRect(); const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
     return cx >= h.left - 3 && cx <= h.right + 3 && cy > h.top && cy < h.bottom + 100;
  });
  if (!inputs.length) return 0;
  inputs.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  inputs[0].setAttribute('data-l2f', '1');
  return 1; }"""

JS_TAG_ACTION = r"""([uid, label]) => {
  document.querySelectorAll('[data-l2a]').forEach(e => e.removeAttribute('data-l2a'));
  const u = uid.trim().toLowerCase(), lab = label.trim().toLowerCase();
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                     return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const txt = e => ((e.tagName === 'INPUT' ? e.value : e.textContent) || '').trim().toLowerCase();
  const cells = Array.from(document.querySelectorAll('td,div,span,a,p,li,label'))
      .filter(e => e.tagName !== 'INPUT' && txt(e) === u && vis(e));
  let n = cells.length;
  for (const cell of cells) {
    let anc = cell;
    for (let i = 0; i < 8 && anc.parentElement; i++) {
      anc = anc.parentElement;
      let acts = Array.from(anc.querySelectorAll('a,button,span,div,td,input,li'))
          .filter(x => txt(x) === lab && x.children.length <= 1 && vis(x));
      acts = acts.filter(x => !acts.some(y => y !== x && x.contains(y)));     // keep the innermost (td > a counts once)
      if (acts.length === 1) {                     // exactly one -> this ancestor is this user's row
        acts[0].setAttribute('data-l2a', '1');
        return {n: n, tagged: true};
      }
      if (acts.length > 1) break;                  // climbed too far (several rows) -> refuse, never click a wrong row
    }
  }
  return {n: n, tagged: false}; }"""

JS_RADIO = r"""([label, doClick]) => {
  const want = label.trim().toLowerCase();
  const el = Array.from(document.querySelectorAll('label,span,div,p,td,li'))
      .find(e => (e.textContent || '').trim().toLowerCase() === want && e.getBoundingClientRect().width > 2);
  if (!el) return null;
  let inp = el.querySelector('input[type=radio]');
  if (!inp && el.getAttribute('for')) inp = document.getElementById(el.getAttribute('for'));
  if (!inp && el.previousElementSibling && el.previousElementSibling.matches('input[type=radio]')) inp = el.previousElementSibling;
  if (!inp && el.parentElement) {
    const rs = el.parentElement.querySelectorAll('input[type=radio]');
    if (rs.length === 1) inp = rs[0];
  }
  if (!inp) return null;
  if (doClick) inp.click();
  return inp.checked; }"""

JS_GRID_DIAG = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                     return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const f = document.querySelector('[data-l2f]');
  const rows = Array.from(document.querySelectorAll('tbody tr')).filter(vis)
      .map(r => (r.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)).filter(t => t);
  return {filter: f ? f.value : null, count: rows.length, rows: rows.slice(0, 3)}; }"""


# =====================================================================================================
# Settings / helpers
# =====================================================================================================
def cfg():
    d = dict(DEFAULT_CFG)
    if CFG_PATH.exists():
        try:
            d.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print("WARNING: selectors.json unreadable, using defaults:", e)
    return d


def ensure_cfg_file():
    """Create selectors.json (or refresh it when the shipped version changed; the old one is kept)."""
    cur = None
    try:
        if CFG_PATH.exists():
            cur = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cur = None
    if cur is None or cur.get("_version") != CFG_VERSION:
        try:
            if CFG_PATH.exists():
                CFG_PATH.replace(BASE / "selectors.old.json")
        except Exception:
            pass
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, indent=2), encoding="utf-8")


def log(msg):
    print(f"   [{datetime.now():%H:%M:%S}] {msg}", flush=True)


def redact(text, *secrets):
    text = str(text or "")
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "****")
    return text


def res(ok, messages=None, detail=""):
    return {"ok": ok, "messages": messages or [], "detail": detail}


class StepError(Exception):
    pass


RUN_SEM = threading.Semaphore(MAX_BROWSERS)
DIALOGS, STEPS, PROGRESS = {}, {}, {}


# =====================================================================================================
# Live status reporting to the Apps Script web app
# =====================================================================================================
class Api:
    """Tiny JSON-over-HTTPS client for the Apps Script web app (Apps Script answers POST with a 302 that urllib follows)."""

    def call(self, action, **payload):
        body = json.dumps({"token": TOKEN, "action": action, "worker": WORKER_ID, **payload}).encode("utf-8")
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(GAS_URL, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read().decode("utf-8", "replace")
                if raw.lstrip().startswith("<"):
                    raise RuntimeError("the web app returned a web page instead of data - deploy it with 'Who has access: Anyone' and use the URL ending in /exec")
                out = json.loads(raw)
                if isinstance(out, dict) and out.get("error"):
                    raise RuntimeError(out["error"])
                return out
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last = e
                time.sleep(1 + attempt * 2)
        raise RuntimeError(f"web app unreachable: {last}")


API = Api()


class Tracker:
    """Live status of one job; a background thread pushes changes to the web app about once a second."""

    def __init__(self, job_id):
        self.id, self.stage_code, self.current, self.idx = job_id, "login", "", 0
        self.events, self._sent, self._last, self._dirty = [], 0, None, True
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def ev(self, text, kind="info"):
        with self._lock:
            self.events.append({"t": datetime.now().strftime("%H:%M:%S"), "m": text, "k": kind})
            self._dirty = True

    def stage(self, code):
        self.stage_code = code
        self._dirty = True
        key = (code, self.current)
        if key == self._last:
            return
        self._last = key
        cur = self.current
        txt = {"login": f"Logging in as {cur}" if cur else "Logging in to the portal",
               "unlock": f"Unlocking {cur}", "reset": f"Resetting password for {cur}", "logout": "Logging out"}.get(code)
        if txt:
            self.ev(txt)

    def user(self, uid, idx):
        self.current, self.idx, self._dirty = uid, idx, True

    def push(self):
        with self._lock:
            new = self.events[self._sent:]
            self._sent = len(self.events)
            self._dirty = False
        try:
            API.call("progress", id=self.id, stage=self.stage_code, current=self.current, idx=self.idx, events=new)
        except Exception as e:
            log(f"progress push failed: {e}")
            with self._lock:
                self._sent -= len(new)
                self._dirty = True

    def _pump(self):
        while not self._stop.wait(1.0):
            if self._dirty:
                self.push()

    def close(self):
        self._stop.set()
        self.push()


def stage(page, code):
    cb = PROGRESS.get(page)
    if cb:
        try:
            cb(code)
        except Exception:
            pass


def step(page, name):
    STEPS[page] = name
    log("step: " + name)


# =====================================================================================================
# Browser helpers
# =====================================================================================================
def vis(scope, selector):
    return scope.locator(selector + " >> visible=true").first


def click_if(scope, selector, timeout=3000):
    loc = vis(scope, selector)
    try:
        loc.wait_for(state="visible", timeout=timeout)
        loc.click(timeout=timeout)
        return True
    except Exception:
        try:
            loc.evaluate("e => e.click()")
            return True
        except Exception:
            return False


def safe_fill(loc, val, timeout=6000):
    try:
        loc.fill(val, timeout=timeout)
    except Exception:
        loc.evaluate("""(e, v) => { e.removeAttribute('readonly'); e.focus();
            const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; set.call(e, v);
            e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }""", val)


def launch_browser(p):
    kw = {"headless": HEADLESS}
    try:
        if EDGE_PATH:
            return p.chromium.launch(executable_path=EDGE_PATH, **kw)
        return p.chromium.launch(channel="msedge", **kw)
    except Exception:
        return p.chromium.launch(**kw)  # falls back to Playwright's bundled Chromium / Chrome


def new_page(browser, c, on_stage=None):
    ctx = browser.new_context(ignore_https_errors=IGNORE_SSL, viewport={"width": 1366, "height": 800})
    page = ctx.new_page()
    page.set_default_timeout(c["step_timeout_ms"])
    page.set_default_navigation_timeout(c["nav_timeout_ms"])
    msgs = []
    DIALOGS[page] = msgs
    if on_stage:
        PROGRESS[page] = on_stage

    def _dialog(d):
        try:
            msgs.append(d.message or "")
            d.accept()
        except Exception:
            pass

    page.on("dialog", _dialog)
    page.on("close", lambda *_: (DIALOGS.pop(page, None), STEPS.pop(page, None), PROGRESS.pop(page, None)))
    return ctx, page


def ensure_ready(page, c, wait_s=5):
    try:
        page.wait_for_function("document.body && (document.body.classList.contains('loaded') || !document.getElementById('loader-wrapper'))",
                               timeout=wait_s * 1000)
    except Exception:
        pass
    try:
        page.add_style_tag(content="#loader-wrapper{display:none !important;pointer-events:none !important}")
    except Exception:
        pass


def home_visible(page, c):
    try:
        return vis(page, "#prof-anchor, #MenuSearchBox").is_visible() or vis(page, c["menu_search"]).is_visible()
    except Exception:
        return False


def pw_visible(page):
    try:
        return page.locator("input[type='password'] >> visible=true").count() > 0
    except Exception:
        return False


def login_page_visible(page):
    try:
        return bool(re.search("/login", page.url or "", re.I)) or pw_visible(page)
    except Exception:
        return False


def visible_text_el(page, regex):
    for fr in list(page.frames):
        try:
            loc = fr.get_by_text(re.compile(regex, re.I))
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def text_visible(page, regex):
    el = visible_text_el(page, regex)
    if el is None:
        return None
    try:
        return (el.inner_text() or "").strip()[:160] or "(message)"
    except Exception:
        return "(message)"


def matching_texts(page, regex):
    out = set()
    for fr in list(page.frames):
        try:
            loc = fr.get_by_text(re.compile(regex, re.I))
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                if el.is_visible():
                    out.add((el.inner_text() or "").strip()[:160])
        except Exception:
            continue
    return out


def watch_start(page, c):
    for fr in list(page.frames):
        try:
            fr.evaluate(JS_WATCH, [c["success_regex"], c["failure_regex"]])
        except Exception:
            pass


def watched_msgs(page):
    out = []
    for fr in list(page.frames):
        try:
            out += fr.evaluate("() => window.__wmsgs || []")
        except Exception:
            pass
    return out


def wait_outcome(page, c, base_ok=None, base_bad=None, dstart=0, allow_login=False, timeout=None, confirm=False):
    """Wait for the application to answer: ('ok'|'bad'|'login'|'none', message)."""
    base_ok, base_bad = base_ok or set(), base_bad or set()
    ok_re, bad_re = re.compile(c["success_regex"], re.I), re.compile(c["failure_regex"], re.I)
    t0 = time.time()
    end = t0 + (timeout or c["outcome_timeout_s"])
    confirms = 0
    while time.time() < end:
        try:
            for m in DIALOGS.get(page, [])[dstart:]:
                if ok_re.search(m):
                    return ("ok", m[:160])
                if bad_re.search(m):
                    return ("bad", m[:160])
            for m in watched_msgs(page):
                if bad_re.search(m) and not ok_re.search(m):
                    return ("bad", m[:160])
                if ok_re.search(m):
                    return ("ok", m[:160])
            new_ok = matching_texts(page, c["success_regex"]) - base_ok
            if new_ok:
                return ("ok", sorted(new_ok)[0])
            new_bad = matching_texts(page, c["failure_regex"]) - base_bad
            if new_bad:
                return ("bad", sorted(new_bad)[0])
            if allow_login and login_page_visible(page):
                return ("login", "")
            if confirm and confirms < 3 and time.time() - t0 > 0.7:
                for fr in list(page.frames):
                    if click_if(fr, c["confirm_btn"], 250):
                        confirms += 1
                        time.sleep(0.5)
                        break
        except Exception:
            pass
        time.sleep(0.4)
    return ("none", "")


def find_form_frame(page, c):
    """Locate the Change-Password form (main page or an iframe): (frame, input_list) or None."""
    for fr in list(page.frames):
        try:
            items = fr.evaluate(JS_FORM_INPUTS)
            npw = sum(1 for it in items if it["type"] == "password")
            if npw >= 3:
                return fr, items
            if npw >= 2 and fr.get_by_text(re.compile(c["form_marker_regex"], re.I)).count() > 0:
                return fr, items
        except Exception:
            continue
    return None


def login_form_untouched(page, c):
    try:
        return bool(page.evaluate("() => { const p = document.querySelector('input[type=password]'); return !!p && p.value.length > 0 && p.offsetParent !== null; }"))
    except Exception:
        return False


def submit_login(page, c, attempt):
    if attempt == 0:
        if not click_if(page, c["login_btn"], 6000):
            vis(page, c["login_pass"]).press("Enter")
    elif attempt == 1:
        vis(page, c["login_pass"]).press("Enter")
    elif attempt == 2:
        page.evaluate("() => { const b = document.querySelector('#Login, button[type=submit], input[type=submit]'); if (b) b.click(); }")
    else:
        page.evaluate("() => { const f = document.querySelector('form'); if (f) { if (f.requestSubmit) f.requestSubmit(); else f.submit(); } }")


def app_login(page, c, url, uid, pw):
    """Sign in. Returns 'home' | 'reset_screen' (expired password) | 'login_error' | 'unknown'."""
    step(page, "open login URL")
    page.goto(url, wait_until="domcontentloaded")
    step(page, "wait for login page to load")
    page.wait_for_load_state("load", timeout=c["nav_timeout_ms"])
    ensure_ready(page, c, wait_s=15)
    step(page, "find User Name box (login_user)")
    u = vis(page, c["login_user"])
    u.wait_for(state="visible")
    time.sleep(0.4)
    step(page, "type User Name / Password")
    safe_fill(u, uid)
    safe_fill(vis(page, c["login_pass"]), pw)
    step(page, "click Login and wait for the next screen")
    attempt = 0
    submit_login(page, c, attempt)
    last = time.time()
    end = time.time() + c["outcome_timeout_s"] + 25
    while time.time() < end:
        time.sleep(0.5)
        try:
            if find_form_frame(page, c):
                return "reset_screen"
            if home_visible(page, c):
                return "home"
            if text_visible(page, c["login_error_regex"]):
                return "login_error"
            if attempt < 3 and time.time() - last > 6 and login_form_untouched(page, c):
                attempt += 1
                last = time.time()
                log(f"{uid}: login form not submitted yet - retry {attempt}")
                ensure_ready(page, c, wait_s=2)
                submit_login(page, c, attempt)
        except Exception:
            continue
    return "unknown"


def _type_slow(loc, text):
    try:
        loc.press_sequentially(text, delay=40)
    except AttributeError:
        loc.type(text, delay=40)


def open_menu(page, c, text):
    """Menu Search box -> type the menu name -> click the autocomplete entry."""
    step(page, "menu search: " + text)
    ensure_ready(page, c)
    s = page.locator("#MenuSearchBox")
    if s.count() == 0:
        s = vis(page, c["menu_search"])
    s = s.first
    s.wait_for(state="visible")
    try:
        s.click(timeout=4000)
    except Exception:
        s.focus()
    s.fill("")
    _type_slow(s, text)
    time.sleep(1.0)
    menu_rx = r"Menu\s*:\s*" + re.escape(text)
    end = time.time() + 10
    while time.time() < end:
        items = page.locator("ul.ui-autocomplete li >> visible=true")
        if items.count() > 0:
            match = items.filter(has_text=re.compile(re.escape(text), re.I))
            tgt = match.first if match.count() > 0 else items.first
            try:
                tgt.locator("a").last.click(timeout=3000)
            except Exception:
                tgt.evaluate("e => e.click()")
            return
        el = visible_text_el(page, menu_rx)
        if el is not None:
            try:
                el.click(timeout=3000)
            except Exception:
                el.evaluate("e => e.click()")
            return
        time.sleep(0.3)
    s.press("Enter")


def open_change_password(page, c, uid=""):
    step(page, "wait for dashboard loader")
    ensure_ready(page, c)
    step(page, "find profile drop-down (#profile-dropdown)")
    page.wait_for_selector("#profile-dropdown", state="attached", timeout=20000)
    if not page.evaluate(JS_OPEN_CHANGE_PW):
        raise StepError("This user has no 'Change Password' option in the profile drop-down")
    step(page, "wait for the Change Password form")
    end = time.time() + 40
    while time.time() < end:
        found = find_form_frame(page, c)
        if found:
            return found
        time.sleep(0.6)
    raise StepError("Change Password screen did not open (form not found in the page or its iframe)")


def classify_fields(items):
    """Identify Existing / New / Confirm password inputs from id, name, placeholder, label (fallback: position)."""
    def txt(it):
        return " ".join([it.get("id", ""), it.get("name", ""), it.get("ph", ""), it.get("label", "")]).lower()

    roles = {}
    for it in items:
        t = txt(it)
        if re.search(r"confirm|re-?type|repeat|verify", t):
            role = "confirm"
        elif re.search(r"exist|\bold|current|previous", t):
            role = "old"
        elif re.search(r"\bnew|new", t):
            role = "new"
        else:
            continue
        roles.setdefault(role, []).append(it)
    if all(len(roles.get(r, [])) == 1 for r in ("old", "new", "confirm")):
        return roles["old"][0], roles["new"][0], roles["confirm"][0]
    pw = sorted((it for it in items if it["type"] == "password"), key=lambda t: (round(t["y"] / 25), t["x"]))
    if len(pw) >= 3:
        pw = pw[-3:]
        return pw[0], pw[1], pw[2]
    if len(pw) == 2:
        return None, pw[0], pw[1]
    raise StepError(f"Change Password form not recognised ({len(pw)} password field(s) found)")


def fill_reset_form(page, c, frame, items, old, new):
    if c["reset_old"] and c["reset_new"]:  # explicit selectors in selectors.json win
        safe_fill(vis(frame, c["reset_old"]), old)
        safe_fill(vis(frame, c["reset_new"]), new)
        safe_fill(vis(frame, c["reset_confirm"] or c["reset_new"]), new)
        return
    o, n, cf = classify_fields(items)
    log("fields -> existing: {} | new: {} | confirm: {}".format(
        *[(f"{x.get('id') or x.get('name') or '?'}/{x.get('label') or '-'}" if x else "none") for x in (o, n, cf)]))
    loc = frame.locator("input, textarea")
    for it, val in ((o, old), (n, new), (cf, new)):
        if it is None:
            continue
        try:
            el = loc.nth(it["i"])
            el.focus(timeout=3000)
            safe_fill(el, val)
            for ev in ("input", "keyup", "change"):
                el.dispatch_event(ev)
            el.evaluate("e => e.blur()")
        except Exception:
            pass


def click_save(page, frame, c):
    for fr in [frame] + [f for f in page.frames if f != frame]:
        try:
            loc = fr.locator(c["save_btn"] + " >> visible=true")
            if loc.count() == 0:
                continue
            b = loc.first
            try:
                b.click(timeout=4000)
            except Exception:
                b.evaluate("e => e.click()")
            return True
        except Exception:
            continue
    return False


def try_logout(page, c, uid=""):
    try:
        clicked = page.evaluate(JS_LOGOUT)
        if not clicked:
            click_if(page, c["logout_btn"], 1500)
        page.wait_for_url(re.compile("login", re.I), timeout=8000)
    except Exception:
        time.sleep(1.5)


def save_shot(page, tag):
    if not SAVE_SHOTS:
        return
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}_" + re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
        page.screenshot(path=str(SHOT_DIR / (stamp + ".png")))
        (SHOT_DIR / (stamp + ".html")).write_text(page.content(), encoding="utf-8")
        for n, fr in enumerate([f for f in page.frames if f != page.main_frame][:3], 1):
            (SHOT_DIR / f"{stamp}_frame{n}.html").write_text(fr.content(), encoding="utf-8")
    except Exception:
        pass


def _guard(fn, page, tag, *sec):
    """Run one unit of work; any failure becomes a clean, redacted result (and a screenshot for diagnosis)."""
    try:
        r = fn()
        if not r["ok"] and page is not None:
            save_shot(page, tag)
        return r
    except StepError as e:
        r = res(False, ["Failed"], redact(e, *sec))
    except Exception as e:
        name, where = type(e).__name__, STEPS.get(page, "?")
        lines = [x.strip(" -") for x in str(e).split("\n") if x.strip() and x.strip() != "Call log:"]
        first = redact(" | ".join(lines[:3]), *sec)[:230]
        reason = f"Timed out at step '{where}': {first}" if "Timeout" in name else f"{name} at step '{where}': {first}"
        log("FAILED -> " + reason)
        r = res(False, ["Failed"], reason)
    if page is not None:
        save_shot(page, tag)
    return r


# =====================================================================================================
# Workflow 2 - Report login reset
# =====================================================================================================
def user_reset_one(browser, url, uid, old, new, on_stage=None):
    c = cfg()
    ctx, page = new_page(browser, c, on_stage)

    def work():
        step(page, uid + ": login page")
        stage(page, "login")
        state = app_login(page, c, url, uid, old)
        if state == "login_error":
            raise StepError("Login failed - invalid credentials / account locked: " + (text_visible(page, c["login_error_regex"]) or ""))
        if state == "unknown":
            where = "still on the login page" if login_page_visible(page) else "page: " + (page.url or "?").split("?")[0][-60:]
            raise StepError(f"Login did not complete ({where}) - screenshot saved in the screenshots folder")
        stage(page, "reset")
        if state == "home":
            scenario = "Change Password (profile menu)"
            step(page, uid + ": open Change Password")
            frame, items = open_change_password(page, c, uid)
        else:
            scenario = "expired-password screen"
            frame, items = find_form_frame(page, c)
        step(page, uid + ": fill Existing/New/Confirm")
        fill_reset_form(page, c, frame, items, old, new)
        base_ok, base_bad = matching_texts(page, c["success_regex"]), matching_texts(page, c["failure_regex"])
        dstart = len(DIALOGS.get(page, []))
        watch_start(page, c)
        step(page, uid + ": click Save")
        if not click_save(page, frame, c):
            raise StepError("Save button not found")
        step(page, uid + ": wait for confirmation")
        kind, txt = wait_outcome(page, c, base_ok, base_bad, dstart, allow_login=True, confirm=True)
        if kind == "ok":
            stage(page, "logout")
            try_logout(page, c, uid)
            return res(True, ["Password reset done."], "via " + scenario)
        if kind == "bad":
            raise StepError("Application message: " + txt)
        step(page, uid + ": verify by signing in with the new password")
        page.context.clear_cookies()
        if app_login(page, c, url, uid, new) == "home":
            ensure_ready(page, c)
            stage(page, "logout")
            try_logout(page, c, uid)
            return res(True, ["Password reset done."], "via " + scenario + "; confirmed by signing in with the new password")
        raise StepError("No confirmation from the application and signing in with the new password failed")

    try:
        return _guard(work, page, "user_" + uid, old, new)
    finally:
        ctx.close()


# =====================================================================================================
# Workflow 1 - L2-Sites login reset (unlock / reset)
# =====================================================================================================
def l2_find_frame(page, c, timeout=40):
    rx = re.compile(r"^\s*" + re.escape(c["l2_radio_all"]) + r"\s*$", re.I)
    end = time.time() + timeout
    while time.time() < end:
        for fr in list(page.frames):
            try:
                loc = fr.get_by_text(rx)
                if loc.count() > 0 and loc.first.is_visible():
                    return fr
            except Exception:
                continue
        time.sleep(0.5)
    raise StepError(f"Password Reset screen did not open (radio buttons '{c['l2_radio_locked']} / {c['l2_radio_all']}' not found)")


def l2_open_screen(page, c):
    try:
        open_menu(page, c, c["l2_menu"])
        step(page, "wait for the Password Reset screen")
        return l2_find_frame(page, c, timeout=15)
    except StepError:
        pass
    step(page, "fallback: left menu User Management > " + c["l2_menu"])
    exact = r"^\s*" + re.escape(c["l2_menu"]) + r"\s*$"
    el = visible_text_el(page, exact)
    if el is None:
        grp = visible_text_el(page, r"^\s*User Management\s*$")
        if grp is not None:
            try:
                grp.click(timeout=3000)
            except Exception:
                grp.evaluate("e => e.click()")
        time.sleep(1.0)
        el = visible_text_el(page, exact)
    if el is not None:
        try:
            el.click(timeout=3000)
        except Exception:
            el.evaluate("e => e.click()")
    return l2_find_frame(page, c, timeout=25)


def l2_select_radio(fr, label):
    el = fr.get_by_text(re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I)).first
    el.wait_for(state="visible", timeout=10000)
    try:
        el.click(timeout=4000)
    except Exception:
        el.evaluate("e => e.click()")
    time.sleep(0.8)
    if fr.evaluate(JS_RADIO, [label, False]) is False:  # radio still unchecked -> click the real <input>
        fr.evaluate(JS_RADIO, [label, True])
        time.sleep(0.5)
    time.sleep(1.0)


def l2_filter_username(fr, c, uid):
    for attempt in range(3):
        if not fr.evaluate(JS_TAG_FILTER, c["l2_username_header"]):
            if attempt == 2:
                raise StepError(f"Username filter box (under the '{c['l2_username_header']}' column) not found on the screen")
            time.sleep(1.0)
            continue
        box = fr.locator("[data-l2f='1']").first
        try:
            box.click(timeout=3000)
        except Exception:
            pass
        try:
            safe_fill(box, "")
            box.press_sequentially(uid, delay=30)
        except Exception:
            safe_fill(box, uid)
        for ev in ("input", "keyup", "change"):
            try:
                box.dispatch_event(ev)
            except Exception:
                pass
        try:
            box.press("Enter")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            fr.evaluate(JS_TAG_FILTER, c["l2_username_header"])
            val = fr.locator("[data-l2f='1']").first.input_value(timeout=2000)
        except Exception:
            return
        if (val or "").strip().lower() == uid.strip().lower():
            return
        log(f"{uid}: filter box shows '{val}' instead - typing it again ({attempt + 1}/3)")


def l2_tag_action(fr, uid, label, wait=20):
    """Tag the action link inside exactly this user's row (refuses ambiguous rows). -> (tagged, rows_seen)"""
    end = time.time() + wait
    n = 0
    while time.time() < end:
        try:
            r = fr.evaluate(JS_TAG_ACTION, [uid, label])
            n = r["n"]
            if r["tagged"]:
                time.sleep(0.8)
                r2 = fr.evaluate(JS_TAG_ACTION, [uid, label])  # re-tag after the grid settled
                if r2["tagged"]:
                    return True, r2["n"]
        except Exception:
            pass
        time.sleep(0.5)
    return False, n


def l2_click_action(fr):
    a = fr.locator("[data-l2a='1']").first
    try:
        a.click(timeout=5000)
    except Exception:
        a.evaluate("e => e.click()")


def l2_find_row(page, c, fr, uid, link, radio, attempts=3, wait=10, stop_after_empty=2):
    n, diag = 0, ""
    for attempt in range(1, attempts + 1):
        if attempt == 2:
            step(page, f"retry {uid}: select '{radio}' again")
            l2_select_radio(fr, radio)
        elif attempt == 3:
            step(page, f"retry {uid}: re-open the Password Reset screen")
            fr = l2_open_screen(page, c)
            l2_select_radio(fr, radio)
        step(page, f"filter Username = {uid}" + (f" (attempt {attempt})" if attempt > 1 else ""))
        l2_filter_username(fr, c, uid)
        found, n = l2_tag_action(fr, uid, link, wait=wait + 3 * (attempt - 1))
        if found:
            return fr, True, n, ""
        d = {"count": -1, "filter": None, "rows": []}
        try:
            d = fr.evaluate(JS_GRID_DIAG)
            diag = f"grid showed {d['count']} row(s)" + (": " + " | ".join(d["rows"]) if d["rows"] else "") + f"; filter box = '{d['filter']}'"
        except Exception:
            diag = "grid state unreadable"
        log(f"{uid}: not found (attempt {attempt}/{attempts}) - {diag}")
        if attempt >= stop_after_empty and d["count"] == 0 and (d["filter"] or "").strip().lower() == uid.strip().lower():
            break  # filter applied, grid empty -> the user really is not in this list
    return fr, False, n, diag


def _l2_action(page, c, uid, label_key, radio_key, wait_msg):
    """Click the row action and wait for the application's answer. Returns (kind, text)."""
    base_ok, base_bad = matching_texts(page, c["success_regex"]), matching_texts(page, c["failure_regex"])
    dstart = len(DIALOGS.get(page, []))
    watch_start(page, c)
    return base_ok, base_bad, dstart


def l2_unlock(page, c, uid, required=True):
    """True = unlocked, False = user was not locked (only allowed when `required` is False)."""
    stage(page, "unlock")
    fr = l2_find_frame(page, c, timeout=20)
    step(page, f"select '{c['l2_radio_locked']}'")
    l2_select_radio(fr, c["l2_radio_locked"])
    step(page, f"find {uid} in the Locked Users list")
    fr, found, n, diag = l2_find_row(page, c, fr, uid, c["l2_unlock_link"], c["l2_radio_locked"],
                                     attempts=3 if required else 2, wait=10 if required else 6, stop_after_empty=2 if required else 1)
    if not found:
        if not required:
            log(uid + ": not in the Locked Users list - unlock skipped")
            return False
        if n:
            raise StepError(f"'{c['l2_unlock_link']}' link not found in the row of {uid} ({diag})")
        raise StepError(f"User ID {uid} is not in the Locked Users list - the account is not locked, or the ID is wrong ({diag})")
    base_ok, base_bad, dstart = _l2_action(page, c, uid, None, None, None)
    step(page, f"click '{c['l2_unlock_link']}' for {uid}")
    l2_click_action(fr)
    step(page, "wait for the unlock confirmation")
    kind, txt = wait_outcome(page, c, base_ok, base_bad, dstart, confirm=True, timeout=c["outcome_timeout_s"])
    if kind == "ok":
        time.sleep(2.0)
        return True
    if kind == "bad":
        raise StepError("Unlock failed - application message: " + txt)
    time.sleep(1.5)  # no message: accept it if the user vanished from the Locked list
    gone, _ = l2_tag_action(fr, uid, c["l2_unlock_link"], wait=3)
    if not gone:
        return True
    raise StepError("Unlock: no confirmation message and the user is still in the Locked Users list")


def l2_reset(page, c, uid):
    stage(page, "reset")
    fr = l2_find_frame(page, c, timeout=20)
    step(page, f"select '{c['l2_radio_all']}'")
    l2_select_radio(fr, c["l2_radio_all"])
    step(page, f"find {uid} in the All Users list")
    fr, found, n, diag = l2_find_row(page, c, fr, uid, c["l2_reset_link"], c["l2_radio_all"], attempts=3, wait=10)
    if not found:
        if n:
            raise StepError(f"'{c['l2_reset_link']}' link not found in the row of {uid} ({diag})")
        raise StepError(f"User ID {uid} was not found in the All Users list ({diag})")
    base_ok, base_bad, dstart = _l2_action(page, c, uid, None, None, None)
    step(page, f"click '{c['l2_reset_link']}' for {uid}")
    l2_click_action(fr)
    step(page, "wait for the reset confirmation")
    kind, txt = wait_outcome(page, c, base_ok, base_bad, dstart, confirm=True, timeout=c["outcome_timeout_s"])
    if kind == "ok":
        time.sleep(2.0)
        return
    if kind == "bad":
        raise StepError("Password reset failed - application message: " + txt)
    raise StepError("Password reset: no confirmation message received from the application")


def l2_do_user(page, c, uid, action):
    msgs = []
    if action in ("unlock", "both"):
        done = l2_unlock(page, c, uid, required=(action == "unlock"))
        msgs.append(f"Successfully unlocked user ID {uid}" if done else f"User ID {uid} was not locked - unlock skipped")
    if action in ("reset", "both"):
        l2_reset(page, c, uid)
        msgs.append("Password reset done.")
    return msgs


def demo_result(uid, action):
    if "fail" in uid.lower():
        return res(False, ["Failed"], "Demo: simulated failure")
    msgs = []
    if action in ("unlock", "both"):
        msgs.append(f"Successfully unlocked user ID {uid}")
    if action in ("reset", "both"):
        msgs.append("Password reset done.")
    return res(True, msgs)


def run_l2_batch(url, uids, action, l2_user, l2_pw, tr, finish):
    if DEMO_MODE:
        tr.stage("login")
        time.sleep(0.9)
        for i, uid in enumerate(uids, 1):
            tr.user(uid, i)
            if action in ("unlock", "both"):
                tr.stage("unlock")
                time.sleep(0.8)
            if action in ("reset", "both"):
                tr.stage("reset")
                time.sleep(0.8)
            finish(uid, demo_result(uid, action))
        tr.stage("logout")
        time.sleep(0.5)
        return
    from playwright.sync_api import sync_playwright
    c = cfg()
    with RUN_SEM, sync_playwright() as p:
        browser = launch_browser(p)
        try:
            ctx, page = new_page(browser, c, tr.stage)

            def start_session():
                tr.stage("login")
                state = app_login(page, c, url, l2_user, l2_pw)
                if state == "login_error":
                    raise StepError("L2 login failed - wrong L2 user/password or the L2 account is locked (check Admin > L2 Credentials)")
                if state == "reset_screen":
                    raise StepError("L2 login shows a change-password screen - the L2 password has expired; change it first")
                if state != "home":
                    raise StepError("L2 login did not complete (site slow or unavailable)")
                l2_open_screen(page, c)

            step(page, "L2 login")
            tr.current = ""
            first = _guard(lambda: (start_session(), res(True))[1], page, "l2_login", l2_pw)
            if not first["ok"]:
                for i, uid in enumerate(uids, 1):
                    tr.user(uid, i)
                    finish(uid, first)
                ctx.close()
                return
            need_reopen = [False]
            for i, uid in enumerate(uids, 1):
                tr.user(uid, i)

                def work():
                    if login_page_visible(page):
                        start_session()
                    elif need_reopen[0]:
                        l2_open_screen(page, c)
                        need_reopen[0] = False
                    return res(True, l2_do_user(page, c, uid, action))

                r = _guard(work, page, "l2_" + uid, l2_pw)
                if not r["ok"]:
                    need_reopen[0] = True
                finish(uid, r)
            tr.stage("logout")
            tr.current = ""
            step(page, "logout")
            try_logout(page, c, l2_user)
            ctx.close()
        finally:
            browser.close()


def test_l2_login(url, l2_user, l2_pw):
    if DEMO_MODE:
        return res(True, ["Login OK (demo)"])
    from playwright.sync_api import sync_playwright
    c = cfg()
    try:
        with RUN_SEM, sync_playwright() as p:
            browser = launch_browser(p)
            try:
                ctx, page = new_page(browser, c)
                r = _guard(lambda: res(True, ["Login OK"]) if app_login(page, c, url, l2_user, l2_pw) == "home"
                           else res(False, ["Login failed"], "L2 login did not reach the home screen"), page, "l2_test", l2_pw)
                ctx.close()
                return r
            finally:
                browser.close()
    except Exception as e:
        return res(False, ["Failed"], redact("Browser could not start / site unavailable: " + type(e).__name__, l2_pw))


# =====================================================================================================
# Job runner
# =====================================================================================================
def run_job(job):
    jid, kind = job["id"], job["kind"]
    if kind == "l2test":
        r = test_l2_login(job["url"], job["l2_user"], job["l2_pw"])
        API.call("result", id=jid, uid="", r=r)
        API.call("done", id=jid)
        return
    ids = job["ids"]
    tr = Tracker(jid)
    secrets = [job.get("l2_pw"), job.get("old"), job.get("new")]

    def finish(uid, r):
        API.call("result", id=jid, uid=uid, r=r)

    done = set()

    def finish_once(uid, r):
        done.add(uid)
        finish(uid, r)

    try:
        if kind == "l2":
            run_l2_batch(job["url"], ids, job["action"], job["l2_user"], job["l2_pw"], tr, finish_once)
        else:  # report login reset
            if DEMO_MODE:
                for i, uid in enumerate(ids, 1):
                    tr.user(uid, i)
                    tr.stage("login")
                    time.sleep(0.8)
                    tr.stage("reset")
                    time.sleep(0.8)
                    finish_once(uid, demo_result(uid, "reset"))
            else:
                from playwright.sync_api import sync_playwright
                with RUN_SEM, sync_playwright() as p:
                    browser = launch_browser(p)
                    try:
                        for i, uid in enumerate(ids, 1):
                            tr.user(uid, i)
                            finish_once(uid, user_reset_one(browser, job["url"], uid, job["old"], job["new"], tr.stage))
                    finally:
                        browser.close()
    except Exception as e:
        for uid in ids:
            if uid not in done:
                finish(uid, res(False, ["Failed"], redact("Browser could not start / site unavailable: " + type(e).__name__, *secrets)))
        traceback.print_exc()
    finally:
        tr.close()
        API.call("done", id=jid)


def main():
    if not GAS_URL or not TOKEN:
        sys.exit("Set GAS_URL and WORKER_TOKEN (environment or worker.env). See README.md")
    if not DEMO_MODE and (not GAS_URL.startswith("https://script.google.com/") or not GAS_URL.rstrip("/").endswith("/exec")):
        sys.exit("GAS_URL must be the Web app URL that starts with https://script.google.com/ and ends with /exec")
    ensure_cfg_file()
    print(f"Password Reset worker {WORKER_ID} -> {GAS_URL[:60]}...  (demo={DEMO_MODE}, headless={HEADLESS}, slots={MAX_BROWSERS})")
    try:
        API.call("ping")
        print("Connected to the portal - the portal now shows 'Worker online'. Keep this window open.")
    except Exception as e:
        sys.exit(f"Cannot reach the portal: {e}\nCheck GAS_URL (must end in /exec) and WORKER_TOKEN (copy it again from the setup log).")
    slots = threading.Semaphore(MAX_BROWSERS)

    def runner(job):
        try:
            log(f"job {job['id'][:8]} ({job['kind']}, {len(job.get('ids') or [])} id(s)) started")
            run_job(job)
            log(f"job {job['id'][:8]} finished")
        except Exception as e:
            log(f"job {job['id'][:8]} crashed: {e}")
        finally:
            slots.release()

    last_work = time.time()
    while True:
        try:
            if slots.acquire(blocking=False):
                out = API.call("claim")
                job = out.get("job")
                if job:
                    last_work = time.time()
                    threading.Thread(target=runner, args=(job,), daemon=True).start()
                else:
                    slots.release()
                    if IDLE_EXIT and time.time() - last_work > IDLE_EXIT and slots._value == MAX_BROWSERS:
                        log("no more jobs - exiting")
                        return
            else:
                last_work = time.time()
                API.call("ping")
        except Exception as e:
            log(f"poll error: {e}")
            time.sleep(5)
        time.sleep(POLL)


if __name__ == "__main__":
    main()

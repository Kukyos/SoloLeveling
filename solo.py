"""SoloLeveling — routine nags via Windows toasts + a leveling dashboard.

  python solo.py add "Dentist" 2026-07-21T15:00 --stat VIT --xp 20
  python solo.py add "Guitar" 20:30 --days mon,wed,fri --stat CRE --xp 30
  python solo.py list
  python solo.py rm 3
  python solo.py run          <- notifier + dashboard at http://localhost:7777

Toast buttons: [Done] [Reschedule -> pick time] [Focus 25 min]
Ignored toasts re-nag every 15 minutes. Done = XP toward that task's stat.
"""
import base64
import json
import os
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # use the OS certificate store, so TLS-inspecting networks (campus firewalls) work
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

if hasattr(time, "tzset"):  # serverless hosts run UTC; force local time (no-op on Windows)
    os.environ["TZ"] = os.environ.get("SOLO_TZ", "Asia/Kolkata")
    time.tzset()

DB = Path(__file__).with_name("tasks.json")
SF = Path(__file__).with_name("stats.json")
SKILLS_F = Path(__file__).with_name("skills.json")
WORK_F = Path(__file__).with_name("workouts.json")
HTML = Path(__file__).with_name("dashboard.html")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STATS = {"STR": "Strength", "AGI": "Agility", "INT": "Intelligence",
         "VIT": "Vitality", "DIS": "Discipline", "CRE": "Creativity"}
RANKS = [(100, "S"), (75, "A"), (50, "B"), (25, "C"), (10, "D"), (0, "E")]
MUSCLES = ["CHEST", "BACK", "SHOULDERS", "BICEPS", "TRICEPS", "LEGS", "ABS"]
SET_XP = 15               # muscle XP per logged set
PORT = 7777
NAG_MINUTES = 15
FOCUS_MINUTES = 25
WATER_EVERY_MIN = 90      # hydration nudge cadence, no tracking, no penalty
WATER_HOURS = (8, 22)
SNOOZES = [("In 30 min", 30), ("In 1 hour", 60), ("In 3 hours", 180),
           ("Tonight 8pm", -1), ("Tomorrow, same time", -2)]
lock = threading.Lock()
toaster = None  # set by cmd_run; without it actions still work, just no toasts
FOCUS = {}     # tid -> end timestamp; in-memory only, a restart drops running timers


def load():
    tasks = load_json(DB, [])
    changed = False
    for t in tasks:
        if t.get("days") and not t.get("next"):
            advance(t)
            changed = True
    if changed:
        save(tasks)
    return tasks


def save(tasks):
    save_json(DB, tasks)


# ---------------- storage: Vercel Blob when token present, local files otherwise --------
BLOB = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
BLOB_API = "https://blob.vercel-storage.com"
_blob_base = None  # store's public base url, resolved lazily


def _blob_req(method, url, body=None, headers=None):
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {BLOB}", "x-api-version": "12", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def _blob_save(name, text):
    global _blob_base
    resp = json.loads(_blob_req("PUT", f"{BLOB_API}/?pathname={name}", text.encode(), {
        "x-add-random-suffix": "0", "x-allow-overwrite": "1",
        "x-cache-control-max-age": "0", "x-content-type": "application/json"}))
    _blob_base = resp["url"][: -len(name) - 1]


def _blob_load(name):
    global _blob_base
    if not _blob_base:
        blobs = json.loads(_blob_req("GET", f"{BLOB_API}/?limit=1")).get("blobs", [])
        if not blobs:
            return None
        _blob_base = blobs[0]["url"][: -len(blobs[0]["pathname"]) - 1]
    try:
        return _blob_req("GET", f"{_blob_base}/{name}?t={time.time()}").decode()
    except Exception:
        return None  # not created yet


# Networks that MITM blob.vercel-storage.com (campus FortiGate) can't reach Blob
# directly; SOLO_REMOTE relays all storage through the deployed app instead.
REMOTE = os.environ.get("SOLO_REMOTE", "").rstrip("/")
DATA_FILES = {"tasks.json", "stats.json", "skills.json", "workouts.json"}


def _remote_req(method, name, body=None):
    import urllib.request
    pw = os.environ.get("SOLO_PASSWORD", "")
    req = urllib.request.Request(f"{REMOTE}/api/data/{name}", data=body, method=method, headers={
        "Authorization": "Basic " + base64.b64encode(f"solo:{pw}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception as e:
        import urllib.error
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            return None
        raise


def load_json(p, default):
    if REMOTE:
        s = _remote_req("GET", p.name)
        return json.loads(s) if s else default
    if BLOB:
        s = _blob_load(p.name)
        return json.loads(s) if s else default
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p, d):
    text = json.dumps(d, indent=1, ensure_ascii=False)
    if REMOTE:
        _remote_req("PUT", p.name, text.encode())
    elif BLOB:
        _blob_save(p.name, text)
    else:
        try:
            p.write_text(text, encoding="utf-8")
        except OSError:
            pass  # read-only serverless fs before a Blob store exists: view-only mode


def load_stats():
    return load_json(SF, {"xp": {}, "log": []})


def save_stats(s):
    save_json(SF, s)


def next_occurrence(days, hhmm, after):
    """Next datetime at hhmm whose weekday is in `days`, strictly after `after`."""
    h, m = map(int, hhmm.split(":"))
    cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= after:
        cand += timedelta(days=1)
    wanted = set(range(7)) if days == "daily" else {WEEKDAYS.index(d) for d in days.split(",")}
    while cand.weekday() not in wanted:
        cand += timedelta(days=1)
    return cand


def advance(t, after=None):
    after = after or datetime.now()
    t["next"] = next_occurrence(t["days"], t["time"], after).isoformat() if t.get("days") else None


def level_info(total_xp):
    lvl, need = 1, 100
    while total_xp >= need:
        total_xp -= need
        lvl += 1
        need = 100 + (lvl - 1) * 50
    return lvl, total_xp, need


# Main-level curve: cumulative XP to reach level L is A*(L-1)^B.
# Tuned so a year of the full routine (~180k XP) lands level 100,
# and level 200 costs roughly two further years (3x total).
CURVE_A, CURVE_B = 122.0, 1.585


def main_level(total):
    lvl = int((max(0, total) / CURVE_A) ** (1 / CURVE_B)) + 1
    lo = CURVE_A * (lvl - 1) ** CURVE_B
    hi = CURVE_A * lvl ** CURVE_B
    return lvl, int(total - lo), max(1, int(hi - lo))


def rank_of(lvl):
    return next(r for th, r in RANKS if lvl >= th)


def streak(log):
    days = {e["d"] for e in log if e["x"] > 0}
    d = date.today()
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def title_of(lvl, stk):
    for cond, name in [(lvl >= 100, "Polymath"), (lvl >= 75, "Renaissance Mind"),
                       (stk >= 60, "Immovable"), (lvl >= 50, "Master of Some"),
                       (stk >= 30, "Unbreakable"), (lvl >= 30, "Autodidact"),
                       (stk >= 14, "Relentless"), (lvl >= 15, "Practitioner"),
                       (stk >= 7, "The Consistent"), (lvl >= 5, "Student")]:
        if cond:
            return name
    return "Dabbler"


def scheduled_on(t, d):
    """Recurring, alarmed task falls on date d. Sunday-style notify:false tasks
    and one-offs are exempt from reckoning."""
    if not t.get("days") or t.get("notify", True) is False:
        return False
    wanted = set(range(7)) if t["days"] == "daily" else \
        {WEEKDAYS.index(x) for x in t["days"].split(",")}
    return d.weekday() in wanted


PERFECT_BONUS = 50


def reckon():
    """Midnight reckoning: quests left unanswered on past days auto-fail at 1x XP
    (explicit Fail stays 2x); a day with every quest cleared pays a bonus."""
    with lock:
        st = load_stats()
        today = date.today()
        if st.get("reckoned") is None:
            st["reckoned"] = today.isoformat()
            save_stats(st)
            return
        d = date.fromisoformat(st["reckoned"]) + timedelta(days=1)
        if d >= today:
            return
        tasks = load()
        while d < today:
            ds = d.isoformat()
            logged = {e["id"] for e in st["log"] if e["d"] == ds}
            due = [t for t in tasks if scheduled_on(t, d)]
            for t in due:
                if t["id"] not in logged:
                    stat, xp = t.get("stat", "DIS"), t.get("xp", 10)
                    old = st["xp"].get(stat, 0)
                    st["xp"][stat] = max(0, old - xp)
                    st["log"].append({"d": ds, "id": t["id"], "s": stat, "x": -xp,
                                      "ap": st["xp"][stat] - old, "auto": True})
            if due and all(any(e["d"] == ds and e["id"] == t["id"] and e["x"] > 0
                               for e in st["log"]) for t in due):
                st["xp"]["DIS"] = st["xp"].get("DIS", 0) + PERFECT_BONUS
                st["log"].append({"d": ds, "id": 0, "s": "DIS", "x": PERFECT_BONUS, "bonus": True})
            d += timedelta(days=1)
        st["reckoned"] = (today - timedelta(days=1)).isoformat()
        save_stats(st)


# ---------------- actions (shared by toasts + dashboard) ----------------

def apply_action(tid, action, when_id="30"):
    with lock:
        tasks = load()
        t = next((x for x in tasks if x["id"] == tid), None)
        if not t:
            return
        now = datetime.now()
        if action in ("done", "fail"):
            today = now.date().isoformat()
            st = load_stats()
            if not any(e["d"] == today and e["id"] == tid for e in st["log"]):
                stat, xp = t.get("stat", "DIS"), t.get("xp", 10)
                delta = xp if action == "done" else -2 * xp
                old = st["xp"].get(stat, 0)
                st["xp"][stat] = max(0, old + delta)
                st["log"].append({"d": today, "id": tid, "s": stat, "x": delta,
                                  "ap": st["xp"][stat] - old})
                save_stats(st)
            FOCUS.pop(tid, None)
            advance(t, now)
        elif action == "undo":
            today = now.date().isoformat()
            st = load_stats()
            e = next((x for x in st["log"] if x["d"] == today and x["id"] == tid), None)
            if e:
                st["log"].remove(e)
                st["xp"][e["s"]] = max(0, st["xp"].get(e["s"], 0) - e.get("ap", e["x"]))
                save_stats(st)
                if t.get("days"):
                    t["next"] = f"{today}T{t['time']}:00"
        elif action == "resched":
            mins = int(when_id)
            if mins == -1:
                nxt = now.replace(hour=20, minute=0, second=0, microsecond=0)
                nxt = nxt if nxt > now else nxt + timedelta(days=1)
            elif mins == -2:
                nxt = datetime.fromisoformat(t["next"]) + timedelta(days=1)
            else:
                nxt = now + timedelta(minutes=mins)
            t["next"] = nxt.isoformat()
        elif action == "focus":
            t["next"] = (now + timedelta(minutes=FOCUS_MINUTES + 5)).isoformat()
            FOCUS[tid] = time.time() + FOCUS_MINUTES * 60
            threading.Timer(FOCUS_MINUTES * 60, focus_done, [tid]).start()
        t.pop("last_nag", None)
        save(tasks)


BACKUP_DIR = Path(__file__).with_name("backups")


def backup():
    """Daily rotating copy of all data files; keeps the last 7 days."""
    dest = BACKUP_DIR / date.today().isoformat()
    if BLOB or dest.exists():  # cloud mode: data lives in Blob, nothing local to snapshot
        return
    dest.mkdir(parents=True, exist_ok=True)
    for p in (DB, SF, SKILLS_F, WORK_F):
        if p.exists():
            shutil.copy2(p, dest / p.name)
    for old in sorted(BACKUP_DIR.iterdir())[:-7]:
        shutil.rmtree(old, ignore_errors=True)


def focus_done(tid):
    FOCUS.pop(tid, None)
    notify(tid, "Focus over — did you finish?")


def notify(tid, header="Have you done this?"):
    if not toaster:
        return
    from windows_toasts import Toast, ToastButton, ToastInputSelectionBox, ToastSelection
    t = next((x for x in load() if x["id"] == tid), None)
    if not t:
        return
    sels = [ToastSelection(str(m), lbl) for lbl, m in SNOOZES]
    toaster.show_toast(Toast(
        [header, f"{t['title']}  (+{t.get('xp', 10)} XP {t.get('stat', '')})"],
        inputs=[ToastInputSelectionBox("when", "Reschedule to", sels, default_selection=sels[0])],
        actions=[ToastButton("Done", f"done:{tid}"),
                 ToastButton("Not Done", f"fail:{tid}"),
                 ToastButton("Reschedule", f"resched:{tid}"),
                 ToastButton(f"Focus {FOCUS_MINUTES} min", f"focus:{tid}")],
        on_activated=lambda e: apply_action(int(e.arguments.split(":")[1]),
                                            e.arguments.split(":")[0],
                                            (e.inputs or {}).get("when", "30"))))


# ---------------- dashboard server ----------------

def state():
    reckon()
    now = datetime.now()
    today = now.date().isoformat()
    st = load_stats()
    tasks = load()
    entries = {e["id"]: e["x"] for e in st["log"] if e["d"] == today}
    quests = []
    for t in tasks:
        if t.get("days"):
            wanted = set(range(7)) if t["days"] == "daily" else \
                {WEEKDAYS.index(d) for d in t["days"].split(",")}
            if now.weekday() not in wanted:
                continue
            tm = t["time"]
        elif t.get("next") and t["next"][:10] == today:
            tm = t["next"][11:16]
        else:
            continue
        quests.append({"id": t["id"], "title": t["title"], "time": tm,
                       "stat": t.get("stat", "DIS"), "xp": t.get("xp", 10),
                       "state": "done" if entries.get(t["id"], 0) > 0
                                else "failed" if t["id"] in entries else None})
    quests.sort(key=lambda q: q["time"])
    total = sum(st["xp"].values())
    lvl, into, need = main_level(total)

    skills = load_json(SKILLS_F, [])
    for sk in skills:
        slvl, sinto, sneed = level_info(sk["xp"])
        sk.update(level=slvl, into=sinto, need=sneed)

    wlog = load_json(WORK_F, [])
    muscles = []
    for m in MUSCLES:
        mine = [e for e in wlog if e["m"] == m]
        mlvl, minto, mneed = level_info(sum(e["s"] * SET_XP for e in mine))
        last = max((e["d"] for e in mine), default=None)
        muscles.append({"m": m, "level": mlvl, "into": minto, "need": mneed,
                        "last": (date.today() - date.fromisoformat(last)).days if last else None})
    exlast = {}
    for e in wlog:
        exlast[e["ex"]] = {"w": e["w"], "r": e["r"], "s": e["s"], "m": e["m"], "d": e["d"]}

    days14 = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        es = [e for e in st["log"] if e["d"] == d]
        days14.append({"d": d[8:], "gain": sum(e["x"] for e in es if e["x"] > 0),
                       "loss": -sum(e["x"] for e in es if e["x"] < 0)})
    titles = {t["id"]: t["title"] for t in tasks}
    feed = [{"d": e["d"][5:], "x": e["x"], "s": e["s"], "auto": e.get("auto", False),
             "t": "Perfect Day" if e.get("bonus") else titles.get(e["id"], f"Quest #{e['id']}")}
            for e in st["log"][-14:][::-1]]

    focus = [{"id": k, "title": titles.get(k, "?"), "left": int(v - time.time())}
             for k, v in FOCUS.items() if v > time.time()]
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    wk = [e for e in st["log"] if e["d"] >= monday]
    week = {"gain": sum(e["x"] for e in wk if e["x"] > 0),
            "loss": -sum(e["x"] for e in wk if e["x"] < 0),
            "done": sum(1 for e in wk if e["x"] > 0 and e["id"]),
            "failed": sum(1 for e in wk if e["x"] < 0),
            "perfect": sum(1 for e in wk if e.get("bonus")),
            "sets": sum(e["s"] for e in wlog if e["d"] >= monday)}

    stk = streak(st["log"])

    tm = date.today() + timedelta(days=1)
    tmq = []
    for t in tasks:
        if t.get("days"):
            wanted = set(range(7)) if t["days"] == "daily" else \
                {WEEKDAYS.index(x) for x in t["days"].split(",")}
            if tm.weekday() in wanted:
                tmq.append(t)
        elif t.get("next") and t["next"][:10] == tm.isoformat():
            tmq.append(t)
    tomorrow = {"count": len(tmq), "xp": sum(t.get("xp", 10) for t in tmq),
                "first": min((t.get("time") or t["next"][11:16] for t in tmq), default=None)}

    vol = sum(e["w"] * e["r"] * e["s"] for e in wlog)
    sets = sum(e["s"] for e in wlog)
    achieves = [{"n": n, "d": d_, "got": bool(g)} for n, d_, g in [
        ("First Steps", "Complete your first task", any(e["x"] > 0 and e["id"] for e in st["log"])),
        ("Perfect Day", "Clear every task in a day", any(e.get("bonus") for e in st["log"])),
        ("The Consistent", "Hold a 7-day streak", stk >= 7),
        ("Momentum", "Reach level 10", lvl >= 10),
        ("Quarter Mark", "Reach level 25", lvl >= 25),
        ("Well-Rounded", "Raise every stat to 11", all(st["xp"].get(k, 0) >= 100 for k in STATS)),
        ("Iron Body", "Log 100 training sets", sets >= 100),
        ("Ton Mover", "Move 10,000 kg of total volume", vol >= 10000),
        ("Apprentice", "Level up any skill", any(x["xp"] >= 100 for x in skills)),
        ("Scholar", "Earn 1,000 INT XP", st["xp"].get("INT", 0) >= 1000),
        ("The Polymath", "Reach level 100 — one full year", lvl >= 100),
    ]]

    return {"now": now.strftime("%H:%M"), "date": now.strftime("%a %d %b %Y").upper(),
            "tomorrow": tomorrow, "achieves": achieves, "focus": focus, "week": week,
            "quests": quests, "xp": st["xp"], "total": total, "level": lvl,
            "into": into, "need": need, "rank": rank_of(lvl), "streak": stk,
            "title": title_of(lvl, stk), "days14": days14, "feed": feed,
            "skills": skills, "muscles": muscles, "exlast": exlast,
            "wtoday": [e for e in wlog if e["d"] == today]}


PASSWORD = os.environ.get("SOLO_PASSWORD", "")  # empty = auth off (local use)
STATIC = {"/manifest.json": "application/json", "/sw.js": "text/javascript",
          "/icon.svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not PASSWORD:
            return True
        want = "Basic " + base64.b64encode(f"solo:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") == want:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Polymath OS"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/":
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path in STATIC:
            self._send(200, Path(__file__).with_name(self.path[1:]).read_bytes(), STATIC[self.path])
        elif self.path == "/api/state":
            self._send(200, json.dumps(state()).encode())
        elif self.path.startswith("/api/data/"):
            name = self.path[len("/api/data/"):]
            if name not in DATA_FILES:
                return self._send(404, b"{}")
            if BLOB:
                s = _blob_load(name)
            else:
                p = Path(__file__).with_name(name)
                s = p.read_text(encoding="utf-8") if p.exists() else None
            self._send(200, s.encode()) if s else self._send(404, b"{}")
        else:
            self._send(404, b"{}")

    def do_PUT(self):
        if not self._authed():
            return
        name = self.path[len("/api/data/"):] if self.path.startswith("/api/data/") else ""
        if name not in DATA_FILES:
            return self._send(404, b"{}")
        body = self.rfile.read(int(self.headers["Content-Length"]))
        try:
            json.loads(body)  # only store valid JSON
        except ValueError:
            return self._send(400, b"{}")
        if BLOB:
            _blob_save(name, body.decode())
        else:
            Path(__file__).with_name(name).write_text(body.decode(), encoding="utf-8")
        self._send(200, b"{}")

    def do_POST(self):
        if not self._authed():
            return
        try:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/act":
                apply_action(int(data["id"]), data["action"], str(data.get("when", "30")))
            elif self.path == "/api/skill":
                with lock:
                    skills = load_json(SKILLS_F, [])
                    sk = next(x for x in skills if x["id"] == int(data["id"]))
                    sk["xp"] = sk.get("xp", 0) + max(0, int(data["mins"]))
                    save_json(SKILLS_F, skills)
            elif self.path == "/api/skill_add":
                with lock:
                    skills = load_json(SKILLS_F, [])
                    name = str(data["name"]).strip()[:40]
                    if name:
                        skills.append({"id": max((x["id"] for x in skills), default=0) + 1,
                                       "name": name, "xp": 0})
                        save_json(SKILLS_F, skills)
            elif self.path == "/api/workout":
                if data["m"] not in MUSCLES:
                    raise ValueError("bad muscle")
                with lock:
                    wlog = load_json(WORK_F, [])
                    wlog.append({"d": date.today().isoformat(), "ex": str(data["ex"]).strip()[:60],
                                 "m": data["m"], "w": float(data["w"]),
                                 "r": int(data["r"]), "s": max(1, int(data["s"]))})
                    save_json(WORK_F, wlog)
            else:
                return self._send(404, b"{}")
            self._send(200, b"{}")
        except (KeyError, ValueError, StopIteration):
            self._send(400, b"{}")

    def log_message(self, *a):
        pass


# ---------------- CLI ----------------

def flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def cmd_add(args):
    title, when = args[0], args[1]
    days = flag(args, "--days")
    tasks = load()
    t = {"id": max((x["id"] for x in tasks), default=0) + 1, "title": title,
         "days": days, "stat": flag(args, "--stat", "DIS"), "xp": int(flag(args, "--xp", 10))}
    if days:
        t["time"] = when
        advance(t)
    else:
        t["next"] = (datetime.fromisoformat(when) if "T" in when
                     else next_occurrence("daily", when, datetime.now())).isoformat()
    tasks.append(t)
    save(tasks)
    print(f"[{t['id']}] {title} -> {t['next']}" + (f" ({days})" if days else ""))


def cmd_list():
    for t in load():
        print(f"[{t['id']:3}] {t['title']:55} {t.get('stat', ''):3} +{t.get('xp', 10):<4}"
              + (f"{t['days']} @ {t['time']}" if t.get("days") else f"once @ {t['next']}"))


def cmd_rm(tid):
    save([t for t in load() if t["id"] != tid])
    print(f"removed {tid}")


def cmd_run():
    global toaster
    from windows_toasts import InteractableWindowsToaster, Toast
    toaster = InteractableWindowsToaster("SoloLeveling")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"SoloLeveling running. Dashboard: http://localhost:{PORT}  (Ctrl+C to stop)")
    last_water = time.time()
    while True:
        try:
            reckon()
            backup()
            now = datetime.now()
            if WATER_HOURS[0] <= now.hour < WATER_HOURS[1] \
                    and time.time() - last_water >= WATER_EVERY_MIN * 60:
                toaster.show_toast(Toast(["Hydration check", "Drink some water."]))
                last_water = time.time()
            with lock:
                tasks = load()
                due = [t for t in tasks
                       if t.get("notify", True) and t.get("next")
                       and datetime.fromisoformat(t["next"]) <= now
                       and now.timestamp() - t.get("last_nag", 0) > NAG_MINUTES * 60]
                for t in due:
                    t["last_nag"] = now.timestamp()
                if due:
                    save(tasks)
            for t in due:
                try:
                    notify(t["id"])
                except Exception as e:  # a broken toast must not kill the notifier
                    print(f"toast failed for {t['id']}: {e}")
        except Exception as e:
            print("loop error:", e)
        time.sleep(30)


def demo():
    base = datetime(2026, 7, 19, 12, 0)  # a Sunday
    assert next_occurrence("daily", "18:00", base).isoformat() == "2026-07-19T18:00:00"
    assert next_occurrence("daily", "09:00", base).isoformat() == "2026-07-20T09:00:00"
    assert next_occurrence("mon,wed", "18:00", base).weekday() == 0
    assert next_occurrence("sun", "11:00", base).isoformat() == "2026-07-26T11:00:00"
    assert level_info(0) == (1, 0, 100)
    assert level_info(100) == (2, 0, 150)
    assert level_info(260) == (3, 10, 200)
    assert rank_of(1) == "E" and rank_of(30) == "C" and rank_of(105) == "S"
    assert main_level(0)[0] == 1 and main_level(121)[0] == 1 and main_level(123)[0] == 2
    assert main_level(180000)[0] in (100, 101)      # 1 year of full routine -> ~level 100
    assert main_level(540000)[0] in (199, 200, 201)  # 3 years -> ~level 200
    assert streak([{"d": date.today().isoformat(), "x": 10},
                   {"d": (date.today() - timedelta(days=1)).isoformat(), "x": 5}]) == 2
    assert streak([{"d": date.today().isoformat(), "x": -20}]) == 0
    assert title_of(1, 0) == "Dabbler" and title_of(16, 2) == "Practitioner"
    assert title_of(100, 40) == "Polymath" and title_of(6, 8) == "The Consistent"
    t = {"days": "mon,wed", "notify": True}
    assert scheduled_on(t, date(2026, 7, 20)) and not scheduled_on(t, date(2026, 7, 21))
    assert not scheduled_on({"days": "sun", "notify": False}, date(2026, 7, 19))
    print("ok")


def cmd_skill(name):
    skills = load_json(SKILLS_F, [])
    skills.append({"id": max((x["id"] for x in skills), default=0) + 1, "name": name, "xp": 0})
    save_json(SKILLS_F, skills)
    print(f"skill added: {name}")


def cmd_sync_up():
    """One-shot: upload local data files to cloud storage.
    Via SOLO_REMOTE (app relay, works behind MITM firewalls) or BLOB_READ_WRITE_TOKEN (direct)."""
    assert REMOTE or BLOB, "set SOLO_REMOTE (+SOLO_PASSWORD) or BLOB_READ_WRITE_TOKEN first"
    for p in (DB, SF, SKILLS_F, WORK_F):
        if p.exists():
            text = p.read_text(encoding="utf-8")
            _remote_req("PUT", p.name, text.encode()) if REMOTE else _blob_save(p.name, text)
            print(f"uploaded {p.name}")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else "list"), sys.argv[2:]
    {"add": lambda: cmd_add(rest), "list": cmd_list, "rm": lambda: cmd_rm(int(rest[0])),
     "skill": lambda: cmd_skill(rest[0]), "sync-up": cmd_sync_up,
     "run": cmd_run, "demo": demo}[cmd]()

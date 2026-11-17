#!/usr/bin/env python3
# Telegram channel -> store, on GitHub (no PC needed).
#   ACTION=list   save the channel's files to tg/list.json (the upload page shows them)
#   ACTION=fetch  download message MSG_ID, put it in the store with CAPTION as its description
#   ACTION=watch  if auto-watch is on (tg/state.json), pull every new APK posted since last time
# After a fetch, the "Update store list" workflow is started to add it (with its icon) to index.json.
import asyncio
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import urllib.error

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
CHANNEL = os.environ["TG_CHANNEL"]
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TAG = "apps"
MAX_PER_WATCH = 3

safe_name = lambda n: re.sub(r"[^A-Za-z0-9._-]", "_", n)
PROGRESS_URL = os.environ.get("PROGRESS_URL", "")
PROGRESS_KEY = os.environ.get("PROGRESS_KEY", "")
_last_report = {}


def report(key, force=False, **fields):
    """Tell the upload page how far this job is (shown as a live bar). Never fails the job."""
    import time
    if not PROGRESS_URL or not PROGRESS_KEY:
        return
    now = time.time()
    prev = _last_report.get(key)
    if not force and prev and now - prev[0] < 1.5 and fields.get("stage") == prev[1]:
        return
    _last_report[key] = (now, fields.get("stage"))
    body = json.dumps({"key": key, **fields}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(PROGRESS_URL, data=body, method="POST",
            headers={"content-type": "application/json", "x-key": PROGRESS_KEY, "user-agent": "ZMD-Store"}), timeout=10).read()
    except Exception as e:
        print("  (progress report failed:", e, ")")


def upload_stream(url, path, on_progress):
    """POST a file to GitHub in chunks so the page can show the upload percentage."""
    import http.client
    u = urllib.parse.urlparse(url)
    total = os.path.getsize(path)
    conn = http.client.HTTPSConnection(u.netloc, timeout=900)
    conn.putrequest("POST", u.path + "?" + u.query)
    for k, v in {"Authorization": "Bearer " + TOKEN, "User-Agent": "ZMD-Store", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/vnd.android.package-archive", "Content-Length": str(total)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    sent = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 18)
            if not b:
                break
            conn.send(b)
            sent += len(b)
            on_progress(sent, total)
    r = conn.getresponse()
    t = r.read()
    conn.close()
    try:
        return r.status, json.loads(t) if t else {}
    except Exception:
        return r.status, {"raw": t[:300]}


def gh(method, url, data=None, ctype=None):
    h = {"Authorization": "Bearer " + TOKEN, "User-Agent": "ZMD-Store", "Accept": "application/vnd.github+json"}
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        h["Content-Type"] = ctype or "application/json"
    r = urllib.request.Request(url if url.startswith("http") else API + url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=900) as resp:
            t = resp.read()
            return resp.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")}


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def doc_name(msg):
    if msg.file and msg.file.name:
        return msg.file.name
    return f"file_{msg.id}{(msg.file.ext if msg.file else '') or ''}"


async def listing(c):
    out = []
    async for msg in c.iter_messages(CHANNEL, limit=200):
        if isinstance(msg.media, MessageMediaDocument) and msg.file:
            name = doc_name(msg)
            out.append({
                "id": msg.id, "name": name, "size": msg.file.size or 0,
                "caption": (msg.message or "").strip(),
                "date": msg.date.strftime("%Y-%m-%d") if msg.date else "",
                "is_apk": name.lower().endswith(".apk"),
            })
    save("tg/list.json", {"items": out})
    print(f"channel: {len(out)} file(s)")
    return out


def release():
    s, rel = gh("GET", f"/repos/{REPO}/releases/tags/{TAG}")
    if s != 200:
        sys.exit(f"release not found: {s}")
    s, assets = gh("GET", f"/repos/{REPO}/releases/{rel['id']}/assets?per_page=100")
    return rel["id"], {a["name"]: a for a in assets}


async def fetch_one(c, msg_id, caption=None):
    key = f"tg-{msg_id}"
    msg = await c.get_messages(CHANNEL, ids=msg_id)
    if not msg or not msg.file:
        print(f"message {msg_id}: no file")
        report(key, True, stage="failed", done=True, ok=False, error="ما بيه ملف")
        return False
    name = safe_name(doc_name(msg))
    if not name.lower().endswith(".apk"):
        print(f"{name}: not an APK, skipped")
        report(key, True, stage="failed", done=True, ok=False, error="مو برنامج .apk")
        return False
    if caption is None:
        caption = (msg.message or "").strip()
    size = msg.file.size or 0
    base = {"name": name, "file": name, "size": size, "from": "tg"}
    report(key, True, stage="download", pct=0, done=False, **base)
    path = os.path.join(tempfile.mkdtemp(), name)
    last = [-1]

    def prog(cur, tot):
        p = int(cur * 100 / tot) if tot else 0
        report(key, stage="download", pct=p, done=False, **base)
        if p // 10 != last[0]:
            last[0] = p // 10
            print(f"  download {name}: {p}%", flush=True)

    await c.download_media(msg, file=path, progress_callback=prog)
    report(key, True, stage="upload", pct=0, done=False, **base)
    rid, _ = release()
    # Temporary name first; the old version is replaced only after the new one fully arrived.
    import time
    tmp = f"{name}.part-{int(time.time())}"
    s, d = upload_stream(f"{UPLOADS}/repos/{REPO}/releases/{rid}/assets?name={urllib.parse.quote(tmp)}", path,
                         lambda sent, tot: report(key, stage="upload", pct=int(sent * 100 / tot), done=False, **base))
    os.remove(path)
    if s not in (200, 201):
        print(f"upload of {name} failed: {s} {d}")
        report(key, True, stage="failed", done=True, ok=False, error=f"GitHub رفض ({s})", **base)
        return False
    _, assets = release()
    if name in assets:
        gh("DELETE", f"/repos/{REPO}/releases/assets/{assets[name]['id']}")
    s2, _ = gh("PATCH", f"/repos/{REPO}/releases/assets/{d['id']}", {"name": name})
    if s2 != 200:
        report(key, True, stage="failed", done=True, ok=False, error=f"ما تبدّل اسمه ({s2})", **base)
        return False
    notes = load("notes.json", {})
    notes[name] = caption
    save("notes.json", notes)
    report(key, True, stage="listing", pct=100, done=False, **base)
    print(f"+ {name} is in the store")
    return True


async def main():
    action = os.environ.get("ACTION", "watch")
    state = load("tg/state.json", {"auto": False, "lastId": 0})
    if action == "watch" and not state.get("auto"):
        print("auto-watch is off")
        return
    c = TelegramClient(StringSession(os.environ["TG_SESSION"]), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit("Telegram session is not logged in")
    added = 0
    try:
        if action == "list":
            await listing(c)
        elif action == "fetch":
            cap = os.environ.get("CAPTION")
            added += await fetch_one(c, int(os.environ["MSG_ID"]), cap if cap is not None and os.environ.get("HAS_CAPTION") == "1" else None)
        elif action == "watch":
            items = await listing(c)
            _, assets = release()
            last = state.get("lastId", 0)
            fresh = sorted((it for it in items if it["is_apk"] and it["id"] > last and safe_name(it["name"]) not in assets),
                           key=lambda it: it["id"])
            for it in fresh[:MAX_PER_WATCH]:
                print(f"auto: new in channel: {it['name']}")
                added += await fetch_one(c, it["id"])
                state["lastId"] = it["id"]
            if len(fresh) <= MAX_PER_WATCH:
                state["lastId"] = max([last] + [it["id"] for it in items])
            save("tg/state.json", state)
    finally:
        await c.disconnect()
    open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a").write(f"added={added}\n")


asyncio.run(main())

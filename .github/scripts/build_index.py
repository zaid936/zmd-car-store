#!/usr/bin/env python3
# Rebuilds index.json from the files in the "apps" release. Runs on GitHub (Actions), so the
# store works without the PC. Only new or re-uploaded APKs are downloaded and read; everything
# else is reused from the previous index, so a run takes seconds.
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone

OWNER_REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
TAG = "apps"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
# Never on public hosting (kept bundled inside the ZMD Car app instead).
SKIP_PACKAGES = {"com.shihab.diplay"}


def req(method, url, data=None, ctype=None):
    h = {"Authorization": "Bearer " + TOKEN, "User-Agent": "ZMD-Store", "Accept": "application/vnd.github+json"}
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        h["Content-Type"] = ctype or "application/json"
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            t = resp.read()
            return resp.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")}


def aapt():
    found = sorted(glob.glob(os.path.join(os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk"), "build-tools", "*", "aapt")))
    if not found:
        sys.exit("aapt not found")
    return found[-1]


AAPT = aapt()


def badging(path):
    out = subprocess.run([AAPT, "dump", "badging", path], capture_output=True, text=True).stdout
    pkg = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", out)
    label = re.search(r"application-label:'([^']*)'", out)
    info = {"package": "", "versionCode": 0, "version": "", "label": ""}
    if pkg:
        info.update(package=pkg.group(1), versionCode=int(pkg.group(2)), version=pkg.group(3))
    if label and label.group(1):
        info["label"] = label.group(1)
    return info


def app_icon(path):
    try:
        from loguru import logger
        logger.remove()
    except Exception:
        pass
    try:
        from androguard.core.apk import APK
        a = APK(path)
        res = a.get_app_icon(max_dpi=320) or a.get_app_icon()
        if res and res.lower().endswith((".png", ".webp")):
            data = a.get_file(res)
            if data:
                return data, (".webp" if data[:4] == b"RIFF" else ".png")
    except Exception:
        pass
    return icon_from_zip(path)


def icon_from_zip(path):
    order = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "nodpi", "anydpi"]
    best = None
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                n = info.filename.lower()
                if not n.startswith("res/") or not n.endswith((".png", ".webp")):
                    continue
                base = n.rsplit("/", 1)[-1]
                if not ("launcher" in base or "appicon" in base or base == "icon.png"):
                    continue
                if any(b in base for b in ("background", "_bg", "notification", "notif", "fallback")):
                    continue
                rank = next((i for i, d in enumerate(order) if d in n), len(order))
                key = (-rank, info.file_size)
                if best is None or key > best[0]:
                    best = (key, info.filename)
            if best:
                data = z.read(best[1])
                return data, (".webp" if data[:4] == b"RIFF" else ".png")
    except Exception:
        pass
    return None, None


def letter_icon(label):
    """For apps that ship no icon: a coloured square with the app's first letter, so every app has one."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io
        ch = (label.strip() or "?")[0].upper()
        colors = ["#3d8bfd", "#5fd3a4", "#f5a524", "#e5484d", "#8e4ec6", "#12a594", "#d6409f"]
        bg = colors[sum(map(ord, label)) % len(colors)]
        img = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, 191, 191), radius=42, fill=bg)
        font = None
        for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(f, 110)
                break
            except Exception:
                pass
        d.text((96, 100), ch, fill="white", font=font or ImageFont.load_default(), anchor="mm")
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue(), ".png"
    except Exception as e:
        print("  letter icon failed:", e)
        return None, None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest):
    r = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/octet-stream", "User-Agent": "ZMD-Store"})
    with urllib.request.urlopen(r, timeout=600) as resp, open(dest, "wb") as f:
        while True:
            b = resp.read(1 << 20)
            if not b:
                break
            f.write(b)


def main():
    s, rel = req("GET", f"{API}/repos/{OWNER_REPO}/releases/tags/{TAG}")
    if s != 200:
        sys.exit(f"release '{TAG}' not found: {s}")
    rid = rel["id"]
    s, assets = req("GET", f"{API}/repos/{OWNER_REPO}/releases/{rid}/assets?per_page=100")
    by_name = {a["name"]: a for a in assets}

    try:
        prev = {it["url"].rsplit("/", 1)[-1]: it for it in json.load(open("index.json", encoding="utf-8-sig")).get("items", [])}
    except Exception:
        prev = {}
    try:
        notes = json.load(open("notes.json", encoding="utf-8"))
    except Exception:
        notes = {}

    items, keep = [], set()
    tmp = tempfile.mkdtemp()
    for name in sorted(by_name, key=str.lower):
        a = by_name[name]
        if not name.lower().endswith(".apk"):
            continue
        old = prev.get(name)
        # Unchanged: same asset id (a re-upload always gets a new id). Older entries have no id yet,
        # so for those the size has to match.
        same = old and (old.get("assetId") == a["id"] or (not old.get("assetId") and old.get("size") == a["size"]))
        icon_ok = old and (old.get("icon") or "").rsplit("/", 1)[-1] in by_name
        same = same and icon_ok
        if same:
            item = dict(old)
            item["assetId"] = a["id"]
            item["note"] = (notes.get(name, item.get("note")) or "").strip()
            keep.add(item["icon"].rsplit("/", 1)[-1])
            items.append(item)
            keep.add(name)
            print(f"= {name} (unchanged)")
            continue

        print(f"+ {name} (new) reading...")
        path = os.path.join(tmp, name)
        download(a["url"], path)
        info = badging(path)
        if info["package"] in SKIP_PACKAGES:
            print(f"  {name} is {info['package']}: not allowed on public hosting, removing it")
            req("DELETE", f"{API}/repos/{OWNER_REPO}/releases/assets/{a['id']}")
            os.remove(path)
            continue
        icon_url = ""
        idata, iext = app_icon(path)
        if not idata:
            print("  no icon inside the app, making one from its first letter")
            idata, iext = letter_icon(info["label"] or os.path.splitext(name)[0])
        if idata:
            iname = name + ".icon" + iext
            for n2, a2 in list(by_name.items()):
                if n2.startswith(name + ".icon"):
                    req("DELETE", f"{API}/repos/{OWNER_REPO}/releases/assets/{a2['id']}")
            ct = "image/webp" if iext == ".webp" else "image/png"
            s2, d2 = req("POST", f"{UPLOADS}/repos/{OWNER_REPO}/releases/{rid}/assets?name={urllib.parse.quote(iname)}", idata, ct)
            if s2 in (200, 201):
                icon_url = d2["browser_download_url"]
                keep.add(iname)
        items.append({
            "name": info["label"] or os.path.splitext(name)[0],
            "url": a["browser_download_url"],
            "icon": icon_url,
            "package": info["package"],
            "versionCode": info["versionCode"],
            "version": info["version"],
            "note": (notes.get(name) or "").strip(),
            "sha256": sha256(path),
            "size": a["size"],
            "assetId": a["id"],
        })
        os.remove(path)

    # Icons left behind by deleted apps.
    for n2, a2 in by_name.items():
        if ".icon." in n2 and n2 not in keep:
            req("DELETE", f"{API}/repos/{OWNER_REPO}/releases/assets/{a2['id']}")

    baghdad = datetime.now(timezone.utc) + timedelta(hours=3)
    old_text = open("index.json", encoding="utf-8-sig").read() if os.path.exists("index.json") else ""
    try:
        old_items = json.loads(old_text).get("items") if old_text else None
    except Exception:
        old_items = None
    if old_items == items:
        print("No changes.")
        return
    text = json.dumps({"updated": baghdad.strftime("%Y-%m-%d %H:%M"), "items": items}, ensure_ascii=False, indent=2)
    open("index.json", "w", encoding="utf-8").write(text)
    print(f"index.json: {len(items)} app(s)")


if __name__ == "__main__":
    main()

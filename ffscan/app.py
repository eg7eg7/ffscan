"""ffscan web app: scan photos into a folder with live preview, metadata, enhancement and cloud upload.

Run:  ./run.sh   (or .venv/bin/python -m ffscan.app)   then open http://localhost:8680
Needs: tools/orient (built by run.sh), exiftool and rclone (Homebrew).

Environment (all optional):
  FFSCAN_PORT       web port (default 8680)
  FFSCAN_DATA_DIR   settings, upload record, duplicate index (default ~/Library/Application Support/FF680W)
  FFSCAN_CACHE_DIR  thumbnails and untouched raw scans (default ~/Library/Caches/FF680W)
"""
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cloud, dupes, ff680w
from . import enhance as enhancer

ROOT = Path(__file__).parent
# per-user app data, not ~/Library/Caches (macOS may purge caches)
DATA_DIR = Path(os.environ.get("FFSCAN_DATA_DIR", Path.home() / "Library" / "Application Support" / "FF680W"))
CACHE_DIR = Path(os.environ.get("FFSCAN_CACHE_DIR", Path.home() / "Library" / "Caches" / "FF680W"))
SETTINGS_FILE = DATA_DIR / "settings.json"
ORIENT = ROOT.parent / "tools" / "orient"
RAW_DIR, RAW_KEEP = CACHE_DIR / "raw", 300  # last N untouched scans
DEFAULTS = {
    "host": "",               # empty: find the scanner on the network automatically (Bonjour)
    "folder": str(Path.home() / "Pictures" / "Scans"),
    "dpi": 300,
    "color": "C024",          # C024 colour, M008 greyscale
    "sides": "picture",       # picture | back | both
    "skip_blank_backs": True, # only with sides=both/back: drop backs without writing
    "blank_threshold": 0.015,
    "crop": True,
    "deskew": True,
    "double_feed": 1,         # 0 off, 1, 3
    "quality": 90,
    "auto_rotate": True,
    "enhance": False,         # also save an enhanced copy (original kept), with these steps:
    "enhance_opts": {"red_eye": True, "restore": True, "auto": True},
    "watch": False,           # keep scanning new stacks until stopped
    "duplicates": "flag",     # flag | skip | off: same print already in the output folder (perceptual hash)
    "metadata": {"album": "", "title": "", "description": "", "date": "", "people": "", "place": "", "tags": ""},
    "upload": {"enabled": False, "remote": "", "path": "FastFoto"},
    # Claude: orientation for photos without faces, caption, tags, date, place, back text
    # provider "anthropic" (Claude API, key from Keychain via run.sh) or "vertex" (project/region below)
    "ai": {"enabled": False, "provider": "anthropic", "project": "", "region": "global", "model": "claude-opus-5-5"},
}
NESTED = ("metadata", "upload", "ai", "enhance_opts")

# ---------------------------------------------------------------- settings


def load_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    s = {**DEFAULTS, **saved}
    for k in NESTED:
        s[k] = {**DEFAULTS[k], **saved.get(k, {})}
    return s


def save_settings(s):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ---------------------------------------------------------------- events (SSE)

events, events_lock = [], threading.Lock()


def emit(kind, **data):
    with events_lock:
        events.append({"id": len(events), "kind": kind, "time": time.time(), **data})


# ---------------------------------------------------------------- photo processing


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


ORIENTATION_TAG = {0: 1, 90: 6, 180: 3, 270: 8}  # degrees clockwise -> EXIF Orientation
TAG_ORIENTATION = {v: k for k, v in ORIENTATION_TAG.items()}


def set_rotation(path, degrees):
    """Lossless rotate: write the EXIF Orientation tag (browsers, Photos and Google Photos honour it)."""
    run(["exiftool", "-q", "-overwrite_original", f"-Orientation#={ORIENTATION_TAG[degrees % 360]}", str(path)])


def get_rotation(path):
    out = run(["exiftool", "-s3", "-n", "-Orientation", str(path)]).strip()
    return TAG_ORIENTATION.get(int(out), 0) if out.isdigit() else 0


def auto_rotation(path):
    """(degrees clockwise, faces found) from Apple Vision face roll; faces=0 means 'no opinion'."""
    if not ORIENT.exists():
        return 0, 0
    degrees, faces, _ = run([str(ORIENT), str(path)]).split("\t", 2)
    return int(degrees), int(faces)


def enhance(src, dst, s):
    return enhancer.enhance(src, dst, **s["enhance_opts"])


METADATA_TAGS = {
    "title": ["-XMP-dc:Title={}", "-IPTC:ObjectName={}"],
    "description": ["-XMP-dc:Description={}", "-EXIF:ImageDescription={}", "-IPTC:Caption-Abstract={}"],
    "album": ["-XMP-xmpDM:Album={}"],
    "place": ["-XMP-iptcCore:Location={}", "-IPTC:Sub-location={}"],
}


def write_metadata(path, md):
    """Write user metadata into the JPEG (EXIF/IPTC/XMP via exiftool). Empty fields are left untouched."""
    args = []
    for key, templates in METADATA_TAGS.items():
        if md.get(key):
            args += [t.format(md[key]) for t in templates]
    for key, tags in (("people", ["-XMP-iptcExt:PersonInImage"]), ("tags", ["-XMP-dc:Subject", "-IPTC:Keywords"])):
        for item in [x.strip() for x in md.get(key, "").split(",") if x.strip()]:
            args += [a for t in tags for a in (f"{t}-={item}", f"{t}+={item}")]  # -= then += : no duplicates
    if md.get("date"):  # accepts YYYY, YYYY-MM or YYYY-MM-DD; missing parts default to 01
        y, m, d = (md["date"].replace("/", "-").split("-") + ["01", "01"])[:3]
        args += [f"-EXIF:DateTimeOriginal={int(y):04d}:{int(m):02d}:{int(d):02d} 00:00:00",
                 f"-XMP-photoshop:DateCreated={md['date']}"]
    if args:
        run(["exiftool", "-q", "-overwrite_original", "-codedcharacterset=utf8", *args, str(path)])


def read_metadata(path):
    out = json.loads(run(["exiftool", "-j", "-Title", "-Description", "-Album", "-Location", "-PersonInImage",
                          "-Subject", "-DateTimeOriginal", str(path)]))[0]
    as_list = lambda v: ", ".join(v) if isinstance(v, list) else (v or "")
    return {"title": out.get("Title", ""), "description": out.get("Description", ""), "album": out.get("Album", ""),
            "place": out.get("Location", ""), "people": as_list(out.get("PersonInImage")),
            "tags": as_list(out.get("Subject")), "date": str(out.get("DateTimeOriginal", ""))[:10].replace(":", "-")}


# ---------------------------------------------------------------- cloud upload (rclone)

uploads = queue.Queue()


LEDGER_FILE = SETTINGS_FILE.parent / "uploads.json"  # {path: {"dest": ..., "mtime": ..., "time": ...}}
DUPES = dupes.Index(SETTINGS_FILE.parent / "hashes.json")  # perceptual hashes for duplicate detection
ledger_lock = threading.Lock()


def read_ledger():
    try:
        return json.loads(LEDGER_FILE.read_text())
    except (OSError, ValueError):
        return {}


def record_upload(path, dest):
    with ledger_lock:
        ledger = read_ledger()
        ledger[path] = {"dest": dest, "mtime": os.path.getmtime(path), "time": time.time()}
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_FILE.write_text(json.dumps(ledger))


def upload_state(path, ledger):
    """'uploaded', 'changed' (edited after upload: rotate/metadata/enhance) or None."""
    entry = ledger.get(str(path))
    if not entry:
        return None
    return "uploaded" if abs(os.path.getmtime(path) - entry["mtime"]) < 1 else "changed"


upload_stats = {"total": 0, "done": 0, "failed": 0, "current": None}  # current batch, for the progress bar
stats_lock = threading.Lock()


def queue_upload(path, remote, base):
    with stats_lock:
        if upload_stats["done"] + upload_stats["failed"] >= upload_stats["total"]:  # previous batch finished
            upload_stats.update(total=0, done=0, failed=0, current=None)
        upload_stats["total"] += 1
    uploads.put((str(path), remote, base))
    emit("upload_queued", file=str(path))


def upload_worker():
    while True:
        path, remote, base = uploads.get()
        dest = cloud.destination(remote.rstrip(":"), base, os.path.basename(os.path.dirname(path)), os.path.basename(path))
        upload_stats["current"] = os.path.basename(path)
        for attempt in range(3):
            try:
                if not os.path.exists(path):  # deleted while waiting in the queue
                    raise FileNotFoundError(path)
                run(["rclone", "copyto", path, dest])
                record_upload(path, dest)
                with stats_lock:
                    upload_stats["done"] += 1
                emit("uploaded", file=path, dest=dest)
                break
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                if attempt == 2 or isinstance(e, FileNotFoundError):
                    with stats_lock:
                        upload_stats["failed"] += 1
                    emit("upload_failed", file=path, dest=dest,
                         error="file was deleted" if isinstance(e, FileNotFoundError) else e.stderr.strip()[-300:])
                    break
                time.sleep(5 * (attempt + 1))
        upload_stats["current"] = None


# ---------------------------------------------------------------- scan job

job = {"thread": None, "cancel": threading.Event()}


def process_page(page, s, batch_dir, stamp, backs):
    """Save one scanned side and run the pipeline. Returns the saved path or None if skipped.

    `backs` maps page index -> raw back scan; the scanner sends side A (back) before side B (picture),
    so the AI step can read writing on the back even when backs aren't being saved.
    """
    path = batch_dir / f"{stamp}_{page.index + 1:03d}{'' if page.side == 'B' else '_back'}.jpg"
    RAW_DIR.mkdir(parents=True, exist_ok=True)  # untouched scan, so crop/rotate bugs can be reprocessed
    raw = RAW_DIR / path.name
    raw.write_bytes(page.jpeg)
    for old in sorted(RAW_DIR.glob("*.jpg"), key=os.path.getmtime)[:-RAW_KEEP]:
        old.unlink()
    if page.side == "A":
        backs[page.index] = raw
    want = {"picture": "B", "back": "A"}.get(s["sides"])
    if want and page.side != want:
        return None
    path.write_bytes(page.jpeg)
    if s["crop"]:
        ff680w.autocrop(str(path))
    if page.side == "A" and s["skip_blank_backs"] and s["sides"] != "back":
        ink = ff680w.ink_fraction(ff680w._thumbnail(str(path)))
        if ink < s["blank_threshold"]:
            path.unlink()
            emit("skipped", index=page.index, side=page.side, reason=f"blank back (ink {ink:.3f})")
            return None
    duplicate_of = None
    if page.side == "B" and s["duplicates"] != "off":
        root = Path(s["folder"]).expanduser()
        duplicate_of = DUPES.find_duplicate(path, (p for p in root.glob("**/*.jpg") if dupes.is_picture(p) and p != path))
        if duplicate_of and s["duplicates"] == "skip":
            path.unlink()
            emit("skipped", index=page.index, side=page.side, reason=f"already scanned: {Path(duplicate_of).name}")
            return None
    files = [path]
    if s["enhance"] and page.side == "B":
        enhanced = path.with_name(path.stem + "_enhanced.jpg")
        enhance(path, enhanced, s)
        files.append(enhanced)
    rotation, faces = auto_rotation(path) if s["auto_rotate"] and page.side == "B" else (0, 0)
    for f in files:
        write_metadata(f, s["metadata"])
        if rotation:
            set_rotation(f, rotation)
    for f in files:
        emit("photo", file=str(f), index=page.index, side=page.side, rotation=rotation,
             duplicate_of=duplicate_of if f == path else None)
    if s["ai"]["enabled"] and page.side == "B":
        # slow (seconds per photo): runs in the background so the scanner keeps feeding; uploads after
        ai_jobs.put((path, files, backs.pop(page.index, None), faces > 0, s))
    else:
        queue_uploads(files, s)
    return path


def queue_uploads(files, s):
    if s["upload"]["enabled"] and s["upload"]["remote"]:
        for f in files:
            queue_upload(f, s["upload"]["remote"], s["upload"]["path"])


# ---------------------------------------------------------------- AI analysis (Claude on Vertex AI)

ai_jobs = queue.Queue()


def back_with_writing(raw_back, s):
    """Crop a temp copy of the raw back and return it if it has writing (saves sending blank backs)."""
    import shutil
    import tempfile

    if not raw_back or not Path(raw_back).exists():
        return None
    tmp = Path(tempfile.mkdtemp()) / "back.jpg"
    shutil.copy(raw_back, tmp)
    ff680w.autocrop(str(tmp))
    return tmp if ff680w.ink_fraction(ff680w._thumbnail(str(tmp))) >= s["blank_threshold"] else None


def ai_setup_problem(ai):
    """Why AI analysis can't run with these settings, or None."""
    if ai["provider"] == "vertex":
        return None if ai["project"] else "set the Google Cloud project under AI analysis"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "no Anthropic API key: add it under AI analysis → API key"
    return None


def analyze_photo(path, files, raw_back, face_rotated, s):
    """Ask Claude about one picture, then apply rotation + metadata to all its files and queue uploads."""
    from . import photo_ai

    ai = s["ai"]
    problem = ai_setup_problem(ai)
    if problem:
        raise RuntimeError(problem)
    result = photo_ai.analyze(path, back_with_writing(raw_back, s), provider=ai["provider"], project=ai["project"],
                              region=ai["region"], model=ai["model"])
    md = photo_ai.to_metadata(result, s["metadata"])
    # Apple Vision's face roll is exact when it found faces; otherwise use Claude's judgement
    rotation = None if face_rotated or not s["auto_rotate"] else result["rotation"]
    for f in files:
        write_metadata(f, md)
        if rotation is not None:
            set_rotation(f, rotation)
    for f in files:
        emit("photo_updated", file=str(f), rotation=get_rotation(f), caption=result["caption"],
             back_text=result["back_text"], date=md.get("date") or result["date_estimate"])
    return result


def ai_worker():
    while True:
        path, files, raw_back, face_rotated, s = ai_jobs.get()
        try:
            analyze_photo(path, files, raw_back, face_rotated, s)
        except Exception as e:  # keep the queue alive; the photo is already saved without AI metadata
            msg = str(e)
            if "credit balance is too low" in msg:
                msg = "the Anthropic account has no API credit: add some at console.anthropic.com → Plans & Billing"
            emit("ai_failed", file=str(path), error=msg[:300])
        queue_uploads(files, s)


def scan_job(s):
    cancel = job["cancel"]
    batch_dir = Path(s["folder"]).expanduser() / (s["metadata"]["album"].strip() or time.strftime("%Y-%m-%d"))
    batch_dir.mkdir(parents=True, exist_ok=True)
    emit("job_started", folder=str(batch_dir))
    waiting, count, backs = None, 0, {}
    try:
        while not cancel.is_set():
            with ff680w.FF680W(s["host"]) as sc:
                err = sc.status().get("ERR")
                if not err:
                    waiting = None
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    emit("stack_started")
                    pages = sc.scan(dpi=s["dpi"], color=s["color"], duplex=s["sides"] != "back",  # picture = side B, needs duplex
                                    deskew=s["deskew"], double_feed=s["double_feed"] or None, jpeg_quality=s["quality"])
                    for page in pages:
                        if cancel.is_set():
                            pages.close()  # sends CAN to the scanner
                            break
                        if process_page(page, s, batch_dir, stamp, backs):
                            count += 1
                elif not s["watch"]:
                    raise ff680w.ScannerError(describe_error(err))
                elif err != waiting:
                    waiting = err
                    emit("waiting", message=describe_error(err))
            if not s["watch"]:
                break
            cancel.wait(2)
        emit("job_done", count=count, cancelled=cancel.is_set())
    except Exception as e:
        emit("job_failed", error=str(e), count=count)


def describe_error(err):
    flat = " ".join(" ".join(map(str, v)) for v in err)
    if "PE" in flat:
        return "Feeder is empty: load photos face up."
    if "PJ" in flat:
        return "Paper jam: open the cover, remove the photo, close the cover."
    return f"Scanner reports: {flat}"


# ---------------------------------------------------------------- API

app = FastAPI(title="FF-680W")


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
def put_settings(new: dict):
    s = load_settings()
    for k, v in new.items():
        if k in DEFAULTS:
            s[k] = {**s[k], **v} if isinstance(DEFAULTS[k], dict) else v
    save_settings(s)
    return s


@app.get("/api/scanner")
def scanner_status():
    s = load_settings()
    if job["thread"] and job["thread"].is_alive():
        return {"online": True, "busy": True, "message": "Scanning..."}
    try:
        with ff680w.FF680W(s["host"], timeout=15) as sc:  # 5 s timed out once (scanner waking from sleep?)
            info, err = sc.info(), sc.status().get("ERR")
        return {"online": True, "busy": False, "ready": not err,
                "message": describe_error(err) if err else "Ready: photos loaded.",
                "model": info["PRD"][0][0].decode(), "serial": info["S/N"][0][0].decode(),
                "firmware": info["PRV"][0][0].decode()}
    except Exception as e:
        return {"online": False, "message": f"Cannot reach scanner{' at ' + s['host'] if s['host'] else ''}: {e}"}


@app.post("/api/scan")
def start_scan():
    if job["thread"] and job["thread"].is_alive():
        raise HTTPException(409, "a scan is already running")
    job["cancel"] = threading.Event()
    job["thread"] = threading.Thread(target=scan_job, args=(load_settings(),), daemon=True)
    job["thread"].start()
    return {"started": True}


@app.post("/api/cancel")
def cancel_scan():
    job["cancel"].set()
    return {"cancelling": True}


@app.get("/api/events")
def get_events(since: int = 0):
    """Events after `since`, returned immediately. The page polls this every second.

    Not a stream on purpose: an open stream per tab used up Chrome's 6 connections per host, after which
    every other request from the browser hung.
    """
    with events_lock:
        return events[since:]


@app.post("/api/pick-folder")
def pick_folder():
    """Native macOS folder chooser (the app runs locally, so the dialog opens on this Mac)."""
    try:
        out = run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Save scanned photos to:")'])
    except subprocess.CalledProcessError:
        return {"folder": None}  # user cancelled
    return put_settings({"folder": out.strip()})


def safe_path(file):
    """Only serve/modify files inside the configured output folder."""
    root = Path(load_settings()["folder"]).expanduser().resolve()
    p = Path(file).expanduser().resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "not found")
    return p


@app.get("/api/photos")
def list_photos():
    root = Path(load_settings()["folder"]).expanduser()
    files = sorted(root.glob("**/*.jpg"), key=os.path.getmtime, reverse=True)[:500]
    ledger = read_ledger()
    duplicate_of = {}  # later scans of a print -> the first scan of it
    if load_settings()["duplicates"] != "off":
        for group in DUPES.groups(files):
            duplicate_of.update({p: group[0] for p in group[1:]})
        DUPES.save()
    return [{"file": str(f), "album": f.parent.name, "upload": upload_state(f, ledger),
             "dest": ledger.get(str(f), {}).get("dest"), "duplicate_of": duplicate_of.get(str(f))} for f in files]


@app.get("/api/file")
def get_file(file: str):
    return FileResponse(safe_path(file), media_type="image/jpeg")


THUMB_DIR = CACHE_DIR / "thumbs"


@app.get("/api/thumb")
def get_thumb(file: str):
    """Small cached preview for the grid: full scans are up to 5100 px / 4 MB, and hundreds froze the browser."""
    import hashlib

    from PIL import Image, ImageOps

    p = safe_path(file)
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()  # changes on rotate/edit
    thumb = THUMB_DIR / f"{key}.jpg"
    if not thumb.exists():
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        im = Image.open(p)
        im.draft("RGB", (480, 480))  # JPEG decodes at reduced size: fast
        im = ImageOps.exif_transpose(im).convert("RGB")  # bake rotation in: thumbs carry no EXIF
        im.thumbnail((480, 480))
        im.save(thumb, quality=82)
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/api/metadata")
def get_metadata(file: str):
    return read_metadata(safe_path(file))


@app.put("/api/metadata")
def put_metadata(body: dict):
    for f in body["files"]:
        write_metadata(safe_path(f), body["metadata"])
    return {"updated": len(body["files"])}


@app.post("/api/rotate")
def rotate(body: dict):
    p = safe_path(body["file"])
    set_rotation(p, get_rotation(p) + int(body["degrees"]))
    return {"rotation": get_rotation(p)}


@app.post("/api/enhance")
def enhance_one(body: dict):
    p = safe_path(body["file"])
    if p.stem.endswith("_enhanced"):  # re-enhance from the original, never stack enhancements
        p = safe_path(str(p.with_name(p.stem.removesuffix("_enhanced") + ".jpg")))
    out = p.with_name(p.stem + "_enhanced.jpg")
    steps = enhance(p, out, load_settings())
    run(["exiftool", "-q", "-overwrite_original", "-tagsFromFile", str(p), "-all:all", str(out)])  # keep metadata + rotation
    emit("photo", file=str(out), index=-1, side="B", rotation=get_rotation(out))
    return {"file": str(out), "steps": steps}


KEYCHAIN_SERVICE = "ff680w-anthropic"


@app.get("/api/ai/key")
def ai_key_status():
    return {"present": bool(os.environ.get("ANTHROPIC_API_KEY"))}  # never return the key itself


@app.post("/api/ai/key")
def save_ai_key(body: dict):
    """Check the key with the API, then keep it in the macOS Keychain (run.sh loads it on start)."""
    import re as _re

    import anthropic
    from . import photo_ai

    key = (body.get("key") or "").strip()
    if not _re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]{20,}", key):  # also keeps the Keychain command line safe
        raise HTTPException(400, "that doesn't look like an Anthropic API key (it starts with sk-ant-)")
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise HTTPException(400, "Anthropic rejected this key")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "couldn't reach Anthropic to check the key; try again")
    # `security -i` reads the command from stdin, so the key never appears in the process list
    cmd = f'add-generic-password -U -a "{os.environ.get("USER", "ff680w")}" -s {KEYCHAIN_SERVICE} -w "{key}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, "couldn't save the key in the Keychain")
    os.environ["ANTHROPIC_API_KEY"] = key
    photo_ai._clients.clear()
    return {"present": True}


@app.delete("/api/ai/key")
def delete_ai_key():
    from . import photo_ai

    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    photo_ai._clients.clear()
    return {"present": False}


@app.post("/api/analyze")
def analyze(body: dict):
    """Queue already-scanned photos for AI analysis (their back and enhanced copy are picked up by name)."""
    s = load_settings()
    problem = ai_setup_problem(s["ai"])
    if problem:
        raise HTTPException(400, problem)
    queued = 0
    for f in body["files"]:
        p = safe_path(f)
        if p.stem.endswith(("_back", "_enhanced")):
            continue
        files = [p] + [x for x in [p.with_name(p.stem + "_enhanced.jpg")] if x.exists()]
        back = p.with_name(p.stem + "_back.jpg")
        # an existing rotation (faces or the user) wins over the AI's guess
        ai_jobs.put((p, files, back if back.exists() else None, get_rotation(p) != 0, s))
        queued += 1
    return {"queued": queued}


@app.post("/api/delete")
def delete(body: dict):
    """Move to the macOS Trash (recoverable), not a permanent delete. Accepts {"file"} or {"files"}."""
    import shutil

    trash = Path.home() / ".Trash"
    files = body.get("files") or [body["file"]]
    for f in files:
        p = safe_path(f)
        target = trash / p.name
        if target.exists():  # Trash already has a file with this name
            target = trash / f"{p.stem} {time.strftime('%H.%M.%S')}{p.suffix}"
        shutil.move(str(p), target)
    return {"deleted": len(files)}


@app.get("/api/uploads")
def uploads_progress():
    s = dict(upload_stats)
    s["active"] = s["done"] + s["failed"] < s["total"]
    return s


@app.get("/api/remotes")
def remotes():
    return cloud.remotes()


@app.get("/api/cloud-providers")
def cloud_providers():
    return [{"id": k, "label": p["label"], "oauth": p["oauth"], "note": p["note"],
             "fields": [{"name": n, "label": l, "secret": s} for n, l, s in p.get("fields", [])]}
            for k, p in cloud.PROVIDERS.items()]


@app.post("/api/remotes")
def connect_remote(body: dict):
    """Start connecting an account; progress arrives as remote_* events (sign-in happens in the browser)."""
    if body.get("provider") not in cloud.PROVIDERS:
        raise HTTPException(400, "unknown provider")
    cloud.connect_async(body["provider"], body.get("fields") or {}, emit)
    return {"started": True}


@app.post("/api/remotes/cancel")
def cancel_remote():
    cloud.cancel_connect()
    return {"cancelled": True}


@app.post("/api/remotes/{name}/test")
def test_remote(name: str):
    ok, message = cloud.test(name)
    return {"ok": ok, "message": message}


@app.get("/api/remotes/{name}/folders")
def list_folders(name: str, path: str = ""):
    try:
        return {"path": path.strip("/"), "folders": cloud.folders(name, path)}
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/remotes/{name}/folders")
def create_folder(name: str, body: dict):
    path = body.get("path", "").strip("/")
    if not path or ".." in path.split("/"):
        raise HTTPException(400, "invalid folder name")
    try:
        cloud.make_folder(name, path)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"created": path}


@app.delete("/api/remotes/{name}")
def delete_remote(name: str):
    if name not in {r["name"] for r in cloud.remotes()}:
        raise HTTPException(404, "no such account")
    cloud.delete(name)
    s = load_settings()
    if s["upload"]["remote"] == name:
        put_settings({"upload": {"remote": "", "enabled": False}})
    return {"deleted": name}


@app.post("/api/upload")
def upload(body: dict):
    """Queue uploads: {"files": [...]} or {"pending": true} = every photo not uploaded yet or changed since."""
    s = load_settings()
    if not s["upload"]["remote"]:
        raise HTTPException(400, "connect a cloud account under Cloud upload first")
    files = [p["file"] for p in list_photos() if p["upload"] != "uploaded"] if body.get("pending") else body["files"]
    for f in files:
        queue_upload(safe_path(f), s["upload"]["remote"], s["upload"]["path"])
    return {"queued": len(files)}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

if __name__ == "__main__":
    threading.Thread(target=upload_worker, daemon=True).start()
    threading.Thread(target=ai_worker, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FFSCAN_PORT", 8680)))

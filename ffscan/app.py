"""ffscan web app: scan photos into a folder with live preview, metadata, enhancement and cloud upload.

Run:  ./run.sh   (or .venv/bin/python -m ffscan.app)   then open http://localhost:8680
Needs: tools/orient (built by run.sh), exiftool and rclone (Homebrew).

Environment (all optional; the Docker image sets several of them):
  FFSCAN_PORT       web port (default 8680)
  FFSCAN_BIND       listen address (default 127.0.0.1; 0.0.0.0 inside Docker)
  FFSCAN_PHOTOS_DIR default Save to folder (default ~/Pictures/Scans)
  FFSCAN_SCANNER    scanner address, skipping discovery (default: discover)
  FFSCAN_SUBNET     subnet to search when Bonjour isn't available, e.g. 192.168.1 (default: this machine's /24)
  FFSCAN_DATA_DIR   settings, upload record, duplicate index (default ~/Library/Application Support/FF680W)
  FFSCAN_CACHE_DIR  thumbnails and untouched raw scans (default ~/Library/Caches/FF680W)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cloud, dupes, ff680w, jobs
from . import enhance as enhancer

ROOT = Path(__file__).parent
# per-user app data, not ~/Library/Caches (macOS may purge caches)
DATA_DIR = Path(os.environ.get("FFSCAN_DATA_DIR", Path.home() / "Library" / "Application Support" / "FF680W"))
CACHE_DIR = Path(os.environ.get("FFSCAN_CACHE_DIR", Path.home() / "Library" / "Caches" / "FF680W"))
SETTINGS_FILE = DATA_DIR / "settings.json"
ORIENT = ROOT.parent / "tools" / "orient"
RAW_DIR, RAW_KEEP = CACHE_DIR / "raw", 300  # last N untouched scans
DEFAULTS = {
    "connection": "auto",     # auto (USB if plugged in, else Wi-Fi) | usb | wifi
    "host": os.environ.get("FFSCAN_SCANNER", ""),  # Wi-Fi address; empty: find the scanner on the network
    "folder": os.environ.get("FFSCAN_PHOTOS_DIR", str(Path.home() / "Pictures" / "Scans")),
    "dpi": 300,
    "color": "C024",          # C024 colour, M008 greyscale
    "sides": "picture",       # picture | both | back | fast (face down, single-sided: only the picture is sent)
    "skip_blank_backs": True, # only with sides=both/back: drop backs without writing
    "blank_threshold": 0.015,
    "crop": True,
    "deskew": True,
    "double_feed": 1,         # 0 off, 1, 3
    "quality": 90,
    "scan_width": 8.5,        # inches read across the feeder, centred; narrower for small prints = faster
    "auto_rotate": True,
    "enhance": False,         # also save an enhanced copy (original kept), with these steps:
    "enhance_opts": {"red_eye": True, "restore": True, "auto": True,  # automatic fixes
                     "brightness": 0, "contrast": 0, "saturation": 0, "warmth": 0, "sharpness": 0},  # sliders
    # split: <album>/Originals + <album>/Enhanced | same: next to the original | subfolder: <album>/Enhanced
    # | folder: enhance_folder/<album>
    "enhance_location": "split",
    "enhance_folder": "",
    "name_prefix": "",        # file name prefix; empty = scan time. Tokens: {album} {date}
    "ask_before_scan": True,  # show the batch details dialog when Scan is pressed
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


# Vision scene-classifier confidence an orientation must beat "as scanned" by before a photo without faces is
# rotated. Measured on 14 photos with a clear "up": 12/14 right at 1.0 (leaving all as scanned: 7/14);
# a brightness/texture heuristic managed only 6/12 and was dropped.
SCENE_MARGIN = 1.0


def auto_rotation(path):
    """(degrees clockwise, decided) on-device: Vision face roll when there are faces (exact), else Vision's scene
    classifier when it is clearly more confident about another orientation. decided=False means no opinion."""
    if not ORIENT.exists():
        return 0, False
    degrees, faces, _ = run([str(ORIENT), str(path)]).split("\t", 2)
    if int(faces):
        return int(degrees), True
    best, *scores = run([str(ORIENT), "--scene", str(path)]).split()
    scores = [float(x) for x in scores]
    if max(scores) - scores[0] >= SCENE_MARGIN:
        return int(best), True
    return 0, False


DATE_PATTERNS = [  # handwritten dates on backs: 14.7.1986, 14/07/86, 7.1986, 1986
    (re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})[./-](\d{4})\b"), "my"),
    (re.compile(r"\b(19[0-9]{2}|20[0-9]{2})\b"), "y"),
]


def date_from_text(text):
    """YYYY, YYYY-MM or YYYY-MM-DD from text written on a back, or "". Day/month order is taken as day first
    unless that is impossible (European order), since US-style dates are less common on prints here."""
    for pattern, kind in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if kind == "y":
            return m.group(1)
        if kind == "my":
            month, year = int(m.group(1)), int(m.group(2))
            return f"{year:04d}-{month:02d}" if 1 <= month <= 12 and 1900 <= year <= 2099 else ""
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year += 1900 if year < 100 else 0
        day, month = (b, a) if a <= 12 < b else (a, b)
        if 1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2099:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


# paper-maker watermarks and their OCR fragments, printed on the backs of photo paper
WATERMARK_WORDS = {"kodak", "koda", "kod", "dak", "odak", "fuji", "fujicolor", "fujifilm", "agfa", "konica", "ilford",
                   "dnp", "quality", "paper", "made", "germany", "japan", "usa", "royal", "premium", "digital",
                   "crystal", "archive", "professional", "photo", "print", "lustre", "glossy", "matte"}


def human_text(text):
    """Keep only lines that look written by a person, dropping paper watermarks and machine-printed lab codes.

    A line is kept if it holds a date with separators (14.7.1986, 08/1992), at least two real words, or one word
    plus a year ("Mom 1986"). Measured on real backs: Kodak/Agfa watermarks and codes like
    "L3145CAXCNO.4A 2009" or "504 1111 NN N-12" are all dropped.
    """
    kept = []
    for line in text.splitlines():
        # standalone all-letter tokens only: letters glued to digits ("L3145CAXCNO") belong to a lab code
        tokens = (t.strip(".-") for t in re.split(r"[\s,;:()\[\]•'\"!?]+", line))
        words = [t for t in tokens if re.fullmatch(r"[^\W\d_]{3,}", t) and t.lower() not in WATERMARK_WORDS]
        separated_date = any(p.search(line) for p, kind in DATE_PATTERNS if kind != "y")
        year = re.search(r"(?<![\w.])(19[0-9]{2}|20[0-9]{2})(?![\w.])", line)
        if separated_date or len(words) >= 2 or (words and year):
            kept.append(line.strip())
    return "\n".join(kept)


def read_back(raw_back, s):
    """On-device reading of handwriting or stamps on a print's back (Apple Vision); "" if none.

    Ink alone can't tell one line of handwriting (~0.008) from printed lab codes (0.000-0.012), so any back
    with marks is read, and human_text() decides what counts as writing.
    """
    back = back_with_writing(raw_back, s, threshold=0.002)
    if not back or not ORIENT.exists():
        return ""
    return human_text(run([str(ORIENT), "--text", str(back)]))


def enhance(src, dst, s):
    """Enhanced copy with all of the original's metadata (dates, GPS from imported photos, rotation)."""
    steps = enhancer.enhance(src, dst, **s["enhance_opts"])
    run(["exiftool", "-q", "-overwrite_original", "-tagsFromFile", str(src), "-all:all", str(dst)])
    return steps


def gps_of(path):
    """(latitude, longitude) in decimal degrees stored in the photo (e.g. imported from FastFoto), or None."""
    out = run(["exiftool", "-s3", "-n", "-GPSLatitude", "-GPSLongitude", str(path)]).split()
    try:
        lat, lon = map(float, out)
    except ValueError:  # no GPS (or only one of the two)
        return None
    return None if lat == lon == 0 else (lat, lon)


METADATA_TAGS = {
    "title": ["-XMP-dc:Title={}", "-IPTC:ObjectName={}"],
    "description": ["-XMP-dc:Description={}", "-EXIF:ImageDescription={}", "-IPTC:Caption-Abstract={}"],
    "album": ["-XMP-xmpDM:Album={}"],
    "place": ["-XMP-iptcCore:Location={}", "-IPTC:Sub-location={}"],
    "headline": ["-XMP-photoshop:Headline={}", "-IPTC:Headline={}"],  # the AI caption on its own
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
                          "-Subject", "-DateTimeOriginal", "-Headline", str(path)]))[0]
    as_list = lambda v: ", ".join(v) if isinstance(v, list) else (v or "")
    return {"title": out.get("Title", ""), "description": out.get("Description", ""), "album": out.get("Album", ""),
            "place": out.get("Location", ""), "people": as_list(out.get("PersonInImage")),
            "tags": as_list(out.get("Subject")), "date": str(out.get("DateTimeOriginal", ""))[:10].replace(":", "-"),
            "headline": out.get("Headline", "")}


# ---------------------------------------------------------------- cloud upload (rclone)

JOBS = jobs.JobStore(DATA_DIR / "jobs.db")  # processing, AI and upload queues that survive a restart


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


def queue_upload(path, s):
    path = Path(path)
    root = next((r for r in roots(s) if r.resolve() in path.resolve().parents), path.parent.parent)
    rel = path.resolve().relative_to(root.resolve()).as_posix()  # e.g. "Summer 1987/Enhanced/x_enhanced.jpg"
    with stats_lock:
        if upload_stats["done"] + upload_stats["failed"] >= upload_stats["total"]:  # previous batch finished
            upload_stats.update(total=0, done=0, failed=0, current=None)
        upload_stats["total"] += 1
    JOBS.put("upload", {"path": str(path), "remote": s["upload"]["remote"], "base": s["upload"]["path"], "rel": rel})
    emit("upload_queued", file=str(path))


def upload_worker():
    while True:
        job_id, j = JOBS.queue("upload").get()
        path, remote, base, rel, error = j["path"], j["remote"], j["base"], j["rel"], None
        wait_for_scanner()
        dest = cloud.destination(remote.rstrip(":"), base, rel)
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
                    error = "file was deleted" if isinstance(e, FileNotFoundError) else e.stderr.strip()[-300:]
                    emit("upload_failed", file=path, dest=dest, error=error)
                    break
                time.sleep(5 * (attempt + 1))
        upload_stats["current"] = None
        JOBS.finish(job_id, error)


# ---------------------------------------------------------------- scan job

job = {"thread": None, "cancel": threading.Event()}
FEEDING = threading.Event()  # set while a stack goes through the scanner: everything else waits, so the scan
                             # gets the CPU (pure-Python processing holds the GIL) and the Wi-Fi to itself


def wait_for_scanner():
    while FEEDING.is_set():
        time.sleep(0.5)


def roots(s):
    """Folders the app reads and writes: Save to, plus a separate enhanced-copies folder when one is set."""
    out = [Path(s["folder"]).expanduser()]
    if s["enhance_location"] == "folder" and s["enhance_folder"]:
        out.append(Path(s["enhance_folder"]).expanduser())
    return out


def album_of(original):
    """The album folder of an original: its folder, or the one above an 'Originals' folder."""
    return original.parent.parent if original.parent.name == "Originals" else original.parent


def enhanced_path(original, s, create=False):
    """Where the enhanced copy of `original` goes (same name + _enhanced), per the enhance_location setting."""
    if s["enhance_location"] in ("split", "subfolder"):
        folder = album_of(original) / "Enhanced"
    elif s["enhance_location"] == "folder" and s["enhance_folder"]:
        folder = Path(s["enhance_folder"]).expanduser() / album_of(original).name  # keep album subfolders
    else:
        folder = original.parent
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder / (original.stem + "_enhanced.jpg")


def original_of(enhanced, s):
    """The original of an _enhanced copy, wherever the copy was saved; None if it's gone."""
    name = enhanced.stem.removesuffix("_enhanced") + ".jpg"
    album = Path(s["folder"]).expanduser() / enhanced.parent.name
    candidates = [enhanced.with_name(name), enhanced.parent.parent / name, enhanced.parent.parent / "Originals" / name,
                  album / name, album / "Originals" / name]
    return next((c for c in candidates if c.exists()), None)


def file_prefix(s, stamp):
    """File name prefix for a stack: the user's prefix (with {album}/{date} filled in) or the scan time."""
    p = s["name_prefix"].strip()
    if not p:
        return stamp
    p = p.replace("{album}", s["metadata"]["album"].strip() or "Photos").replace("{date}", time.strftime("%Y-%m-%d"))
    return re.sub(r'[\\/:*?"<>|]+', "-", p).strip(" .") or stamp


def next_number(batch_dir, prefix):
    """Highest number already used with this prefix in the album (incl. Enhanced/), so names never repeat."""
    pattern = re.compile(re.escape(prefix) + r"_(\d+)(?:_back)?(?:_enhanced)?")
    used = [int(m.group(1)) for f in batch_dir.rglob("*.jpg") if (m := pattern.fullmatch(f.stem))]
    return max(used, default=0)


def page_name(prefix, number, side):
    return f"{prefix}_{number:03d}{'' if side == 'B' else '_back'}.jpg"


def save_raw(page, prefix, offset):
    """The only work done while the scanner is feeding: write the untouched scan to disk (milliseconds)."""
    name = page_name(prefix, offset + page.index + 1, page.side)
    RAW_DIR.mkdir(parents=True, exist_ok=True)  # untouched scans also let crop/rotate bugs be reprocessed
    raw = RAW_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"  # unique even with a fixed prefix
    raw.write_bytes(page.jpeg)
    return raw, name


BACK_TEXT = {}  # raw back scan -> text read from it, computed once for side A and reused for side B


def back_text_of(raw_back, s):
    key = str(raw_back)
    if key not in BACK_TEXT:  # also recomputed here after a restart, when the cache is gone
        BACK_TEXT[key] = read_back(raw_back, s)
    return BACK_TEXT[key]


def process_page(page, raw, name, s, out_dir, back_raw=None):
    """Run the pipeline for one scanned side (background worker). Returns the saved path or None if skipped.

    `back_raw` is the raw scan of this picture's back (the scanner sends side A before side B), so writing on
    the back can be read even when backs aren't being saved.
    """
    path = out_dir / name
    for old in sorted(RAW_DIR.glob("*.jpg"), key=os.path.getmtime)[:-RAW_KEEP]:
        old.unlink()
        BACK_TEXT.pop(str(old), None)
    if page.side == "A":
        back_text_of(raw, s)  # read now; the picture (side B) arrives next and reuses it
    want = {"picture": "B", "back": "A"}.get(s["sides"])
    if want and page.side != want:
        return None
    shutil.copyfile(raw, path)
    if s["crop"]:
        ff680w.autocrop(str(path))
    if page.side == "A" and s["skip_blank_backs"] and s["sides"] != "back" and not back_text_of(raw, s):
        ink = ff680w.ink_fraction(ff680w._thumbnail(str(path)))
        if ink < s["blank_threshold"]:
            path.unlink()
            emit("skipped", index=page.index, side=page.side, reason=f"blank back (ink {ink:.3f})")
            return None
    # fast mode reads only the face-down side: a blank one means the prints went in face up (measured: backs
    # ~0.002 ink, pictures 0.14+), so don't save the back as the picture
    if s["sides"] == "fast" and s["crop"] and ff680w.ink_fraction(ff680w._thumbnail(str(path))) < s["blank_threshold"]:
        path.unlink()
        emit("skipped", index=page.index, side=page.side,
             reason="was the back of the print: Fast mode reads only the side facing down, so load photos face DOWN")
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
        enhanced = enhanced_path(path, s, create=True)
        enhance(path, enhanced, s)
        files.append(enhanced)
    rotation, rotation_decided = auto_rotation(path) if s["auto_rotate"] and page.side == "B" else (0, False)
    back_text = back_text_of(back_raw, s) if page.side == "B" and back_raw else ""
    md = dict(s["metadata"])
    if back_text:  # written on the back: keep it with the picture, and take a date from it if none was given
        md["description"] = "\n".join(x for x in (md["description"], "Written on back: " + back_text) if x)
        md["date"] = md["date"] or date_from_text(back_text)
    for f in files:
        write_metadata(f, md)
        if rotation:
            set_rotation(f, rotation)
    for f in files:
        emit("photo", file=str(f), index=page.index, side=page.side, rotation=rotation,
             duplicate_of=duplicate_of if f == path else None)
    if s["ai"]["enabled"] and page.side == "B":
        # slow (seconds per photo): runs in the background so the scanner keeps feeding; uploads after
        # the back goes to Claude only if on-device reading found nothing (writing it couldn't read)
        JOBS.put("ai", {"path": str(path), "files": [str(f) for f in files],
                        "raw_back": None if back_text else (str(back_raw) if back_raw else None),
                        "rotation_decided": rotation_decided, "s": s})
    else:
        queue_uploads(files, s)
    return path


def queue_uploads(files, s):
    if s["upload"]["enabled"] and s["upload"]["remote"]:
        for f in files:
            queue_upload(f, s)


# ---------------------------------------------------------------- AI analysis (Claude on Vertex AI)


def back_with_writing(raw_back, s, threshold=None):
    """Crop a temp copy of the raw back and return it if it has marks (saves sending blank backs)."""
    import shutil
    import tempfile

    if not raw_back or not Path(raw_back).exists():
        return None
    tmp = Path(tempfile.mkdtemp()) / "back.jpg"
    shutil.copy(raw_back, tmp)
    ff680w.autocrop(str(tmp))
    return tmp if ff680w.ink_fraction(ff680w._thumbnail(str(tmp))) >= (threshold or s["blank_threshold"]) else None


def ai_setup_problem(ai):
    """Why AI analysis can't run with these settings, or None."""
    if ai["provider"] == "vertex":
        return None if ai["project"] else "set the Google Cloud project under AI analysis"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "no Anthropic API key: add it under AI analysis → API key"
    return None


def analyze_photo(path, files, raw_back, rotation_decided, s):
    """Ask Claude about one picture, then apply rotation + metadata to all its files and queue uploads."""
    from . import photo_ai

    ai = s["ai"]
    problem = ai_setup_problem(ai)
    if problem:
        raise RuntimeError(problem)
    result = photo_ai.analyze(path, back_with_writing(raw_back, s), provider=ai["provider"], project=ai["project"],
                              region=ai["region"], model=ai["model"], gps=gps_of(path))
    existing = read_metadata(path)  # batch description and text read from the back on-device
    md = photo_ai.to_metadata(result, {**s["metadata"], "date": s["metadata"]["date"] or existing["date"]})
    # the caption always goes in: first line of the description (before the batch description and back text)
    # and on its own as the Headline. A previous caption is replaced, not stacked, when a photo is re-analysed.
    rest = existing["description"]
    if existing["headline"] and rest.startswith(existing["headline"]):
        rest = rest[len(existing["headline"]):].lstrip("\n")
    parts = [result["caption"], rest]
    if result["back_text"] and result["back_text"] not in rest:
        parts.append("Written on back: " + result["back_text"])
    md["description"] = "\n".join(p for p in parts if p)
    md["headline"] = result["caption"]
    # on-device rotation (faces, or a confident scene classifier) wins; Claude decides only when it had no opinion
    rotation = None if rotation_decided or not s["auto_rotate"] else result["rotation"]
    for f in files:
        write_metadata(f, md)
        if rotation is not None:
            set_rotation(f, rotation)
    for f in files:
        emit("photo_updated", file=str(f), rotation=get_rotation(f), caption=result["caption"],
             back_text=result["back_text"], date=md.get("date") or result["date_estimate"])
    return result


NO_CREDIT = threading.Event()  # set when the Anthropic account ran out: the rest of the queue is skipped, not sent


def ai_worker():
    while True:
        job_id, j = JOBS.queue("ai").get()
        wait_for_scanner()
        path, files, s, error = Path(j["path"]), [Path(f) for f in j["files"]], j["s"], None
        try:
            if NO_CREDIT.is_set():
                raise RuntimeError("skipped: no API credit")  # "Analyse remaining" picks it up after a top-up
            analyze_photo(path, files, j["raw_back"], j["rotation_decided"], s)
        except Exception as e:  # keep the queue alive; the photo is already saved without AI metadata
            error = str(e)
            if "credit balance is too low" in error:
                NO_CREDIT.set()
                error = "the Anthropic account has no API credit"
                emit("ai_no_credit")
            if not error.startswith("skipped"):
                emit("ai_failed", file=str(path), error=error[:300])
        queue_uploads([f for f in files if f.exists()], s)
        JOBS.finish(job_id, error)


progress_lock = threading.Lock()
PROGRESS = {}  # scan job id -> counters for the status line (jobs resumed after a restart have none)


def process_worker():
    """Scanned sides are processed strictly in order (back pairing and duplicate checks depend on it)."""
    while True:
        job_id, j = JOBS.queue("process").get()
        wait_for_scanner()  # scan first, at full speed; process the batch as soon as the scanner stops
        stats, error = PROGRESS.get(j["batch"]), None
        try:
            saved = process_page(ff680w.Page(j["index"], j["side"], 0, 0, b""), Path(j["raw"]), j["name"], j["s"],
                                 Path(j["out_dir"]), j["back_raw"] and Path(j["back_raw"]))
            if saved and stats:
                stats["saved"] += 1
        except Exception as e:  # one bad photo must not stop the rest; its raw scan is still in the cache
            error = str(e)[:300]
            emit("process_failed", file=j["name"], error=error)
        JOBS.finish(job_id, error)
        if stats:
            with progress_lock:
                stats["processed"] += 1
                emit("processed", scanned=stats["scanned"], processed=stats["processed"])
                if stats["scan_done"] and stats["processed"] == stats["scanned"]:
                    emit("processing_done", saved=stats["saved"])


def scan_job(s):
    cancel = job["cancel"]
    batch_dir = Path(s["folder"]).expanduser() / (s["metadata"]["album"].strip() or time.strftime("%Y-%m-%d"))
    out_dir = batch_dir / "Originals" if s["enhance_location"] == "split" else batch_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    emit("job_started", folder=str(batch_dir))
    waiting, back_raws, batch = None, {}, f"{time.time():.6f}"
    stats = PROGRESS[batch] = {"scanned": 0, "processed": 0, "saved": 0, "scan_done": False}
    try:
        while not cancel.is_set():
            with ff680w.FF680W(s["host"], transport=s["connection"]) as sc:
                err = sc.status().get("ERR")
                if not err:
                    waiting = None
                    prefix = file_prefix(s, time.strftime("%Y%m%d-%H%M%S"))
                    offset = next_number(batch_dir, prefix)
                    emit("stack_started")
                    side_started = time.monotonic()
                    # face-up prints: the picture is side B, so both sides must be sent. "fast": prints face down,
                    # single-sided, so the scanner sends only side A, which is then the picture (about half the data)
                    fast = s["sides"] == "fast"
                    pages = sc.scan(dpi=s["dpi"], color=s["color"], duplex=s["sides"] in ("picture", "both"),
                                    deskew=s["deskew"], double_feed=s["double_feed"] or None, jpeg_quality=s["quality"],
                                    width_in=s["scan_width"])
                    FEEDING.set()
                    try:
                        for page in pages:
                            if cancel.is_set():
                                pages.close()  # sends CAN to the scanner
                                break
                            if fast:
                                page = page._replace(side="B")
                            raw, name = save_raw(page, prefix, offset)  # fast: the scanner keeps feeding
                            now = time.monotonic()
                            mb, secs, side_started = len(page.jpeg) / 1e6, now - side_started, now
                            with progress_lock:
                                stats["scanned"] += 1
                            if page.side == "A":
                                back_raws[page.index] = raw
                            back_raw = back_raws.pop(page.index, None) if page.side == "B" else None
                            JOBS.put("process", {"index": page.index, "side": page.side, "raw": str(raw), "name": name,
                                                 "s": s, "out_dir": str(out_dir), "back_raw": back_raw and str(back_raw),
                                                 "batch": batch})
                            st = getattr(sc, "last_side_stats", {})
                            emit("scanned", scanned=stats["scanned"], processed=stats["processed"], side=page.side,
                                 mb=round(mb, 2), secs=round(secs, 2), block_size=sc.block_size_accepted,
                                 blocks=st.get("blocks"), max_block_kb=st.get("max_block", 0) // 1024,
                                 wait_s=round(st.get("wait_s", 0), 2), transfer_s=round(st.get("transfer_s", 0), 2))
                    finally:
                        FEEDING.clear()  # processing, AI and uploads continue now
                elif not s["watch"]:
                    raise ff680w.ScannerError(describe_error(err))
                elif err != waiting:
                    waiting = err
                    emit("waiting", message=describe_error(err))
            if not s["watch"]:
                break
            cancel.wait(2)
        emit("job_done", count=stats["scanned"], cancelled=cancel.is_set())
    except Exception as e:
        emit("job_failed", error=str(e), count=stats["scanned"])
    finally:
        with progress_lock:
            stats["scan_done"] = True
            if stats["processed"] == stats["scanned"]:  # the worker already caught up
                emit("processing_done", saved=stats["saved"])


def fastfoto_photos(folder):
    """(picture, back or None) pairs in an Epson FastFoto folder, in name order, subfolders included.

    FastFoto saves Name_0001.jpg (the scan), Name_0001_a.jpg (its enhanced copy) and Name_0001_b.jpg (the back).
    The enhanced copies are skipped: the pipeline makes its own from the original.
    """
    files = {f.with_suffix(""): f for f in Path(folder).expanduser().rglob("*") if f.suffix.lower() in (".jpg", ".jpeg")}
    out = []
    for key, f in sorted(files.items(), key=lambda kv: str(kv[0]).lower()):
        base = key.with_name(re.sub(r"_[ab]$", "", key.name))
        if key == base or (key.name.endswith("_a") and base not in files):  # an original, or an enhanced copy alone
            out.append((f, files.get(base.with_name(base.name + "_b"))))
    return out


def import_job(folder, s):
    """Feed photos from Epson's FastFoto app through the same pipeline as a scan (no scanner involved)."""
    photos = fastfoto_photos(folder)
    # already cropped by FastFoto; fast mode's face-down check only makes sense for real scans
    s = {**s, "crop": False, "sides": "picture" if s["sides"] == "fast" else s["sides"]}
    batch_dir = Path(s["folder"]).expanduser() / (s["metadata"]["album"].strip() or Path(folder).name)
    out_dir = batch_dir / "Originals" if s["enhance_location"] == "split" else batch_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = file_prefix(s, time.strftime("%Y%m%d-%H%M%S"))
    offset, batch = next_number(batch_dir, prefix), f"{time.time():.6f}"
    sides = ["A", "B"] if s["sides"] in ("both", "back") else ["B"]
    jobs_ = [(i, side, back if side == "A" else picture, back) for i, (picture, back) in enumerate(photos)
             for side in sides if side == "B" or back]
    PROGRESS[batch] = {"scanned": len(jobs_), "processed": 0, "saved": 0, "scan_done": True}
    emit("import_started", folder=str(batch_dir), source=str(folder), count=len(photos))
    for i, side, raw, back in jobs_:
        # the source files are only read (copied into the album), never changed
        JOBS.put("process", {"index": i, "side": side, "raw": str(raw), "name": page_name(prefix, offset + i + 1, side),
                             "s": s, "out_dir": str(out_dir), "back_raw": back and str(back), "batch": batch})
    if not jobs_:
        emit("processing_done", saved=0)
    return len(photos)


def describe_error(err):
    flat = " ".join(" ".join(map(str, v)) for v in err)
    if "PE" in flat:
        return "Feeder is empty: load photos face up."
    if "PJ" in flat:
        return "Paper jam: open the cover, remove the photo, close the cover."
    return f"Scanner reports: {flat}"


# ---------------------------------------------------------------- API

REPO_URL = "https://github.com/eg7eg7/ffscan"


def _commit():
    """Short commit hash of this build: FFSCAN_COMMIT (set in Docker builds) or the git checkout's HEAD."""
    if os.environ.get("FFSCAN_COMMIT"):
        return os.environ["FFSCAN_COMMIT"][:7]
    try:
        out = subprocess.run(["git", "-C", str(ROOT.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


VERSION = {"repo": REPO_URL, "commit": _commit()}

app = FastAPI(title="FF-680W")


@app.get("/api/version")
def version():
    return VERSION


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
        with ff680w.FF680W(s["host"], timeout=15, transport=s["connection"]) as sc:  # 5 s timed out once (waking?)
            info, err, via = sc.info(), sc.status().get("ERR"), "USB" if sc.usb else "Wi-Fi"
        return {"online": True, "busy": False, "ready": not err, "via": via,
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


@app.post("/api/import/pick")
def pick_import_folder():
    """Choose a FastFoto folder (native dialog); returns it with the number of photos found."""
    try:
        out = run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Import photos saved by Epson FastFoto:")'])
    except subprocess.CalledProcessError:
        return {"folder": None}
    except FileNotFoundError:
        raise HTTPException(501, "the folder picker needs macOS; type the folder path instead")
    folder = out.strip()
    return {"folder": folder, "count": len(fastfoto_photos(folder)), "name": Path(folder).name}


@app.post("/api/import")
def start_import(body: dict):
    folder = Path(body["folder"]).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, f"not a folder: {folder}")
    return {"queued": import_job(folder, load_settings())}


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
def pick_folder(body: dict | None = None):
    """Native macOS folder chooser (the app runs locally, so the dialog opens on this Mac).

    {"key": "enhance_folder"} picks the enhanced-copies folder instead of Save to.
    """
    key = (body or {}).get("key", "folder")
    if key not in ("folder", "enhance_folder"):
        raise HTTPException(400, "unknown folder setting")
    prompt = "Save enhanced copies to:" if key == "enhance_folder" else "Save scanned photos to:"
    try:
        out = run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'])
    except subprocess.CalledProcessError:
        return {"folder": None}  # user cancelled
    except FileNotFoundError:
        raise HTTPException(501, "the folder picker needs macOS; type the folder path instead")
    return put_settings({key: out.strip()})


def safe_path(file):
    """Only serve/modify files inside the app's folders (Save to, and a separate enhanced folder if set)."""
    p = Path(file).expanduser().resolve()
    if not any(r.resolve() in p.parents for r in roots(load_settings())) or not p.is_file():
        raise HTTPException(404, "not found")
    return p


@app.get("/api/photos")
def list_photos():
    found = {f for r in roots(load_settings()) for f in r.glob("**/*.jpg")}
    files = sorted(found, key=os.path.getmtime, reverse=True)[:500]
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


ENHANCE_KEYS = {"red_eye", "restore", "auto", *enhancer.MANUAL}
_eye_cache = {}  # path -> eye positions (Vision is slow enough to notice while dragging a slider)


@app.get("/api/preview")
def preview(file: str, opts: str = "{}", original: bool = False):
    """A real photo at preview size, with the given enhancement options applied (or untouched if original)."""
    import io

    from fastapi.responses import Response
    from PIL import Image

    p = safe_path(file)
    im = Image.open(p)
    full_width, rotation = im.width, get_rotation(p)
    im.draft("RGB", (1000, 1000))
    im = im.convert("RGB")
    im.thumbnail((1000, 1000))
    if not original:
        o = {k: v for k, v in json.loads(opts).items() if k in ENHANCE_KEYS}
        eyes = ()
        if o.get("red_eye"):
            if str(p) not in _eye_cache:
                _eye_cache[str(p)] = enhancer.find_eyes(p)
            k = im.width / full_width  # eye positions are in full-size pixels
            eyes = [(x * k, y * k, r * k) for x, y, r in _eye_cache[str(p)]]
        im, _ = enhancer.enhance_image(im, eyes, **o)
    if rotation:  # show it the way photo apps will
        im = im.rotate(-rotation, expand=True)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/enhance")
def enhance_one(body: dict):
    """Enhanced copy of one photo, with the saved options or {"opts": {...}} from the adjust dialog."""
    s = load_settings()
    if body.get("opts"):
        s = {**s, "enhance_opts": {**s["enhance_opts"], **{k: v for k, v in body["opts"].items() if k in ENHANCE_KEYS}}}
    p = safe_path(body["file"])
    if p.stem.endswith("_enhanced"):  # re-enhance from the original, never stack enhancements
        original = original_of(p, s)
        if not original:
            raise HTTPException(404, "the original of this enhanced copy is gone")
        p = safe_path(str(original))
    out = enhanced_path(p, s, create=True)
    steps = enhance(p, out, s)
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
    import shutil

    os.environ["ANTHROPIC_API_KEY"] = key
    photo_ai._clients.clear()
    if not shutil.which("security"):  # no macOS Keychain (Docker/Linux): works until the app restarts
        return {"present": True, "persisted": False}
    # `security -i` reads the command from stdin, so the key never appears in the process list
    cmd = f'add-generic-password -U -a "{os.environ.get("USER", "ff680w")}" -s {KEYCHAIN_SERVICE} -w "{key}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, "couldn't save the key in the Keychain")
    return {"present": True, "persisted": True}


@app.post("/api/ai/test")
def test_ai():
    """One tiny real request to the selected service, so problems show up before a batch is scanned."""
    import anthropic

    from . import photo_ai

    ai = load_settings()["ai"]
    problem = ai_setup_problem(ai)
    if problem:
        return {"ok": False, "message": problem}
    try:
        r = photo_ai.client(ai["provider"], ai["project"], ai["region"]).messages.create(
            model=ai["model"], max_tokens=64, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word OK."}])
        service = "Google Vertex AI" if ai["provider"] == "vertex" else "the Anthropic API"
        return {"ok": True, "message": f"Works: {r.model} answered through {service}."}
    except anthropic.AuthenticationError:
        return {"ok": False, "message": "The API key was rejected. Create a new one at https://platform.claude.com/dashboard"}
    except anthropic.PermissionDeniedError as e:
        return {"ok": False, "message": f"This key or project may not use {ai['model']}: {e.message}"}
    except anthropic.RateLimitError as e:
        quota = "quota" in str(e).lower()
        return {"ok": False, "message": "No quota for Claude in this Google Cloud project yet: request it under IAM & Admin → Quotas."
                if quota else "Rate limited right now; try again in a minute."}
    except anthropic.BadRequestError as e:
        if "credit balance is too low" in str(e):
            return {"ok": False, "message": "The key works, but the account has no API credit: top it up at https://platform.claude.com/dashboard"}
        return {"ok": False, "message": f"Request refused: {e.message}"}
    except anthropic.APIConnectionError:
        return {"ok": False, "message": "Couldn't reach the service: check the internet connection."}
    except Exception as e:  # e.g. missing Google credentials for Vertex
        return {"ok": False, "message": str(e)[:300]}


@app.delete("/api/ai/key")
def delete_ai_key():
    from . import photo_ai

    if shutil.which("security"):
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    photo_ai._clients.clear()
    return {"present": False}


def unanalysed(s):
    """Pictures without an AI caption (Headline), in all of the app's folders. One exiftool run per folder."""
    out = []
    for root in roots(s):
        if root.is_dir():
            rows = json.loads(run(["exiftool", "-j", "-q", "-r", "-ext", "jpg", "-Headline", str(root)]) or "[]")
            out += [r["SourceFile"] for r in rows if not r.get("Headline") and dupes.is_picture(r["SourceFile"])]
    return out


@app.post("/api/analyze")
def analyze(body: dict):
    """Queue already-scanned photos for AI analysis (their back and enhanced copy are picked up by name)."""
    s = load_settings()
    problem = ai_setup_problem(s["ai"])
    if problem:
        raise HTTPException(400, problem)
    NO_CREDIT.clear()  # the user is retrying, e.g. after topping up
    files = unanalysed(s) if body.get("pending") else body["files"]
    queued, seen = 0, set()
    for f in files:
        p = safe_path(f)
        if p.stem.endswith("_back") or not dupes.is_picture(p) and not p.stem.endswith("_enhanced"):
            continue
        if p.stem.endswith("_enhanced"):  # one AI call per photo: analyse the original, results go to both
            original = original_of(p, s)
            if not original:
                continue
            p = original
        if p in seen:
            continue
        seen.add(p)
        files = [p] + [x for x in [enhanced_path(p, s)] if x.exists()]
        back = p.with_name(p.stem + "_back.jpg")
        # an existing rotation (faces or the user) wins over the AI's guess
        JOBS.put("ai", {"path": str(p), "files": [str(f) for f in files], "raw_back": str(back) if back.exists() else None,
                        "rotation_decided": get_rotation(p) != 0, "s": s})
        queued += 1
    return {"queued": queued}


@app.get("/api/cloud_link")
def cloud_link(file: str):
    """Web page of a photo's cloud copy (Google Drive, Google Photos, Dropbox, Box)."""
    entry = read_ledger().get(str(safe_path(file)))
    url = entry and cloud.web_url(entry["dest"])
    if not url:
        raise HTTPException(404, "this cloud service has no web page for single files")
    return {"url": url}


@app.post("/api/reveal")
def reveal(body: dict):
    """Show the photo in Finder (selected in its folder)."""
    try:
        run(["open", "-R", str(safe_path(body["file"]))])
    except FileNotFoundError:
        raise HTTPException(501, "Show in Finder needs macOS")
    return {"revealed": True}


@app.post("/api/delete")
def delete(body: dict):
    """Move to the macOS Trash (recoverable), not a permanent delete. Accepts {"file"} or {"files"}."""
    import shutil

    trash = Path.home() / ".Trash" if (Path.home() / ".Trash").is_dir() else DATA_DIR / "Trash"
    trash.mkdir(parents=True, exist_ok=True)
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
        queue_upload(safe_path(f), s)
    return {"queued": len(files)}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


def startup():
    """Before serving: the API key from the Keychain, then the workers (resuming work left from the last run)."""
    if not os.environ.get("ANTHROPIC_API_KEY") and shutil.which("security"):
        key = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True).stdout.strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    JOBS.prune()
    pending = JOBS.counts()
    if pending:  # work queued before the last shutdown is picked up again by the workers
        print("Resuming unfinished work:", ", ".join(f"{n} {kind}" for kind, n in pending.items()))
        emit("resumed", **pending)
    threading.Thread(target=upload_worker, daemon=True).start()
    threading.Thread(target=ai_worker, daemon=True).start()
    threading.Thread(target=process_worker, daemon=True).start()


if __name__ == "__main__":
    startup()
    uvicorn.run(app, host=os.environ.get("FFSCAN_BIND", "127.0.0.1"), port=int(os.environ.get("FFSCAN_PORT", 8680)))

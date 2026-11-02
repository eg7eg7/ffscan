"""Offline checks: protocol parsing, cropping, enhancement, duplicates, AI metadata merging.

Run from the repository root:  .venv/bin/python tests/test_ffscan.py
Protocol fixtures are real scanner replies with the serial number replaced.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffscan.ff680w import fix_jpeg_height, parse_tokens, tokens_to_dict

INFO = (b"#ADFTYPEFEED#ADFDPLX1SCN#ADFAREAd850i0001550#IMXi0065535i0065535#PRDh007FF-680W\x00"
        b"#PRVh00C18.61.SP18P4#S/Nh00AX0AB000000\x00\x00#AFM#DFMd100#EXI")
d = tokens_to_dict(parse_tokens(INFO))
assert d["ADF"] == [["TYPE", "FEED"], ["DPLX", "1SCN"], ["AREA", 850, 1550]], d["ADF"]
assert d["PRD"] == [[b"FF-680W"]] and d["S/N"] == [[b"X0AB000000"]] and d["PRV"] == [[b"18.61.SP18P4"]]
assert d["DFM"] == [[100]] and d["AFM"] == [[]] and d["EXI"] == [[]]

hdr = b"#peni0002550i0001263#lftd000#typIMGB#---#---"
t = tokens_to_dict(parse_tokens(hdr))
assert t == {"pen": [[2550, 1263]], "lft": [[0]], "typ": [["IMGB"]]}, t
assert tokens_to_dict(parse_tokens(b"#CRPRANGi-000020d020"))["CRP"] == [["RANG", -20, 20]]
assert tokens_to_dict(parse_tokens(b"#ERRADF PE  ")) == {"ERR": [["ADF ", "PE  "]]}

# minimal JPEG: SOI, APP0(len 4), SOF0 with height 3600
jpg = b"\xff\xd8" + b"\xff\xe0\x00\x04\x00\x00" + b"\xff\xc0\x00\x0b\x08" + (3600).to_bytes(2, "big") + b"\x09\xf6\x01\x01\x11\x00" + b"\xff\xd9"
out = fix_jpeg_height(jpg, 1263)
assert out[13:15] == (1263).to_bytes(2, "big") and len(out) == len(jpg)  # SOF at 8, height at +5
from ffscan.ff680w import holder

assert holder(bytes.fromhex("0104000000")) is None  # free
assert holder(bytes.fromhex("0104810001010110fe80000000000000000000000000abcd")) == "fe80::abcd"
assert holder(bytes.fromhex("01048100010101040a000002")) == "10.0.0.2"
from ffscan.ff680w import acq_width

assert acq_width(8.5, 600) == 5100 and acq_width(8.5, 300) == 2550  # full width, as always sent
assert acq_width(6.5, 600) == 3900 and acq_width(20, 600) == 5100 and acq_width(1, 300) == 600  # clamped 2-8.5 in
from ffscan.ff680w import _longest_run
F, T = False, True
assert _longest_run([T, F, F, F, F, F, T, T, T, F, T, T, F, F, F, F, F, T]) == (6, 12)  # edge specks lose, 1-gap bridged
assert _longest_run([F, F]) is None
import struct
from ffscan.ff680w import find_photo_box


def bmp(w, h, pixel):
    """Top-down 24-bit BMP built from pixel(x, y) -> (r, g, b)."""
    stride = (w * 3 + 3) & ~3
    rows = b"".join(bytes(c for x in range(w) for c in pixel(x, y)[::-1]).ljust(stride, b"\0") for y in range(h))
    return b"BM" + struct.pack("<IHHI", 54 + len(rows), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 24, 0, len(rows), 0, 0, 0, 0) + rows


GREY, PAPER, BAND, FILL = (176, 172, 165), (240, 240, 240), (90, 80, 60), (255, 255, 255)


def scan(x, y):  # 100x80: photo at x 20..80, y 5..50; dark full-width band below it at y 60..75; white fill corner
    if 20 <= x < 80 and 5 <= y < 50:
        return PAPER
    if 60 <= y < 75:
        return BAND
    if x > 90 and y < 10:
        return FILL
    return GREY


assert find_photo_box(bmp(100, 80, scan)) == (20, 5, 80, 50), find_photo_box(bmp(100, 80, scan))

# 300 dpi raw scans: the outermost columns differ from the backing; must not become the "background" colour
STRIP = (40, 40, 45)
edge_strip = lambda x, y: STRIP if x < 1 or x > 98 else scan(x, y)
assert find_photo_box(bmp(100, 80, edge_strip)) == (20, 5, 80, 50), find_photo_box(bmp(100, 80, edge_strip))
from PIL import Image

import tempfile

# skewed print with a shadow along one edge: straightened, and no backing grey left in the crop
from ffscan.ff680w import autocrop

with tempfile.TemporaryDirectory() as tmp:
    photo = Image.new("RGB", (1235, 800), (120, 118, 114))  # 35 px shadow along the right edge
    for i in range(0, 1200, 40):
        photo.paste((150 + i % 80, 60, 40), (i, 0, i + 40, 800))  # vertical colour stripes
    scan = Image.new("RGB", (1800, 1100), GREY)
    tilted = photo.rotate(1.0, Image.BICUBIC, expand=True)
    scan.paste(tilted, (250, 140), Image.new("L", photo.size, 255).rotate(1.0, expand=True))
    scan.save(Path(tmp) / "s.jpg", quality=95)
    w, h = autocrop(str(Path(tmp) / "s.jpg"))
    assert 1140 < w <= 1200 and 760 < h <= 800, (w, h)
    out = Image.open(Path(tmp) / "s.jpg").convert("RGB")
    for x, y in ((3, 3), (w - 4, 3), (3, h - 4), (w - 4, h - 4), (w - 4, h // 2)):
        r, g, b = out.getpixel((x, y))
        assert r - b > 40, ((x, y), (r, g, b))  # photo red, not neutral backing or shadow

# cool-grey backing (not symmetric in R/B: catches BMP's blue-green-red order) and pale sky at the photo's edge,
# about as bright as the backing but clearly blue: the sky is photo, the crop keeps it
with tempfile.TemporaryDirectory() as tmp:
    COOL, SKY = (164, 172, 177), (150, 178, 190)  # sky: within 40 of the backing, but chroma 40 vs 13
    photo = Image.new("RGB", (1200, 800), SKY)
    for i in range(300, 1200, 40):
        photo.paste((150 + i % 80, 60, 40), (i, 0, i + 40, 800))  # picture right of a 300 px band of sky
    scan = Image.new("RGB", (1800, 1100), COOL)
    scan.paste(photo, (300, 150))
    scan.save(Path(tmp) / "sky.jpg", quality=95)
    w, h = autocrop(str(Path(tmp) / "sky.jpg"))
    assert 1170 < w <= 1200 and 770 < h <= 800, (w, h)

from ffscan.enhance import remove_red_eye

# red pupil inside the eye circle is neutralised; skin-coloured and out-of-circle pixels are untouched
eye = Image.new("RGB", (20, 20), (200, 150, 130))       # skin
eye.putpixel((10, 10), (220, 40, 40))                   # red pupil
eye.putpixel((1, 1), (220, 40, 40))                     # red outside the eye circle
remove_red_eye(eye, [(10, 10, 4)])
assert eye.getpixel((10, 10)) == (40, 40, 40), eye.getpixel((10, 10))
assert eye.getpixel((12, 10)) == (200, 150, 130) and eye.getpixel((1, 1)) == (220, 40, 40)

from ffscan import dupes

# same print rotated / resized / recompressed is a duplicate; a different print is not
import random


def blocks(seed):  # 12x8 random grey blocks, 10 px each: stands in for a photo
    rnd, im = random.Random(seed), Image.new("RGB", (120, 80))
    for bx in range(12):
        for by in range(8):
            v = rnd.randrange(256)
            im.paste((v, v, v), (bx * 10, by * 10, bx * 10 + 10, by * 10 + 10))
    return im


grad, other = blocks(1), blocks(2)
h = lambda im: [dupes.dhash(im.rotate(a, expand=True)) for a in (0, 90, 180, 270)]
assert dupes.distance(h(grad.rotate(90, expand=True).resize((60, 90))), h(grad)) <= dupes.THRESHOLD
assert dupes.distance(h(other), h(grad)) > dupes.THRESHOLD

import tempfile

from ffscan import app

# file names: prefix tokens, unsafe characters, numbering continues after existing files (never overwrite)
with tempfile.TemporaryDirectory() as tmp:
    s = {**app.DEFAULTS, "folder": tmp, "metadata": {**app.DEFAULTS["metadata"], "album": "Summer 87"}}
    assert app.file_prefix({**s, "name_prefix": ""}, "20260101-120000") == "20260101-120000"
    assert app.file_prefix({**s, "name_prefix": "{album}"}, "x") == "Summer 87"
    assert app.file_prefix({**s, "name_prefix": "a/b:c"}, "x") == "a-b-c"
    album = Path(tmp) / "Summer 87"
    (album / "Enhanced").mkdir(parents=True)
    for name in ("Summer 87_001.jpg", "Summer 87_002_back.jpg", "Enhanced/Summer 87_007_enhanced.jpg", "Other_050.jpg"):
        (album / name).write_bytes(b"")
    assert app.next_number(album, "Summer 87") == 7, app.next_number(album, "Summer 87")
    assert app.next_number(album, "New") == 0

    # enhanced copies: each location round-trips back to its original
    original = album / "Summer 87_001.jpg"
    other = Path(tmp) / "elsewhere"
    for loc in ("same", "subfolder", "folder"):
        st = {**s, "enhance_location": loc, "enhance_folder": str(other)}
        e = app.enhanced_path(original, st, create=True)
        e.write_bytes(b"")
        assert app.original_of(e, st) == original, (loc, e)
    assert app.enhanced_path(original, {**s, "enhance_location": "subfolder"}).parent.name == "Enhanced"

    # split layout: <album>/Originals/x.jpg <-> <album>/Enhanced/x_enhanced.jpg
    (album / "Originals").mkdir()
    kept = album / "Originals" / "Summer 87_003.jpg"
    kept.write_bytes(b"")
    st = {**s, "enhance_location": "split"}
    e = app.enhanced_path(kept, st, create=True)
    assert e == album / "Enhanced" / "Summer 87_003_enhanced.jpg", e
    e.write_bytes(b"")
    assert app.original_of(e, st) == kept
    assert app.next_number(album, "Summer 87") == 7  # numbering sees Originals/ and Enhanced/ too

# FastFoto import: originals with their backs; Epson's enhanced "_a" copies only when there's no original
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "Al").mkdir()
    for n in ("F_0001.jpg", "F_0001_a.jpg", "F_0002.jpg", "F_0002_b.jpg", "Al/F_0001.jpg", "Al/F_0001_a.jpg", "G_0003_a.jpg"):
        (d / n).write_bytes(b"")
    found = [(str(p.relative_to(d)), b and str(b.relative_to(d))) for p, b in app.fastfoto_photos(d)]
    assert found == [("Al/F_0001.jpg", None), ("F_0001.jpg", None), ("F_0002.jpg", "F_0002_b.jpg"), ("G_0003_a.jpg", None)], found

# dates written on the backs of prints
assert app.date_from_text("Seaside, summer 1986") == "1986"
assert app.date_from_text("14.7.1986 grandma") == "1986-07-14"       # day first (European)
assert app.date_from_text("7/14/86") == "1986-07-14"                 # impossible as day-first -> US order
assert app.date_from_text("08/1992") == "1992-08"
assert app.date_from_text("no date here, 504 1111 N N") == ""

# what counts as handwriting on a back: real OCR output from Kodak/Agfa paper backs must be dropped
for noise in ["Kodak", "5041111 NN N-12", "Kod", "•Kodak", "MADE IN GERMANY", "QUALITY PAPER", "AGRA", "akl",
              "JAL3145(AX(NO 25 )058", "073 01** N N N N-232 (038)", "L3145CAXCNO.4A 2009", "[Kodak", "dak"]:
    assert app.human_text(noise) == "", noise
for writing in ["Living room, 14.7.1986", "Seaside, summer 1986", "Mom 1986", "08/1992", "Dana and Yoni"]:
    assert app.human_text(writing) == writing, writing

# durable job queue: pending work survives a "restart" (a new store on the same file), finished work doesn't return
from ffscan import jobs

with tempfile.TemporaryDirectory() as tmp:
    store = jobs.JobStore(Path(tmp) / "jobs.db")
    a = store.put("upload", {"path": "a.jpg"})
    store.put("upload", {"path": "b.jpg"})
    store.finish(a)
    restarted = jobs.JobStore(Path(tmp) / "jobs.db")
    assert [p["path"] for _, p in restarted.pending("upload")] == ["b.jpg"]
    assert restarted.queue("upload").get_nowait()[1] == {"path": "b.jpg"}
    assert restarted.counts() == {"upload": 1}

from ffscan.photo_ai import to_metadata

r = {"rotation": 0, "caption": "Kids at the beach.", "tags": ["beach"], "date_estimate": "1980s",
     "date_confidence": "medium", "place": "", "back_text": "Seaside 1986", "back_date": "1986"}
assert to_metadata(r, {}) == {"description": "Kids at the beach.\nWritten on back: Seaside 1986", "date": "1986", "tags": "beach"}
assert to_metadata(r, {"description": "typed", "date": "1990"}) == {"tags": "beach"}  # never overwrite the user
assert to_metadata({**r, "back_text": "", "back_date": ""}, {})["tags"] == "beach, c. 1980s"  # decade -> tag
# cloud web links: Dropbox from the path alone; providers without a file page give None
from ffscan import cloud

cloud.remote_type = lambda name: {"db": "dropbox", "s3": "s3"}[name]
assert cloud.web_url("db:FastFoto/Summer 87/x 1.jpg") == "https://www.dropbox.com/home/FastFoto/Summer%2087?preview=x%201.jpg"
assert cloud.web_url("s3:bucket/x.jpg") is None
# Imported photos may carry GPS: enhanced copies keep it, and gps_of reads it for the AI analysis
import shutil
if shutil.which("exiftool"):
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "x.jpg", Path(d) / "x_enhanced.jpg"
        Image.new("RGB", (64, 48), (120, 90, 60)).save(src)
        assert app.gps_of(src) is None
        app.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=49.7075", "-GPSLatitudeRef=N",
                 "-GPSLongitude=0.2034", "-GPSLongitudeRef=W", str(src)])
        assert app.gps_of(src) == (49.7075, -0.2034)
        app.enhance(src, dst, {"enhance_opts": {"red_eye": False, "restore": True, "auto": True}})
        assert app.gps_of(dst) == (49.7075, -0.2034)

print("ok")

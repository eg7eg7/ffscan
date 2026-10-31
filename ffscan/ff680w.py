"""Epson FastFoto FF-680W client: ESC/I-2 over Epson's "IS" network protocol (TCP 1865).

    with FF680W() as sc:                       # host found automatically via Bonjour
        print(sc.info(), sc.status())
        for page in sc.scan(dpi=600):
            open(f"{page.index:02d}{page.side}.jpg", "wb").write(page.jpeg)

CLI: python -m ffscan.ff680w [--host H] info|capa|status|scan [scan options]
See docs/PROTOCOL.md for the wire format.
"""
import argparse
import math
import os
import re
import socket
import struct
import time
from collections import namedtuple

Page = namedtuple("Page", "index side width height jpeg")  # side "A" = paper back, "B" = picture (face-up load)

# ---------------------------------------------------------------- IS transport


def _recv_exact(s, n):
    buf, got = bytearray(n), 0  # preallocated: image blocks can be large
    view = memoryview(buf)
    while got < n:
        k = s.recv_into(view[got:], n - got)
        if not k:
            raise ConnectionError(f"scanner closed connection after {got}/{n} bytes")
        got += k
    return bytes(buf)


def _send_packet(s, ptype, payload=b""):
    # "IS" type(u16) 00 0C len(u32) 00 00 payload
    s.sendall(b"IS" + struct.pack(">H", ptype) + b"\x00\x0c" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload)


def _recv_packet(s):
    hdr = _recv_exact(s, 12)
    if hdr[:2] != b"IS":
        raise ConnectionError(f"bad IS header {hdr.hex(' ')}")
    size = struct.unpack(">I", hdr[6:10])[0]
    return struct.unpack(">H", hdr[2:4])[0], _recv_exact(s, size) if size else b""


# ---------------------------------------------------------------- ESC/I-2 tokens


def parse_tokens(data):
    """Parse '#KEY' + values into [(key, [values])].

    Values: d+3 digits, i+7 digits (may be negative), x+7 hex -> int;
    h+3 hex length + bytes (padded to 4) -> bytes; anything else -> 4-char str.
    """
    out, i = [], 0
    while i < len(data):
        if data[i : i + 1] != b"#":
            i += 1
            continue
        key, i, vals = data[i + 1 : i + 4].decode("latin-1"), i + 4, []
        while i < len(data) and data[i : i + 1] != b"#":
            t = data[i : i + 1]
            if t == b"d" and data[i + 1 : i + 4].isdigit():
                vals.append(int(data[i + 1 : i + 4])); i += 4
            elif t == b"i" and re.fullmatch(rb"-?\d+", data[i + 1 : i + 8]):
                vals.append(int(data[i + 1 : i + 8])); i += 8
            elif t == b"x" and re.fullmatch(rb"[0-9A-F]{7}", data[i + 1 : i + 8]):
                vals.append(int(data[i + 1 : i + 8], 16)); i += 8
            elif t == b"h" and re.fullmatch(rb"[0-9A-F]{3}", data[i + 1 : i + 4]):
                n = int(data[i + 1 : i + 4], 16)
                vals.append(data[i + 4 : i + 4 + n]); i += 4 + n + (-n % 4)
            else:
                vals.append(data[i : i + 4].decode("latin-1")); i += 4
        if key != "---":
            out.append((key, vals))
    return out


def tokens_to_dict(tokens):
    """Group repeated keys: {'ADF': [['TYPE','FEED'], ...], 'PRD': [[b'FF-680W']]}."""
    d = {}
    for k, v in tokens:
        d.setdefault(k, []).append(v)
    return d


def fix_jpeg_height(jpg, height):
    """The scanner writes the requested height into SOF; patch in the real one from #pen."""
    i = 2
    while i < len(jpg) - 9:
        if jpg[i] != 0xFF:
            raise ValueError(f"bad JPEG marker at {i}")
        marker, seg_len = jpg[i + 1], int.from_bytes(jpg[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):  # SOF: FF Cx len(2) precision(1) height(2) width(2)
            return jpg[: i + 5] + height.to_bytes(2, "big") + jpg[i + 7 :]
        i += 2 + seg_len
    return jpg


def find_photo_box(bmp, threshold=40):
    """Bounding box (x0, y0, x1, y1) of the photo in a 24-bit BMP, against the scanner's flat grey backing.

    Background colour is sampled just inside the left/right edges. A row/column counts as photo when
    more than 5% of its pixels differ from the background by > threshold (sum of RGB deltas) and are
    not pure-white deskew fill.
    """
    w, h, px = _bmp_pixels(bmp)
    bg = _background(bmp)
    def is_fg(p):  # pure white (>= 250) is the scanner's deskew corner fill; photo paper measures <= 246
        return sum(abs(a - b) for a, b in zip(p, bg)) > threshold and min(p) < 250

    fg = [[is_fg(px(x, y)) for x in range(w)] for y in range(h)]
    # rows first, then columns counted only inside those rows (and refine rows inside the columns):
    # a full-width band above/below the photo (seen on back-side scans) would otherwise mark every column
    rows = _longest_run([sum(fg[y]) > w * 0.05 for y in range(h)])
    if not rows:
        return 0, 0, w, h
    cols = _longest_run([sum(fg[y][x] for y in range(*rows)) > (rows[1] - rows[0]) * 0.3 for x in range(w)])
    if not cols:
        return 0, 0, w, h
    rows = _longest_run([sum(fg[y][cols[0]:cols[1]]) > (cols[1] - cols[0]) * 0.3 for y in range(h)]) or rows
    return cols[0], rows[0], cols[1], rows[1]


def _background(bmp):
    """Median colour of the scanner backing, sampled 1.5-3% in from the left and right edges: the outermost
    columns of 300 dpi scans are a different colour than the backing, and even an 8" photo leaves this clear."""
    w, h, px = _bmp_pixels(bmp)
    band = range(int(w * 0.015), max(int(w * 0.03), int(w * 0.015) + 1))
    edge = [px(x, y) for y in range(h) for x in [*band, *(w - 1 - x for x in band)]]
    return [sorted(p[c] for p in edge)[len(edge) // 2] for c in range(3)]


def skew_angle(im, bg, threshold=40, size=1500):
    """Degrees to rotate `im` (Pillow, counter-clockwise positive) so the photo's edges are straight, or 0.

    Each edge of the photo that doesn't touch the image border is traced in thin strips of a foreground mask
    (differs from the backing like find_photo_box, specks eroded); its slope is the median over pairs of
    points half the edge apart, so a stretch of sky the colour of the backing doesn't skew it.
    """
    from PIL import Image, ImageChops, ImageFilter

    small = im.convert("RGB")
    small.thumbnail((size, size))
    r, g, b = ImageChops.difference(small, Image.new("RGB", small.size, tuple(bg))).split()
    differs = ImageChops.add(ImageChops.add(r, g), b).point(lambda v: 255 if v > threshold else 0)
    not_fill = ImageChops.darker(ImageChops.darker(*small.split()[:2]), small.split()[2]).point(lambda v: 255 if v < 250 else 0)
    mask = ImageChops.multiply(differs, not_fill).filter(ImageFilter.MinFilter(5))
    box = mask.getbbox()
    if not box:
        return 0.0
    w, h = mask.size
    x0, y0, x1, y1 = box
    slopes = []
    for vertical in (True, False):  # left/right edges: x along y; top/bottom edges: y along x
        lo, hi = (y0, y1) if vertical else (x0, x1)
        steps = range(int(lo + 0.15 * (hi - lo)), int(hi - 0.15 * (hi - lo)), 4)
        near, far = [], []
        for t in steps:
            strip = mask.crop((0, t, w, t + 4) if vertical else (t, 0, t + 4, h)).getbbox()
            if strip:
                a, b_ = (strip[0], strip[2]) if vertical else (strip[1], strip[3])
                near.append((t, a))
                far.append((t, b_))
        limit = w if vertical else h
        for pts in (near, far):
            if len(pts) < 10 or min(p for _, p in pts) <= 2 or max(p for _, p in pts) >= limit - 2:
                continue  # edge touches the image border (cut off by the scan): can't trace it
            half = len(pts) // 2
            pair = sorted((pts[i + half][1] - pts[i][1]) / (pts[i + half][0] - pts[i][0]) for i in range(half))
            s = pair[len(pair) // 2]
            slopes.append(-s if vertical else s)  # x drifting right going down = clockwise tilt
    if not slopes:
        return 0.0
    slopes.sort()
    angle = math.degrees(math.atan(slopes[len(slopes) // 2]))
    return angle if 0.1 <= abs(angle) <= 5 else 0.0  # below 0.1 deg: under a pixel per 600; above 5: not skew


def trim_backing(im, box, bg, step=4, max_share=0.03):
    """Move each side of `box` inward past lines that are flat grey: the backing, or the shadow along the
    print's edge (measured: std <= 5, near-neutral, no brighter than the backing; picture lines vary 30-70).
    A white print border is brighter than the backing and stays. At most 3% per side."""
    from PIL import ImageStat

    luma = sum(bg) / 3

    def flat(strip):
        st = ImageStat.Stat(im.crop(strip).convert("RGB"))
        return sum(st.stddev) / 3 < 8 and max(st.mean) - min(st.mean) < 15 and sum(st.mean) / 3 < luma + 25

    x0, y0, x1, y1 = box
    mx, my = int((x1 - x0) * max_share), int((y1 - y0) * max_share)
    iy0, iy1, ix0, ix1 = y0 + (y1 - y0) // 10, y1 - (y1 - y0) // 10, x0 + (x1 - x0) // 10, x1 - (x1 - x0) // 10
    while x0 < box[0] + mx and flat((x0, iy0, x0 + step, iy1)):
        x0 += step
    while x1 > box[2] - mx and flat((x1 - step, iy0, x1, iy1)):
        x1 -= step
    while y0 < box[1] + my and flat((ix0, y0, ix1, y0 + step)):
        y0 += step
    while y1 > box[3] - my and flat((ix0, y1 - step, ix1, y1)):
        y1 -= step
    return x0, y0, x1, y1


def inside(valid, box, step=2):
    """Shrink `box` until its every edge line lies on pixels where the `valid` mask is 255."""
    x0, y0, x1, y1 = box
    bad = lambda b: valid.crop(b).getextrema()[0] < 255
    while x1 - x0 > 2 * step and y1 - y0 > 2 * step:
        sides = [bad((x0, y0, x0 + 1, y1)), bad((x1 - 1, y0, x1, y1)), bad((x0, y0, x1, y0 + 1)), bad((x0, y1 - 1, x1, y1))]
        if not any(sides):
            break
        x0, x1 = x0 + step * sides[0], x1 - step * sides[1]
        y0, y1 = y0 + step * sides[2], y1 - step * sides[3]
    return x0, y0, x1, y1


def _longest_run(flags, gap=3):
    """(start, end) of the longest run of True, bridging gaps <= `gap`; thin edge artefacts lose to the photo."""
    best, start, last = None, None, None
    for i, f in enumerate(flags + [False] * (gap + 1)):
        if f:
            if start is None or i - last > gap + 1:
                start = i
            last = i
        elif start is not None and i - last > gap:
            if best is None or last + 1 - start > best[1] - best[0]:
                best = (start, last + 1)
            start = None
    return best


def _bmp_pixels(bmp):
    """(width, height, px(x, y) -> 3 bytes) for an uncompressed 24/32-bit BMP, either row order."""
    off, w, h = struct.unpack_from("<I", bmp, 10)[0], *struct.unpack_from("<ii", bmp, 18)
    bpp = struct.unpack_from("<H", bmp, 28)[0] // 8
    stride, top_down, h = (w * bpp + 3) & ~3, h < 0, abs(h)

    def px(x, y):
        o = off + (y if top_down else h - 1 - y) * stride + x * bpp
        return bmp[o : o + 3]

    return w, h, px


def _thumbnail(path, size=600):
    """Downscaled BMP bytes of an image file (or Pillow image), in its stored orientation (crop boxes are in stored pixels)."""
    import io

    from PIL import Image

    im = Image.open(path) if isinstance(path, (str, os.PathLike)) else path.copy()
    im.draft("RGB", (size, size))  # JPEG: decode at reduced size, fast
    im = im.convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "BMP")
    return buf.getvalue()


def ink_fraction(bmp, threshold=90, border=0.04):
    """Share of pixels clearly darker/different than the paper colour, ignoring a 4% border.

    Blank photo backs (incl. faint maker watermarks) score ~0; handwriting or stamps score higher.
    """
    w, h, px = _bmp_pixels(bmp)
    bx, by = int(w * border), int(h * border)
    pixels = [px(x, y) for y in range(by, h - by, 2) for x in range(bx, w - bx, 2)]
    paper = [sorted(p[c] for p in pixels)[len(pixels) // 2] for c in range(3)]
    return sum(sum(abs(a - b) for a, b in zip(p, paper)) > threshold for p in pixels) / max(1, len(pixels))


def autocrop(path, margin=2):
    """Crop a saved scan to the photo, in place; returns (width, height).

    Re-encodes once at JPEG quality 95 and keeps EXIF, XMP, ICC profile and DPI.
    """
    from PIL import Image

    im = Image.open(path)
    keep = {k: im.info[k] for k in ("exif", "xmp", "icc_profile", "dpi") if im.info.get(k)}
    bmp = _thumbnail(im)
    # straighten first: a print that went through at a slight angle otherwise leaves wedges of backing
    angle, scanned = skew_angle(im, bg := _background(bmp)), None
    if angle:
        scanned = Image.new("L", im.size, 255).rotate(angle, expand=True)  # 0 where rotation filled the corners
        im = im.convert("RGB").rotate(angle, Image.BICUBIC, expand=True, fillcolor=tuple(bg))
        bmp = _thumbnail(im)
    thumb_w = struct.unpack_from("<i", bmp, 18)[0]
    scale = im.width / thumb_w
    # shrink by margin (thumbnail px) to drop the soft edge, then scale to full resolution
    bx0, by0, bx1, by1 = find_photo_box(bmp)
    box = tuple(int(v * scale) for v in (bx0 + margin, by0 + margin, bx1 - margin, by1 - margin))
    box = trim_backing(im, box, bg)
    if scanned:  # a print cut off by the scan's edge: keep the rotated fill wedges out of the crop
        box = inside(scanned, box)
    im.crop(box).save(path, "JPEG", quality=95, **keep)
    return box[2] - box[0], box[3] - box[1]


class ScannerError(Exception):
    pass


# ---------------------------------------------------------------- client


_discovered = {}


def discover(wait=3.0, refresh=False):
    """Address of the first Epson scanner on the local network, or None. Works whatever IP the scanner has:

    1. Bonjour `_scanner._tcp` (macOS `dns-sd`): finds it by name, so a new DHCP address is picked up.
    2. Fallback for networks that block multicast (some mesh/guest Wi-Fi): probe every address in this
       Mac's /24 subnet on TCP 1865 and keep the one that greets with an Epson "IS" hello packet.

    Cached for the process (browsing takes a few seconds); `refresh=True` forgets the cached address.
    """
    if refresh:
        _discovered.clear()
    if "host" not in _discovered:
        host = _bonjour(wait) or _scan_subnet()
        if not host:
            return None
        _discovered["host"] = host
    return _discovered["host"]


def _bonjour(wait):
    import shutil
    import subprocess

    if not shutil.which("dns-sd"):  # macOS tool; absent in Docker/Linux -> subnet scan instead
        return None

    def run_for(args):
        p = subprocess.Popen(["dns-sd", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(wait)
        p.terminate()
        return p.communicate()[0]

    # browse lines: "<time>  Add  <flags> <if> local.  _scanner._tcp.  <instance name>"
    names = [line.split(None, 6)[6] for line in run_for(["-B", "_scanner._tcp", "local."]).splitlines()
             if " Add " in line and len(line.split(None, 6)) == 7 and "EPSON" in line.upper()]
    if not names:
        return None
    m = re.search(r"can be reached at (\S+?)\.?:(\d+)", run_for(["-L", names[0], "_scanner._tcp", "local."]))
    return m.group(1) if m else None


def _local_ipv4():
    """This Mac's LAN address (no packet is sent: UDP connect only picks the outgoing interface)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # documentation address; never actually contacted
            return s.getsockname()[0]
        except OSError:
            return None


def _is_epson_scanner(ip, port=1865, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(1.0)
            hello = s.recv(12)
            return hello[:2] == b"IS" and hello[2:4] == b"\x80\x00"  # scanner speaks first: 0x8000 hello
    except OSError:
        return False


def _scan_subnet():
    from concurrent.futures import ThreadPoolExecutor

    me = _local_ipv4()
    # FFSCAN_SUBNET: the LAN to search when this process can't see it (e.g. Docker Desktop's VM network)
    prefix = os.environ.get("FFSCAN_SUBNET", "").rstrip(".") or (me.rsplit(".", 1)[0] if me else "")
    if not prefix or prefix.startswith("127"):
        return None  # ponytail: /24 only; larger subnets need Bonjour or an explicit address
    candidates = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != me]
    with ThreadPoolExecutor(64) as pool:
        for ip, ok in zip(candidates, pool.map(_is_epson_scanner, candidates)):
            if ok:
                return ip
    return None


def acq_width(width_in, dpi):
    """Scan width in pixels, clamped to the feeder's 2-8.5 in."""
    return int(max(2.0, min(8.5, width_in)) * dpi)


def _connect(host, port, timeout):
    """TCP connection, IPv4 first. Bonjour names also resolve to IPv6 link-local addresses, which fail with
    "No route to host" without an interface scope, and a session over IPv6 left a lock we couldn't see."""
    try:
        addrs = [a[4][0] for a in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)]
    except socket.gaierror:
        addrs = []
    for ip in addrs:
        try:
            return socket.create_connection((ip, port), timeout=timeout)
        except OSError:
            pass
    return socket.create_connection((host, port), timeout=timeout)


def holder(hello):
    """Address of the client that holds the scanner, from its 0x8000 hello, or None when it's free.
    Busy hello payload: 01 04 81 00 01 01 01 <address length> <address bytes>."""
    if len(hello) < 8 or len(hello) < 8 + hello[7] or hello[7] not in (4, 16):
        return None
    family = socket.AF_INET if hello[7] == 4 else socket.AF_INET6
    return socket.inet_ntop(family, hello[8:8 + hello[7]])


class FF680W:
    LOCK = bytes([0x01, 0xA0, 0x04, 0x00, 0x00, 0x01, 0x2C])  # as sent by Epson's client
    BLOCK_SIZE = 1 << 20  # #BSZ image block size: 1 MiB, the same default Epson's own engine sends

    def __init__(self, host=None, port=1865, timeout=60, verbose=False):
        self.host, self.port, self.timeout, self.verbose = host, port, timeout, verbose
        self.sock = None

    def __enter__(self):
        auto = not self.host
        if auto:
            self.host = discover()
            if not self.host:
                raise ScannerError("no Epson scanner found on the network; is it on and on the same Wi-Fi?")
        try:
            self.sock = _connect(self.host, self.port, self.timeout)
        except OSError:
            if not auto:
                raise
            self.host = discover(refresh=True)  # the scanner may have a new address since it was found
            if not self.host:
                raise ScannerError("the scanner is no longer on the network; is it on?")
            self.sock = _connect(self.host, self.port, self.timeout)
        # request/reply protocol with small requests: don't let Nagle's algorithm hold them back
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _, hello = _recv_packet(self.sock)  # 0x8000 hello
        _send_packet(self.sock, 0x2100, self.LOCK)
        try:
            locked = _recv_packet(self.sock) == (0xA100, b"\x06")
        except OSError:  # a busy scanner resets the connection instead of answering
            locked = False
        if not locked:
            who = holder(hello)
            raise ScannerError(f"the scanner is in use by {who or 'another client'}. If nothing else is scanning, "
                               "a previous session didn't release it: switch the scanner off and on.")
        if self._xfer(b"\x1cX", 1) != b"\x06":
            raise ScannerError("scanner did not enter ESC/I-2 mode")
        return self

    def __exit__(self, *exc):
        try:
            self.command("FIN ")
        finally:
            _send_packet(self.sock, 0x2101)
            _recv_packet(self.sock)
            self.sock.close()

    def _xfer(self, data, reply_len):
        """Wrap ESC/I bytes in a 0x2000 packet; the scanner sends at most reply_len back."""
        _send_packet(self.sock, 0x2000, struct.pack(">II", len(data), reply_len) + data)
        ptype, payload = _recv_packet(self.sock)
        if ptype != 0xA000:
            raise ScannerError(f"unexpected packet type 0x{ptype:04x}")
        return payload

    def command(self, cmd, params=b""):
        """Send a 4-char command (+param block); return (64-byte reply header, body)."""
        hdr = self._xfer(cmd.encode() + b"x%07X" % len(params) + params, 64)
        body_len = int(hdr[5:12], 16)
        parts, got = [], 0
        while got < body_len:
            chunk = self._xfer(b"", body_len - got)
            parts.append(chunk)
            got += len(chunk)
        body = b"".join(parts)
        if self.verbose:
            print(f"{cmd}: {hdr!r} body[{body_len}]")
        return hdr, body

    def _query(self, cmd):
        return tokens_to_dict(parse_tokens(self.command(cmd)[1]))

    def info(self):
        return self._query("INFO")

    def capabilities(self):
        return self._query("CAPA")

    def status(self):
        return self._query("STAT")

    def scan(self, dpi=300, color="C024", duplex=True, deskew=False,
             double_feed=None, jpeg_quality=90, gamma="UG18", max_length_in=36, width_in=8.5, poll_timeout=60):
        """Feed every photo in the tray; yield Page per side as each finishes.

        color: C024 (24-bit colour), M008 (8-bit grey). double_feed: None, 1 or 3 (DFL1/DFL3).
        dpi: one of 50 75 100 150 200 240 300 360 400 600 (600 = optical max).
        width_in: width of the strip read, centred like the feeder (ALGN CNTR); narrower = less data to send.
        """
        err = self.status().get("ERR")
        if err:
            raise ScannerError(f"scanner not ready: {err} (PE = feeder empty, PJ = jam)")

        adf = "DPLX" * duplex + "PEDT" + "SKEW" * deskew  # "CRP " is accepted but has no effect; use autocrop()
        if double_feed:
            adf += f"DFL{double_feed}"
        width, length = acq_width(width_in, dpi), int(max_length_in * dpi)
        left = (int(8.5 * dpi) - width) // 2  # prints are centred in the feeder
        params = (
            f"#ADF{adf}#COL{color}#FMTJPG #JPGd{jpeg_quality:03d}#GMM{gamma}"
            f"#RSMi{dpi:07d}#RSSi{dpi:07d}#ACQi{left:07d}i0000000i{width:07d}i{length:07d}"
        ).encode()
        # image block size. Without #BSZ the scanner sends 64 KB blocks, each costing two Wi-Fi round trips
        # (~8 s per 600 dpi side). Epson's engine sends BufferSizeValue from the model file, default 0x100000
        # (SDEModelInfo -bufferSize). Fall back without it for firmware that rejects the key.
        hdr, _ = self.command("PARA", params + b"#BSZi%07d" % self.BLOCK_SIZE)
        self.block_size_accepted = b"#parOK" in hdr
        if not self.block_size_accepted:
            hdr, _ = self.command("PARA", params)
        if b"#parOK" not in hdr:
            raise ScannerError(f"settings rejected: {parse_tokens(hdr[12:])}")

        self.command("TRDT")
        index, parts, last = 0, {}, time.monotonic()
        # per side: blocks received, largest block, seconds waiting (empty replies) vs moving data
        self.side_stats = {}
        try:
            while True:
                t0 = time.monotonic()
                hdr, block = self.command("IMG ")
                spent = time.monotonic() - t0
                if hdr[:4] == b"INVD":
                    return  # transfer finished
                t = tokens_to_dict(parse_tokens(hdr[12:]))
                side = "B" if ["IMGB"] in t.get("typ", []) else "A"
                parts.setdefault(side, []).append(block)
                st = self.side_stats.setdefault(side, {"blocks": 0, "max_block": 0, "wait_s": 0.0, "transfer_s": 0.0})
                if block:
                    st["blocks"] += 1
                    st["max_block"] = max(st["max_block"], len(block))
                    st["transfer_s"] += spent
                else:
                    st["wait_s"] += spent
                if "err" in t:
                    raise ScannerError(f"scan error: {t['err']}")
                if "pen" in t:
                    w, h = t["pen"][0]
                    self.last_side_stats = self.side_stats.pop(side, {})
                    yield Page(index, side, w, h, fix_jpeg_height(b"".join(parts.pop(side)), h))
                    if side == "B" or not duplex:
                        index += 1
                    if t.get("lft") == [[0]] and not parts:
                        return
                if block or "pst" in t or "pen" in t:
                    last = time.monotonic()
                elif time.monotonic() - last > poll_timeout:
                    raise ScannerError(f"no data for {poll_timeout}s")
                else:
                    time.sleep(0.05)  # photo still feeding
        except BaseException:
            self.command("CAN ")
            raise


# ---------------------------------------------------------------- CLI


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("FF680W_HOST"), help="scanner address (default: find it via Bonjour)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("info", "capa", "status"):
        sub.add_parser(name)
    sc = sub.add_parser("scan")
    sc.add_argument("--dpi", type=int, default=300)
    sc.add_argument("--gray", action="store_true")
    sc.add_argument("--picture-only", action="store_true", help="save side B (the picture) only, skip photo backs")
    sc.add_argument("--no-crop", action="store_true", help="keep the full 8.5in-wide scan (no software crop)")
    sc.add_argument("--deskew", action="store_true", help="hardware deskew")
    sc.add_argument("--double-feed", type=int, choices=(1, 3))
    sc.add_argument("--quality", type=int, default=90)
    sc.add_argument("--skip-blank-backs", nargs="?", type=float, const=0.015, metavar="INK",
                    help="drop backs with less than INK share of marks (default 0.015); keeps handwritten ones")
    sc.add_argument("--watch", action="store_true", help="after each stack, wait for the next one (Ctrl-C to stop)")
    sc.add_argument("--out", default="scans")
    a = ap.parse_args()

    if a.cmd != "scan":
        with FF680W(a.host, verbose=a.verbose) as s:
            for k, v in {"info": s.info, "capa": s.capabilities, "status": s.status}[a.cmd]().items():
                print(f"{k}: {v}")
        return

    os.makedirs(a.out, exist_ok=True)
    waiting = None
    try:
        while True:
            # new session per stack, so the scanner is free for other clients between stacks
            with FF680W(a.host, verbose=a.verbose) as s:
                err = s.status().get("ERR")
                if not err:
                    waiting = None
                    scan_stack(s, a)
                elif not a.watch:
                    raise ScannerError(f"scanner not ready: {err} (PE = feeder empty, PJ = jam)")
                elif err != waiting:
                    waiting = err
                    print("waiting for photos..." if ["ADF ", "PE  "] in err else f"scanner reports {err}", flush=True)
            if not a.watch:
                return
            time.sleep(2)
    except KeyboardInterrupt:
        print("stopped")


def scan_stack(s, a):
    """Scan everything in the feeder once; save, crop and filter pages as they arrive."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for p in s.scan(dpi=a.dpi, color="M008" if a.gray else "C024", deskew=a.deskew,
                    double_feed=a.double_feed, jpeg_quality=a.quality):
        if a.picture_only and p.side == "A":
            continue
        name = os.path.join(a.out, f"{stamp}_{p.index:02d}{p.side}.jpg")
        open(name, "wb").write(p.jpeg)
        size = f"{p.width}x{p.height}" if a.no_crop else "%dx%d (cropped)" % autocrop(name)
        if a.skip_blank_backs is not None and p.side == "A":
            ink = ink_fraction(_thumbnail(name))
            if ink < a.skip_blank_backs:
                os.remove(name)
                print(f"skipped blank back {p.index:02d}A  (ink {ink:.3f})", flush=True)
                continue
        print(f"saved {name}  {size}", flush=True)


if __name__ == "__main__":
    main()

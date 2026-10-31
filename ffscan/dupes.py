"""Duplicate photo detection with a perceptual hash (dHash), robust to resolution, small crop differences,
recompression and the print being fed in a different orientation.
"""
import json
import os
import re
import threading
from pathlib import Path

from PIL import Image

# max differing bits (of 64) to call two scans the same print. Measured on 4 rescans of an 18-print stack
# (300 vs 600 dpi, different crops): same print 0-6, different prints >= 9 (two similar room shots)
THRESHOLD = 7
SKIP_SUFFIXES = ("_back", "_enhanced")  # derivatives of a picture, not separate photos


def dhash(im):
    """64-bit difference hash: is each pixel brighter than its right neighbour, on a 9x8 greyscale thumbnail."""
    g = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = bits << 1 | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hashes(path):
    """dHash of the stored image in all 4 rotations (the same print may be fed in any way round)."""
    im = Image.open(path)
    im.draft("RGB", (256, 256))  # decode a reduced JPEG: fast
    im = im.convert("RGB")
    im.thumbnail((256, 256))
    return [dhash(im.rotate(angle, expand=True)) for angle in (0, 90, 180, 270)]


def distance(a, b):
    """Smallest Hamming distance between photo a (as stored) and any rotation of photo b."""
    return min(bin(a[0] ^ h).count("1") for h in b)


def is_picture(path):
    """Not a back or enhanced copy. Backs are '_back' (web app) or '_<n>A' (CLI); blank backs look alike."""
    stem = Path(path).stem
    return not (stem.endswith(SKIP_SUFFIXES) or re.search(r"_\d+A$", stem))


class Index:
    """{path: {"mtime": float, "h": [4 hashes]}} cached in a JSON file; hashes are recomputed when a file changes."""

    def __init__(self, file):
        self.file = Path(file)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, path):
        path, mtime = str(path), os.path.getmtime(path)
        entry = self.data.get(path)
        if not entry or entry["mtime"] != mtime:
            entry = {"mtime": mtime, "h": hashes(path)}
            with self.lock:
                self.data[path] = entry
        return entry["h"]

    def save(self):
        with self.lock:
            live = {p: e for p, e in self.data.items() if os.path.exists(p)}
            self.data = live
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(live))

    def find_duplicate(self, path, candidates):
        """The first of `candidates` (other pictures) that shows the same print as `path`, or None."""
        h = self.get(path)
        for other in candidates:
            if str(other) != str(path) and distance(h, self.get(other)) <= THRESHOLD:
                return str(other)
        return None

    def groups(self, paths):
        """Duplicate groups among `paths` (oldest first in each group); singletons left out."""
        paths = sorted((p for p in paths if is_picture(p)), key=os.path.getmtime)
        seen, out = set(), []
        for i, p in enumerate(paths):
            if p in seen:
                continue
            group = [p] + [q for q in paths[i + 1:] if q not in seen and distance(self.get(p), self.get(q)) <= THRESHOLD]
            if len(group) > 1:
                seen.update(group)
                out.append([str(x) for x in group])
        return out

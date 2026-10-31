"""Photo enhancement for scanned prints: red-eye removal, faded colour restoration, auto enhance.

All local (Pillow + Apple Vision via tools/orient). Each step is optional; originals are never modified.
"""
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps, ImageStat

ORIENT = Path(__file__).resolve().parent.parent / "tools" / "orient"


def find_eyes(path):
    """[(cx, cy, r)] in pixels of the stored image, from Apple Vision face landmarks."""
    if not ORIENT.exists():
        return []
    out = subprocess.run([str(ORIENT), "--eyes", str(path)], capture_output=True, text=True, check=True).stdout
    w, h = Image.open(path).size
    eyes = (line.split("\t") for line in out.splitlines() if line)
    return [(float(x) * w, float(y) * h, float(r) * w) for x, y, r in eyes]


def remove_red_eye(im, eyes):
    """Neutralise strongly red pixels within each eye: red becomes the mean of green and blue."""
    px = im.load()
    for cx, cy, r in eyes:
        r = max(3, r * 1.1)  # eye contour radius covers the iris; a little margin catches the glow
        for y in range(max(0, int(cy - r)), min(im.height, int(cy + r) + 1)):
            for x in range(max(0, int(cx - r)), min(im.width, int(cx + r) + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                    continue
                red, green, blue = px[x, y][:3]
                gb = (green + blue) / 2
                if red > 60 and red > 1.7 * gb:  # red-eye: red dominant, not skin (skin has red ~1.2-1.5x)
                    px[x, y] = (int(gb), green, blue)
    return im


def restore_faded(im):
    """Faded prints lose contrast unevenly per dye layer and drift to a colour cast (often magenta/red).
    Stretching each channel to full range restores contrast and neutralises the cast at the same time."""
    return Image.merge("RGB", [ImageOps.autocontrast(c, cutoff=0.5) for c in im.split()])


def auto_enhance(im, target_brightness=0.47, target_saturation=0.38):
    """Brightness, contrast and saturation, each measured and nudged toward a target rather than fixed boosts."""
    im = ImageOps.autocontrast(im, cutoff=0.5, preserve_tone=True)  # contrast, hue unchanged
    mean = ImageStat.Stat(im.convert("L")).mean[0] / 255
    if 0.02 < mean < 0.98:  # gamma that moves mean brightness toward target, limited to avoid washed-out results
        gamma = max(0.7, min(1.4, math.log(target_brightness) / math.log(mean)))
        im = im.point([round(255 * (i / 255) ** gamma) for i in range(256)] * 3)
    sat = ImageStat.Stat(im.convert("HSV").getchannel("S")).mean[0] / 255
    im = ImageEnhance.Color(im).enhance(max(1.0, min(1.3, target_saturation / max(sat, 0.05))))
    return ImageEnhance.Sharpness(im).enhance(1.15)


def enhance(src, dst, red_eye=True, restore=True, auto=True):
    """Write an enhanced copy of src to dst with the chosen steps. Returns the list of steps applied."""
    im = Image.open(src)
    dpi = im.info.get("dpi", (300, 300))
    im = im.convert("RGB")
    done = []
    if red_eye:
        eyes = find_eyes(src)
        if eyes:
            remove_red_eye(im, eyes)
            done.append(f"red-eye ({len(eyes)} eyes checked)")
    if restore:
        im = restore_faded(im)
        done.append("restored colours")
    if auto:
        im = auto_enhance(im)
        done.append("auto enhance")
    im.save(dst, quality=95, dpi=dpi)
    return done

# ffscan

**An open-source scanning app for the Epson FastFoto FF-680W photo scanner, for macOS.**

ffscan talks to the scanner directly over Wi-Fi, using a protocol worked out for interoperability (see
[docs/PROTOCOL.md](docs/PROTOCOL.md)), so you don't need Epson's FastFoto software. It runs as a small local
web app: load a stack of prints, press **Scan**, and photos appear one by one, cropped, organised into albums,
tagged and optionally uploaded to the cloud.

![ffscan main window](docs/screenshots/app.png)

## Features

- **Fast batch scanning** of up to 36 prints per stack, both sides in one pass, 50–600 dpi, colour or greyscale,
  with live thumbnails as each photo comes off the scanner.
- **Finds the scanner automatically** on your network (Bonjour, with a network-scan fallback), whatever IP
  address it has, and finds it again if the address changes.
- **Auto-crop and straighten**, so each file is just the photo, without the grey scanner background.
- **Backs of prints:** save the picture only, the back too, or skip backs that have no writing on them.
- **Enhanced copies** (originals are always kept):
  - Remove red-eye: Apple Vision finds the eyes, and only red pixels inside them are changed.
  - Restore faded colours.
  - Auto brightness, contrast and saturation.
- **Auto-rotate:** photos with faces are turned upright on-device with Apple Vision. With AI analysis on, Claude
  also rotates photos without people.
- **Metadata written into the files** (EXIF/IPTC/XMP): album, date taken, place, people, tags and a description,
  so Apple Photos, Google Photos and Lightroom can search and sort them.
- **AI analysis (optional):** Claude writes a caption and tags, estimates the year, guesses the place and
  transcribes handwriting on the back. It only fills fields you left empty.
- **Cloud upload** to Google Drive, Google Photos, Dropbox, OneDrive or Amazon S3 (and S3-compatible services),
  set up entirely in the app. It includes a folder browser, a progress bar and an *Uploaded* badge on every photo.
- **Duplicate detection:** a visual fingerprint spots a print you've already scanned, even at another
  resolution, crop or orientation.
- **Built-in help:** a tooltip on every setting and a full help page.

![Built-in help](docs/screenshots/help.png)

## Requirements

- macOS 13 or later (Apple Silicon or Intel) with Python 3.10+ and the Xcode Command Line Tools
  (`xcode-select --install`, which provides `swiftc`)
- [Homebrew](https://brew.sh) tools: `brew install exiftool rclone`
- An Epson FastFoto FF-680W connected to the same Wi-Fi network as the Mac
- Optional: an [Anthropic API key](https://console.anthropic.com) for AI analysis

## Quick start

```bash
git clone https://github.com/eg7eg7/ffscan.git
cd ffscan
./run.sh
```

The first run creates a Python virtual environment, installs the dependencies and builds the small Apple
Vision helper. Then it opens **http://localhost:8680**. Stop the app with `Ctrl-C`.

1. Load photos **face up** in the scanner's feeder.
2. Choose where to save them under **Save to**, and optionally an **Album** name.
3. Pick settings (hover the ⓘ next to any setting for an explanation) and press **Scan**.

Tick **Keep scanning as I load more photos** to work through a whole box without touching the computer.

### Cloud upload

Under **Cloud upload**, press **+ Connect account…** and choose a service. Google Drive, Google Photos, Dropbox
and OneDrive sign in through your browser; Amazon S3 takes access keys in a form. Then pick a folder and tick
**Upload each photo automatically**, or use **☁ Upload new** at any time.

Uploads use [rclone](https://rclone.org), which keeps account tokens in its own config file
(`~/.config/rclone/rclone.conf`).

### AI analysis

Under **AI analysis**, paste an Anthropic API key and press **Save**. The key is checked with Anthropic and
stored in the macOS Keychain, never in a file. Then tick **Analyse photos with Claude**. Claude on Google Vertex
AI is also supported (choose it under **Service**), if your Google Cloud project has quota for Claude models.

Photos are sent for analysis only while this option is on. Blank backs are never sent.

## Command line

The scanner library also works on its own:

```bash
.venv/bin/python -m ffscan.ff680w status           # ready / feeder empty / jam
.venv/bin/python -m ffscan.ff680w info             # model, serial, firmware
.venv/bin/python -m ffscan.ff680w scan --dpi 600 --picture-only --skip-blank-backs --out ~/Pictures/scans
.venv/bin/python -m ffscan.ff680w scan --help      # all options (--watch, --gray, --deskew, ...)
```

Or from Python:

```python
from ffscan.ff680w import FF680W

with FF680W() as scanner:                  # finds the scanner on the network
    for page in scanner.scan(dpi=300):     # yields each side as soon as it is finished
        if page.side == "B":               # side B is the picture when prints are loaded face up
            open(f"photo_{page.index:03d}.jpg", "wb").write(page.jpeg)
```

## How it works

```
 Scanner ──Wi-Fi, TCP 1865──▶ ff680w.py ──▶ app.py pipeline ──▶ your folder ──▶ rclone ──▶ cloud
 (ESC/I-2 inside "IS" packets)   │            │
                                 │            ├─ crop / straighten       (ff680w.py, sips)
                                 │            ├─ duplicates              (dupes.py)
                                 │            ├─ enhance, red-eye        (enhance.py, Apple Vision)
                                 │            ├─ rotate (faces)          (tools/orient.swift)
                                 │            ├─ metadata                (exiftool)
                                 │            └─ AI analysis, background (photo_ai.py, Claude)
                                 └─ the browser UI polls /api/events once a second
```

| Path | What it is |
|---|---|
| `ffscan/ff680w.py` | Scanner protocol client and command-line tool |
| `ffscan/app.py` | Web server (FastAPI) and the scan → save → process → upload pipeline |
| `ffscan/enhance.py` | Red-eye removal, faded-colour restoration, auto enhance |
| `ffscan/dupes.py` | Duplicate detection (perceptual hash, all four rotations) |
| `ffscan/photo_ai.py` | AI analysis with Claude (Anthropic API or Google Vertex AI) |
| `ffscan/cloud.py` | Cloud accounts through rclone, set up without its text interface |
| `ffscan/static/` | The single-page UI and the help page |
| `tools/orient.swift` | Apple Vision helper: face-based orientation and eye positions |
| `tests/test_ffscan.py` | Offline tests: `.venv/bin/python tests/test_ffscan.py` |
| `docs/PROTOCOL.md` | The scanner's network protocol, documented |
| `docs/ROADMAP.md` | What's done, what's next, known limitations |

## Where things are stored

| What | Where |
|---|---|
| Your scans | The **Save to** folder (default `~/Pictures/Scans`), one subfolder per album or date |
| Settings, upload record, duplicate index | `~/Library/Application Support/FF680W/` |
| Thumbnails and the last 300 untouched scans | `~/Library/Caches/FF680W/` |
| Anthropic API key | macOS Keychain, item `ff680w-anthropic` |
| Cloud account tokens | rclone's config, `~/.config/rclone/rclone.conf` |

The app listens on `127.0.0.1` only, so it isn't reachable from other devices. Set `FFSCAN_PORT`,
`FFSCAN_DATA_DIR` or `FFSCAN_CACHE_DIR` to change the defaults.

## Troubleshooting

| Problem | What to do |
|---|---|
| *No Epson scanner found* | Check the scanner is on and its Wi-Fi light is steady, on the same network as the Mac. If your network blocks discovery, enter the scanner's address under **Scanner → Address**. |
| *Feeder is empty* | Push the photos in until they stop, face up, then press **Refresh**. |
| *Paper jam* | Open the cover, remove the photo, close it, and scan again. Photos already saved are kept. |
| *Scanner refused lock* | Another app is using the scanner: quit Epson FastFoto, ScanSmart or Epson Scan 2. |
| Google refuses the Drive sign-in | rclone's shared Google sign-in is being retired. Create your own OAuth client ID (see **Help → Cloud upload**) and paste it in the connect dialog. |

## Limitations

- macOS only: it uses Apple Vision, `sips`, the Keychain and the native folder picker.
- Tested with the FF-680W over Wi-Fi. USB and the scanner's own Scan button aren't supported yet.
- Without AI analysis, auto-rotate only handles photos with faces.

See [docs/ROADMAP.md](docs/ROADMAP.md) for planned work.

## Disclaimer

ffscan is an independent project, not affiliated with or endorsed by Seiko Epson Corporation. *Epson* and
*FastFoto* are trademarks of their respective owners and are used here only to identify compatible hardware.
The protocol description was produced for interoperability; no Epson software or data is included.
Use at your own risk.

## License

[MIT](LICENSE)

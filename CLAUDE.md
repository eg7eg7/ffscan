# CLAUDE.md

Guidance for AI coding agents (and humans) working on ffscan, an open-source scanning app for the Epson
FastFoto FF-680W. Read README.md for what the app does, and docs/PROTOCOL.md before touching scanner code.

## Tracking work

Tasks live as issues on the [project board](https://github.com/users/eg7eg7/projects/5) (Status: Backlog → Ready → In progress →
Done; labels `area:*`, `feature`, `bug`, `needs-testing`, `blocked`). When you finish a task, close its issue
and set Status to Done; when you discover new work, open an issue with labels. Keep `docs/ROADMAP.md` in sync.

## Ground rules

- **This repository is public. Never commit anything personal:** scanner serial numbers or hostnames
  (`EPSONxxxxxx.local`), real IP addresses (use `192.0.2.x` in docs), emails, cloud project IDs, names or text
  from people's photos, or user home paths. Grep the staged diff before every commit.
- **Never commit photos or scanner output.** `.gitignore` blocks `*.jpg` and `scans/`. Screenshots in
  `docs/screenshots/` must use synthetic demo images, never real prints.
- `NOTES.local.md`, if present, holds the maintainer's private history and state. It's gitignored: read it
  for context, but never copy its contents into tracked files.

## Commands

```bash
./run.sh                                        # set up .venv, build tools/orient, start the app on :8680
.venv/bin/python tests/test_ffscan.py           # offline tests, no scanner needed; prints "ok"
.venv/bin/python -m ffscan.ff680w status        # talk to a real scanner from the CLI
docker build -t ffscan . && docker compose up   # container (macOS-only features disabled)
tools/build-mac-app.sh                          # standalone dist/FFScan.app (pywebview window, PyInstaller)
```

Restarting the app: `kill $(lsof -t -iTCP:8680 -sTCP:LISTEN)`, then `./run.sh`. The process name is `Python`
with a capital P, so `pkill -f "python …"` misses it. A second instance then fails to bind and the old code
keeps serving.

There is no build step for the UI: `ffscan/static/*.html` are served as is. After editing the inline
script, syntax-check it (extract the `<script>` block and run `node --check`).

## Layout

| Path | Role |
|---|---|
| `ffscan/ff680w.py` | Protocol client and CLI: IS packets, ESC/I-2 tokens, discovery, crop, JPEG height fix |
| `ffscan/app.py` | FastAPI app: settings, scan job, per-photo pipeline, background AI and upload workers, API |
| `ffscan/enhance.py` | Red-eye (Vision eye landmarks), faded-colour restoration, auto tone |
| `ffscan/dupes.py` | Perceptual-hash duplicate index |
| `ffscan/photo_ai.py` | Claude analysis (Anthropic API or Vertex AI), structured JSON output |
| `ffscan/cloud.py` | rclone remotes via its `--non-interactive` config protocol; upload destinations |
| `ffscan/desktop.py` | Mac app entry: server thread + pywebview window; closing the window cancels a scan and exits |
| `ffscan/static/` | `index.html` (whole UI, vanilla JS) and `help.html` |
| `tools/orient.swift` | Apple Vision CLI: face-roll orientation and `--eyes`; compiled by `run.sh` |

## Durable queues

`ffscan/jobs.py`: processing, AI and upload work is a row in SQLite (`DATA_DIR/jobs.db`) before it runs and is
marked done after, so a restart resumes it in order. Payloads must be JSON (paths as strings). Picture jobs carry
`back_raw` (their back's raw scan) instead of relying on in-memory pairing. SQLite was chosen over MongoDB: no
server to run, and it works the same natively and in Docker.

## Pipeline (per scanned side)

The scan loop (`app.scan_job`) only calls `save_raw` and queues the side: never add slow work there, or the
scanner stalls. `FEEDING` is set while a stack goes through the scanner; the process, AI and upload workers
call `wait_for_scanner()` first, so the scan gets the CPU (pure-Python crop code holds the GIL) and the Wi-Fi.
`import_job` feeds Epson FastFoto files (`X.jpg`, back `X_b.jpg`; `X_a.jpg` is Epson's enhanced copy) into
the same process queue with crop off. `sides: "fast"` scans single-sided (prints face down) and relabels side A as the picture (side B). `process_worker` runs `app.process_page` strictly in order (back pairing and duplicate checks
depend on it):

copy raw into the album (`Originals/` in the default split layout) → side filter → crop → blank-back check → duplicate check → enhanced copy → face rotation →
metadata → emit `photo` event → AI analysis (background queue) **or** upload queue. The AI worker queues the
uploads after it has written its metadata.

Order matters:
- Crop before writing metadata. It's harmless now that crop keeps EXIF and XMP, but keep the order.
- Rotation is stored as the EXIF Orientation tag (lossless). Crop boxes and Vision coordinates are in
  **stored** pixels: never `exif_transpose` before cropping or before sending images to Claude.

## Scanner facts that cost time to learn

- Photos load face up: **side B is the picture**, side A the back of the print. A is sent first.
  Single-sided scans read only side A (face down). `PARB` always answers `#parFAIL`: no per-side settings.
- Keep polling `IMG` while it returns empty. Stopping early aborts the page, and the scanner reports a jam.
- The JPEG SOF carries the *requested* height; `fix_jpeg_height` patches it to the `#pen` height.
- The scanner sends at most `reply_len` bytes per request; pull bodies with empty `0x2000` requests.
  Never ask for header and body in one reply (`reply_len` > 64 on a command): the scanner stops answering and
  keeps the session locked even after the connection drops, until it is switched off and on.
- USB (`FF680W` with host empty or `"usb"`, pyusb + libusb): same commands, raw bulk transfers, no lock. Header
  and parameter block must be **separate** writes or `PARA` hangs; `_enter_usb` resets a stuck device. USB moves
  a 600 dpi side in ~0.3 s (Wi-Fi ~4.5 s): over USB the scanner's own ~6.5 s per print is the limit.
- Hardware crop (`CRP `) is a no-op; crop is done in software (`find_photo_box`).
- Sample the crop background 1.5–3% in from the edges: the outermost columns of 300 dpi scans differ.
- Pure-white pixels (≥ 250) are deskew corner fill, not photo.
- Find rows first, then count columns only within them: back-side scans have a full-width band past the paper.
- Prints come through tilted (~0.5–1°) even with hardware `SKEW`, often turn a little while feeding (edges
  not square to each other), and cast a colourless grey shadow up to ~40 px wide along an edge; the white paper
  core shows as a thin line at the cut edge. `autocrop` fits each edge as its own line (`photo_quad`) and maps the
  four corners onto a rectangle, then `trim_backing` and `inside`. Re-check on cached raws (last 300 sides in
  `~/Library/Caches/FF680W/raw`) before changing any of it.
- `_bmp_pixels` returns blue-green-red bytes; `_background` returns R, G, B. The backing is a cool grey, so mixing
  the two orders silently breaks crop.

## Prefer on-device methods over AI

The user's rule: use classical/on-device methods wherever they work; Claude is optional and only for what they
can't do (captions, tags, date estimates). Current split: duplicates = perceptual hash; rotation = face roll,
then Vision scene classifier with `SCENE_MARGIN` (measured 12/14); back text = Vision OCR + `human_text()`
filter (drops watermarks and lab codes); Claude rotates or reads backs only when those had no answer.

## Gotchas

- **Scan speed is bound by Wi-Fi transfer, not processing.** Measured: ~8 s per side at 600 dpi / JPEG 95 (~5.8 MB,
  ~0.7 MB/s) even with all processing in the background. Each image block used to cost two round trips; `IMG` now
  uses `read_ahead` and `#BSZ` (1 MiB). Epson's own engine sends `#BSZ` = 1 MiB (`SDEModelInfo -bufferSize`
  default when the model file has no `BufferSizeValue`), so ffscan does the same. The scanner answers `#parOK`
  to any `#BSZ` value, so check the `MB/s` lines in the activity log after a real scan. Uploads pause while a scan runs so they don't share the Wi-Fi.

- **Live updates are polled** (`/api/events?since=N`, once a second), on purpose. One SSE stream per tab used up
  Chrome's six connections per host, and every other request hung.
- The grid must use `/api/thumb` (cached 480 px), never full scans: hundreds of 4 MB JPEGs froze the browser.
- CSS: `[hidden] { display: none !important }` exists because `.folder`/`.progress` set `display:flex`.
- Escape any server or cloud string before putting it in `innerHTML` (`esc()` in index.html).
- Duplicate threshold 7 was measured: the same print differs by 0–6 bits, different prints by 9 or more.
  Re-measure before changing it. Backs (`_back`, CLI `_<n>A`) and `_enhanced` copies are excluded, because
  blank backs all look alike.
- macOS-only pieces degrade gracefully in Docker or Linux: `dns-sd`, `security` (Keychain), `osascript`,
  `tools/orient`, `~/.Trash`. Keep it that way: check with `shutil.which` or path existence.
- Claude API code: default model `claude-opus-5-5`. Use structured output via `output_config.format`.
  Server-side fallbacks on the Anthropic API; `BetaRefusalFallbackMiddleware` on Vertex.

## Tests

`tests/test_ffscan.py` is a plain assert script, not pytest. Protocol fixtures are real scanner replies with
the serial replaced. Add one check for any new non-trivial logic: a parser, a threshold, an image heuristic.

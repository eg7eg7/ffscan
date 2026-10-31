# Roadmap

## Done

- **Scanner protocol:** ESC/I-2 over Epson's "IS" network framing (see [PROTOCOL.md](PROTOCOL.md)).
- **Library and CLI:** status, info, capabilities, and duplex scanning at 50–600 dpi, colour or grey, with
  deskew, double-feed detection and paper-end detection.
- **Discovery** with Bonjour plus a /24 network-scan fallback, and rediscovery when the scanner's address changes.
- **Software crop**, robust to the grey backing, white print borders, deskew corner fill, the band past the
  paper end, and the edge strip in 300 dpi scans.
- **Web app** on a local port:
  - live grid with cached thumbnails
  - every scan setting in the UI, with tooltips and a help page
  - albums, and metadata written into the files
  - watch mode, which keeps scanning new stacks
- **Backs:** save the picture only, the back too, or skip blank backs (measured by an "ink" score).
- **Enhancement:** red-eye removal (Apple Vision eye landmarks), faded-colour restoration, and auto
  brightness/contrast/saturation.
- **Orientation:** on-device face roll angle (Apple Vision). AI analysis covers photos without faces.
- **AI analysis with Claude** through the Anthropic API or Google Vertex AI: rotation, caption, tags, date
  estimate, place, and handwriting on the back. It only fills empty fields.
- **Cloud upload through rclone:**
  - accounts connected from the UI (OAuth in the browser, S3 keys in a form)
  - folder browser, progress bar, and a per-photo upload badge
- **Duplicate detection:** dHash in four rotations. Tuned on repeated scans of an 18-print stack: the same
  print differs by 0–6 bits, different prints by 9 or more; the threshold is 7.
- **Safe delete:** photos go to the macOS Trash.

## Next

- **Face recognition, local only.** Group faces across the library, name a group once, and write the names to
  the *person in image* field. No faces leave the Mac.
- **Scan button:** start a scan from the scanner's own button (push-scan events; the interrupt channel is
  listed in PROTOCOL.md).
- **Measure AI orientation** on a labelled set of photos without faces, and compare models and costs.
- **Red-eye check** on real flash photos; so far it has only been tested on synthetic eyes.
- **Enhancement v2:** dust and scratch removal, and a side-by-side before/after view.
- **USB transport** for scanners without Wi-Fi.
- **Other ESC/I-2 scanners** (FF-640, DS series): the protocol is shared, but model details may differ.

## Known limitations

- macOS only.
- The network-scan fallback assumes a /24 subnet. Larger networks rely on Bonjour or a manually entered address.
- rclone's shared Google sign-in is being retired by rclone; Google accounts may need your own OAuth client ID.
- Cropping uses `sips`, which re-encodes the JPEG once at quality 95.

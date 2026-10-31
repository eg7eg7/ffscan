# FF-680W network protocol

How ffscan talks to an **Epson FastFoto FF-680W** over Wi-Fi. Everything here was established by
interoperability analysis of Epson's own macOS software (Epson Scan 2), then confirmed against a real scanner.
It should also apply, with small differences, to other Epson scanners that use ESC/I-2, such as the
FastFoto FF-640 and the DS series.

> Addresses below use the documentation range `192.0.2.0/24`; serial numbers are replaced with `X0AB000000`.

## Overview

```
TCP 1865 ─ "IS" packets (Epson network framing)
             └─ type 0x2000 payloads carry ESC/I-2 commands and replies
                  INFO / CAPA / STAT   →  what the scanner is and what it can do
                  PARA → TRDT → IMG…   →  a scan: settings, start, pull image blocks
                  FIN                  →  leave ESC/I-2 mode
```

| Item | Value |
|---|---|
| Discovery | Bonjour `_scanner._tcp`, instance "EPSON FF-680W", port 1865 (TXT: `mdl=FF-680W`, `scannerAvailable=1`, `adminurl`) |
| Other ports | 80/443: built-in web admin. 631 and 9100 are closed. |
| USB (not used by ffscan) | VID `0x04B8`, PID `0x016B`; Epson's internal model ID `ES0170` |
| Command set | ESC/I-2 (Epson Scan 2 `ESCommandType` 4, class `ESCI2Scanner`) |
| Image format | JPEG only (no raw RGB over the wire) |

## 1. "IS" packets (network framing)

Every packet starts with a 12-byte header, big-endian:

```
offset  0  'I' 'S'
        2  type (u16)            requests 0x2xxx; replies set the high bit (0x2000 → 0xA000)
        4  0x00 0x0C             (the scanner sends 0x38 0x0C here)
        6  payload length (u32)
       10  0x00 0x00
       12  payload
```

| Type | Direction | Meaning |
|---|---|---|
| `0x8000` | ← | Hello, sent by the scanner as soon as you connect. Payload `01 04 00 00 00`; while another client holds the scanner it also carries that client's IPv4 address. |
| `0x2100` | → | Lock (claim the scanner). Payload `01 A0 04 00 00 01 2C`. Reply `0xA100` with `06` (ACK). |
| `0x2101` | → | Unlock. Reply `0xA101`, empty. |
| `0x2000` | → | ESC/I data. Payload = `data_len (u32)` + `reply_len (u32)` + data. Reply `0xA000`. |

**The scanner never sends more than `reply_len` bytes.** To read a reply body you send another `0x2000`
packet with no data and `reply_len` = the number of bytes still wanted. A command sent with `reply_len` larger
than the 64-byte header (to get header and body in one go) is never answered, and the scanner then stays locked
to that client until it is switched off and on: every block costs two round trips.

After locking, send `1C 58` (FS X) with `reply_len` 1: the scanner answers `06` and switches to ESC/I-2.

## 2. ESC/I-2 commands

A request is a 12-character ASCII header, optionally followed by a parameter block, sent in **one** `0x2000` packet:

```
<4-char command> 'x' <7 hex digits: parameter block length> [parameter block]
e.g.  INFOx0000000        PARAx0000062#ADFDPLX...
```

The reply is a 64-byte header of the same shape: `<command>x<7 hex digits: body length>` followed by tokens,
padded with `#---`. Then read the body (see "never more than reply_len" above).

### Tokens

Parameters and replies are a sequence of `#KEY` (3 characters after `#`) followed by typed values:

| Prefix | Example | Meaning |
|---|---|---|
| `d` + 3 digits | `d600` | small decimal |
| `i` + 7 digits | `i0002550`, `i-000020` | integer (may be negative) |
| `x` + 7 hex digits | `x0010000` | hex number |
| `h` + 3 hex digits + bytes | `h007FF-680W` | string of that many bytes, padded to a multiple of 4 |
| anything else | `FEED`, `JPG ` | a 4-character word (space-padded) |

One key can repeat: `#ADFTYPEFEED#ADFDPLX1SCN` is two `ADF` entries.

### Commands used by ffscan

| Command | Purpose |
|---|---|
| `INFO` | Model, firmware, serial, feeder geometry |
| `CAPA` | Capabilities (`CAPB` = back-side capabilities, empty on this model) |
| `STAT` | Status: no `#ERR` = ready; `#ERRADF PE  ` feeder empty; `#ERRADF PJ  ` jam |
| `PARA` | Scan settings (reply `#parOK  ` or a list of rejected tokens) |
| `TRDT` | Start the scan (the scanner starts feeding) |
| `IMG ` | Pull image data (poll) |
| *(in `PARA`)* `#BSZ` | Image block size. **Send it.** Without it the scanner uses 64 KB blocks; each block costs two round trips, which caps Wi-Fi throughput (~0.7 MB/s measured). Epson's engine sends `BufferSizeValue` from its model file, default `0x100000` (1 MiB). The scanner answers `#parOK` to any value. |
| `CAN ` | Cancel a running scan |
| `FIN ` | Leave ESC/I-2 mode |

A command that isn't valid in the current state is answered with `INVDx0000000`.

### What the FF-680W reports

`INFO` (abridged):
```
ADF   TYPE FEED · DPLX 1SCN (one-pass duplex) · ALGN CNTR (centred) · AREA d850 i0001550
      AMIN d200 d200 · AMAX d850 i0024000 · RESO d600
PRD "FF-680W" · VER "ADF 18P4" · PRV "18.61.SP18P4" · S/N "X0AB000000" · DSZ 1536
```
Lengths are in 1/100 inch: 2″ minimum, 8.5″ wide, up to 240″ long; 600 dpi optical.

`CAPA` (abridged):
```
ADF   DPLX PEDT (paper-end detection) DFL1 DFL3 (double-feed levels) LDF CRP SKEW OVSN
COL   C024 (24-bit colour) M008 (8-bit grey) R008 G008 B008
FMT   JPG       JPG RANG d001..d100 (quality)
GMM   UG10 UG18 (gamma 1.0 / 1.8)    GMT  RED GRN BLU MONO (custom tables)
RSM / RSS   50 75 100 150 200 240 300 360 400 600 (main / sub-scan dpi)
CRP RANG -20..20 · LAM ON OFF (laminated photo mode)
```

## 3. A complete scan

```
→ STAT                                            ← (no #ERR: photos loaded)
→ PARA #ADFDPLXPEDTSKEWDFL1#COLC024#FMTJPG #JPGd090#GMMUG18
       #RSMi0000300#RSSi0000300#ACQi0000000i0000000i0002550i0010800#BSZi1048576
                                                  ← PARAx0000000#parOK
→ TRDT                                            ← TRDTx0000000
→ IMG   (repeat every ~0.2 s)
    ← IMG x0000000                                  nothing yet: the photo is still feeding
    ← IMG x0000265 #psti0002550d000i0010800 #typIMGA   page start, side A; JPEG data begins (FFD8)
    ← IMG x0100000 #typIMGA                          more JPEG data (blocks up to #BSZ; 64 KB without it)
    ← IMG x0000000 #peni0002550i0001263 #typIMGA     page end: real size 2550 × 1263
    ← … the same for #typIMGB (side B) …
    ← IMG x0000000 #peni…#lftd000#typIMGB            last page: 0 left in the feeder
→ IMG                                             ← INVDx0000000   (transfer finished)
→ FIN
```

`#ADF` takes several 4-character flags concatenated: `DPLX` duplex, `PEDT` stop at the end of the paper,
`SKEW` straighten, `DFL1`/`DFL3` double-feed detection. `#ACQ` is x, y, width, height in pixels: always the
full 8.5″ width, with the height as the longest photo you expect.

### Things that are easy to get wrong

- **Keep polling `IMG`.** It returns empty replies while a photo feeds. Stopping early aborts the page,
  the photo still ejects, and the scanner then reports a jam (`PJ`).
- **Fix the JPEG height.** The JPEG's SOF header carries the *requested* `#ACQ` height. Patch it to the
  height from `#pen`, or every image gets a long blank bottom.
- **Sides.** Photos load face up, so **side B (`IMGB`) is the picture** and side A is the back of the print.
  A single-sided scan (no `DPLX`) reads only side A, the side facing down: to get just the pictures that way,
  load the prints face down.
- **No per-side settings.** `PARB` (settings for the second side) exists but answers `#parFAIL` to every
  parameter set, even one identical to `PARA`, and `CAPB` is empty: both sides of a duplex scan always share
  resolution, colour and quality.
- **Hardware crop** (`CRP `) is accepted but has no visible effect. ffscan crops in software, as Epson's
  own software does (`ESSWAutoSize` in its model file).
- **Photos added mid-scan:** a print added after `#lftd000` isn't fed; start a new `TRDT`.
- **The 300 dpi edge strip.** The outermost columns of a 300 dpi scan differ from the grey backing; sample
  the background colour slightly inward when cropping.

## 4. Not covered yet

- The **Scan button** on the scanner (push-scan events; Epson's model file names `com.epson.EventManager`
  as the destination and enables an interrupt channel).
- USB transport (`USBInterface` in Epson's `Scanner.framework`: bulk in/out plus an interrupt pipe).
- The newer TLS network path (`ESNWInterface`).

## Method

The analysis was done with Claude Code and the [REA](https://github.com/morluto/rea) reverse-engineering skill.

1. **Static analysis** of Epson Scan 2 6.7.84 on macOS (`Scanner.framework`, `ScanEngine.framework`): class
   and method names, format strings such as `x%07lX`, `i%07d`, `d%03u`, `h%03X`, and disassembly of
   `-[ESCI2Scanner sendRequest:paramBlock:]`, which writes the 12-byte header and expects a 64-byte reply.
2. **Device configuration** from Epson's model file (USB IDs, command type, capabilities).
3. **Live confirmation** with read-only probes (`INFO`, `CAPA`, `STAT`), then real scans.

No Epson code or data files are included in this project.

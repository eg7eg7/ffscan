"""ffscan as one Mac app: the server and its window in one process. Closing the window quits both.

Run from the repo with `.venv/bin/python -m ffscan.desktop`; tools/build-mac-app.sh packs it into FFScan.app.
Only one ffscan runs at a time: starting it again brings the running one to the front instead.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):  # inside FFScan.app: exiftool, rclone and libusb are bundled next to the code
    os.environ["PATH"] = f"{Path(sys._MEIPASS) / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["DYLD_LIBRARY_PATH"] = sys._MEIPASS  # read by ctypes.util.find_library when pyusb loads libusb

PORT = int(os.environ.get("FFSCAN_PORT", 8680))
URL = f"http://127.0.0.1:{PORT}"


def port_in_use():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def show_running():
    """ffscan is already serving PORT: bring its window to the front, or, if it runs without one (./run.sh in
    browser mode), open its page in the browser."""
    pids = subprocess.run(["lsof", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication

        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pids[0]))
        if running and running.activationPolicy() == NSApplicationActivationPolicyRegular:  # it has a window
            running.unhide()
            running.activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
            return
    except (ImportError, IndexError, ValueError):  # no AppKit (browser-only setup) or no lsof answer
        pass
    webbrowser.open(URL)


def main():
    if port_in_use():
        show_running()
        return

    import uvicorn
    import webview

    from ffscan import app as ffscan

    def on_closing():
        scan = ffscan.job["thread"]
        if scan and scan.is_alive():  # stop cleanly: quitting mid-page makes the scanner report a jam
            ffscan.job["cancel"].set()
            scan.join(30)

    ffscan.startup()
    server = uvicorn.Server(uvicorn.Config(ffscan.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    window = webview.create_window("FF-680W Scanner", URL, width=1400, height=900, min_size=(800, 600))
    window.events.closing += on_closing
    # Keep the page's own storage (minimised log, last tab) between launches.
    webview.start(private_mode=False, storage_path=str(ffscan.DATA_DIR / "webview"))
    os._exit(0)  # the workers are daemon threads; their queues live in SQLite and resume on the next start


if __name__ == "__main__":
    main()

"""Cloud accounts for upload, managed through rclone without its text UI.

rclone's `--non-interactive` config protocol returns each setup question as JSON; we answer them here, so
connecting an account is one click in the web app. OAuth providers open the browser for sign-in (rclone runs
the local redirect server), S3 takes keys from a form. Tokens/keys live in rclone's own config file.
"""
import json
import re
import subprocess
import threading

PROVIDERS = {
    "drive": {"label": "Google Drive", "type": "drive", "params": {"scope": "drive.file"}, "oauth": True,
              "note": "Uploads into a folder in your Drive. The app can only see files it uploaded."},
    "googlephotos": {"label": "Google Photos", "type": "googlephotos", "params": {}, "oauth": True,
                     "note": "Each album becomes a Google Photos album."},
    "dropbox": {"label": "Dropbox", "type": "dropbox", "params": {}, "oauth": True, "note": ""},
    "onedrive": {"label": "OneDrive", "type": "onedrive", "params": {}, "oauth": True, "note": ""},
    "s3": {"label": "Amazon S3 / S3-compatible", "type": "s3", "params": {"provider": "AWS"}, "oauth": False,
           "fields": [("access_key_id", "Access key ID", False), ("secret_access_key", "Secret access key", True),
                      ("region", "Region (e.g. eu-west-1)", False), ("endpoint", "Endpoint (only for non-AWS)", False)],
           "note": "Put the bucket name in 'Folder in the cloud', e.g. my-bucket/FastFoto."},
}

# answers for rclone's setup questions; anything else gets rclone's default
AUTO_ANSWERS = {
    "config_shared_client_id": "true",   # Google: try rclone's shared client (being retired in 2026)
    "config_is_local": "true",           # this Mac has a browser: rclone opens it and catches the redirect
    "config_change_team_drive": "false",
    "config_type": "onedrive",           # OneDrive: personal/business drive of the signed-in user
    "config_drive_ok": "true",
}

_connecting = {"proc": None, "cancelled": False}


def _rclone(*args, timeout=None):
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)


def remotes():
    """[{name, type, label}] of configured accounts. Never returns tokens or keys."""
    r = _rclone("config", "dump")
    conf = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
    labels = {p["type"]: p["label"] for p in PROVIDERS.values()}
    return [{"name": n, "type": c.get("type", ""), "label": labels.get(c.get("type"), c.get("type", ""))}
            for n, c in sorted(conf.items())]


def remote_type(name):
    return next((r["type"] for r in remotes() if r["name"] == name), "")


def new_name(provider):
    taken = {r["name"] for r in remotes()}
    return next(n for n in [provider] + [f"{provider}{i}" for i in range(2, 100)] if n not in taken)


def connect(provider, fields=None, on_question=None):
    """Create an rclone remote, answering rclone's questions. Blocks during browser sign-in. Returns its name.

    fields: values typed in the UI (S3 keys, or Google client_id/client_secret).
    """
    p = PROVIDERS[provider]
    name = new_name(provider)
    params = {**p["params"], **{k: v for k, v in (fields or {}).items() if v}}
    kv = [f"{k}={v}" for k, v in params.items()]
    args = ["config", "create", name, p["type"], *kv, "--non-interactive"]
    _connecting["cancelled"] = False
    try:
        for _ in range(20):  # each round answers one question; real flows need 1-4
            proc = subprocess.Popen(["rclone", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            _connecting["proc"] = proc
            out, err = proc.communicate()  # waits while the user signs in in the browser
            if _connecting["cancelled"]:
                raise RuntimeError("cancelled")
            if proc.returncode != 0:
                raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "rclone failed")
            state = json.loads(out)
            if state.get("Error"):
                raise RuntimeError(state["Error"])
            if not state.get("State"):
                return name
            option = state["Option"]
            answer = AUTO_ANSWERS.get(option["Name"], option.get("DefaultStr", str(option.get("Default", ""))))
            if on_question:
                on_question(option["Name"])
            args = ["config", "update", name, *kv, "--continue", "--non-interactive",
                    "--state", state["State"], "--result", answer]
        raise RuntimeError("too many setup questions")
    except BaseException:
        _rclone("config", "delete", name)  # don't leave half-configured accounts behind
        raise
    finally:
        _connecting["proc"] = None


def cancel_connect():
    _connecting["cancelled"] = True
    if _connecting["proc"]:
        _connecting["proc"].terminate()


def connect_async(provider, fields, emit):
    def run():
        emit("remote_connecting", provider=provider, oauth=PROVIDERS[provider]["oauth"])
        try:
            name = connect(provider, fields)
            emit("remote_connected", name=name, label=PROVIDERS[provider]["label"])
        except Exception as e:
            emit("remote_failed", provider=provider, error=str(e)[:400])

    threading.Thread(target=run, daemon=True).start()


def delete(name):
    _rclone("config", "delete", name)


def test(name):
    """(ok, message): list the top level of the account."""
    path = f"{name}:album" if remote_type(name) == "googlephotos" else f"{name}:"
    try:
        r = _rclone("lsjson", "--max-depth", "1", path, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["failed"])[-1]
        return False, re.sub(r"^\S+ \S+ (NOTICE|ERROR) ?: ", "", last)[:300]  # drop rclone's log prefix
    return True, f"connected ({len(json.loads(r.stdout or '[]'))} items at top level)"


def folders(name, path=""):
    """Sub-folder names of `path` in the account (for S3 the top level lists buckets)."""
    r = _rclone("lsjson", "--dirs-only", "--max-depth", "1", f"{name}:{path.strip('/')}", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(re.sub(r"^\S+ \S+ (NOTICE|ERROR) ?: ", "", (r.stderr.strip().splitlines() or ["failed"])[-1]))
    return sorted((d["Name"] for d in json.loads(r.stdout or "[]")), key=str.lower)


def make_folder(name, path):
    r = _rclone("mkdir", f"{name}:{path.strip('/')}", timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["failed"])[-1])


def destination(remote, folder, album, filename):
    """rclone destination path for one photo. Google Photos only accepts uploads into album/<name>/."""
    if remote_type(remote) == "googlephotos":
        return f"{remote}:album/{album}/{filename}"
    return f"{remote}:{folder.strip('/')}/{album}/{filename}"

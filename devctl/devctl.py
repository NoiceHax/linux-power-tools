#!/usr/bin/env python3
"""devctl - switch off unused USB hardware (saves power) and turn it back on remotely.

  devctl serve            root daemon, listens on 127.0.0.1 ONLY (systemd runs this)
  devctl tui              curses UI: space=toggle, a=all ON, o=all OFF, q=quit
  devctl status|on|off [name|all]   plain CLI

Remote use: nothing is exposed on the network. From another machine:
  ssh -L 8765:127.0.0.1:8765 minty@<host>      then run `devctl tui --token <token>` locally,
  or just ssh in and run `devctl tui` on the box. (Token: /etc/devctl/token)

Only the devices in DEVICES below can ever be touched, matched by USB vid:pid.
"""
import argparse, curses, hmac, json, os, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765          # never bind anything but loopback
USB = "/sys/bus/usb/devices"
STATE_DIR, TOKEN_FILE = "/var/lib/devctl", "/etc/devctl/token"
DEVICES = {                               # name -> (vid, pid, description)
    "webcam":     ("04f2", "b579", "Chicony EasyCamera (built-in webcam)"),
    "cardreader": ("0bda", "0129", "Realtek RTS5129 SD card reader"),
    "bluetooth":  ("0bda", "0821", "Realtek RTL8821A Bluetooth radio"),
}
DRY = False  # --dry-run: pretend, don't write sysfs (for testing)
_fake = {n: True for n in DEVICES}
_lock = threading.Lock()


def _rd(p):
    try:
        return open(p).read().strip()
    except OSError:
        return None


def _sysfs_dir(name):
    vid, pid, _ = DEVICES[name]
    try:
        for d in os.listdir(USB):
            p = f"{USB}/{d}"
            if _rd(f"{p}/idVendor") == vid and _rd(f"{p}/idProduct") == pid:
                return p
    except OSError:
        pass
    return None


def get_state():
    out = {}
    for n, (vid, pid, desc) in DEVICES.items():
        if DRY:
            out[n] = {"on": _fake[n], "present": True, "desc": desc}
            continue
        p = _sysfs_dir(n)
        out[n] = {"on": bool(p) and _rd(f"{p}/authorized") == "1", "present": bool(p), "desc": desc}
    return out


def set_dev(name, on):
    if name not in DEVICES:
        raise KeyError(name)
    if DRY:
        _fake[name] = on
        return
    p = _sysfs_dir(name)
    if not p:
        raise FileNotFoundError(f"{name} not found on USB bus")
    with open(f"{p}/authorized", "w") as f:
        f.write("1" if on else "0")


def save_wanted(st):
    if DRY:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump({n: v["on"] for n, v in st.items()}, open(f"{STATE_DIR}/wanted.json", "w"))


def apply_wanted():
    """At boot: devices come back authorized; re-apply the saved choice (default OFF)."""
    try:
        wanted = json.load(open(f"{STATE_DIR}/wanted.json"))
    except (OSError, ValueError):
        wanted = {}
    for n in DEVICES:
        try:
            set_dev(n, bool(wanted.get(n, False)))
        except OSError as e:
            print(f"apply {n}: {e}", file=sys.stderr)


# ------------------------------------------------------------------ server
def make_handler(token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _ok(self):
            # defeat DNS-rebinding: only accept loopback Host names
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "bad host"}); return False
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got, f"Bearer {token}"):
                self._send(401, {"error": "bad token"}); return False
            return True

        def do_GET(self):
            if self._ok() and self.path == "/status":
                self._send(200, get_state())
            elif self._ok():
                self._send(404, {"error": "not found"})

        def do_POST(self):  # /on/<name|all>  /off/<name|all>
            if not self._ok():
                return
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] not in ("on", "off") or (parts[1] != "all" and parts[1] not in DEVICES):
                return self._send(404, {"error": "use /on|off/<name|all>"})
            with _lock:
                errs = {}
                for n in (DEVICES if parts[1] == "all" else [parts[1]]):
                    try:
                        set_dev(n, parts[0] == "on")
                    except OSError as e:
                        errs[n] = str(e)
                time.sleep(0.3)
                st = get_state()
                save_wanted(st)
            self._send(200 if not errs else 207, {"state": st, "errors": errs})
    return H


def serve():
    if not DRY and os.geteuid() != 0:
        sys.exit("serve must run as root")
    token = os.environ.get("DEVCTL_TOKEN") or _rd(TOKEN_FILE)
    if not token:
        sys.exit(f"no token: create {TOKEN_FILE}")
    if not DRY:
        apply_wanted()
    srv = ThreadingHTTPServer((HOST, PORT), make_handler(token))
    print(f"devctl listening on {HOST}:{PORT}", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------ client
def call(method, path, a):
    req = urllib.request.Request(f"{a.url}{path}", method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {a.token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        d = json.load(e)
        if e.code != 207:
            raise SystemExit(f"error {e.code}: {d.get('error')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {a.url} ({e.reason}) - is the service running / tunnel up?")
    return d.get("state", d), d.get("errors", {})


def tui(a):
    def run(scr):
        curses.curs_set(0)
        sel, msg = 0, ""
        names = list(DEVICES)
        st, _ = call("GET", "/status", a)
        while True:
            scr.erase()
            scr.addstr(0, 0, "devctl - USB power switches", curses.A_BOLD)
            for i, n in enumerate(names):
                s = st.get(n, {})
                tag = "ON " if s.get("on") else "OFF"
                line = f" [{tag}] {n:<11} {s.get('desc', '')}"
                scr.addstr(2 + i, 0, line, curses.A_REVERSE if i == sel else 0)
            scr.addstr(3 + len(names), 0, "up/down select   space toggle   a ALL ON   o ALL OFF   r refresh   q quit")
            scr.addstr(5 + len(names), 0, msg[: curses.COLS - 1] if hasattr(curses, "COLS") else msg)
            k = scr.getch()
            try:
                if k in (ord("q"), 27):
                    return
                if k in (curses.KEY_UP, ord("k")):
                    sel = (sel - 1) % len(names)
                elif k in (curses.KEY_DOWN, ord("j")):
                    sel = (sel + 1) % len(names)
                elif k in (ord(" "), 10):
                    n = names[sel]
                    st, e = call("POST", f"/{'off' if st[n]['on'] else 'on'}/{n}", a)
                    msg = f"{n}: {e[n]}" if e else f"{n} toggled"
                elif k == ord("a"):
                    st, e = call("POST", "/on/all", a); msg = "all ON" + (f" (errors: {e})" if e else "")
                elif k == ord("o"):
                    st, e = call("POST", "/off/all", a); msg = "all OFF" + (f" (errors: {e})" if e else "")
                elif k == ord("r"):
                    st, _ = call("GET", "/status", a); msg = "refreshed"
            except SystemExit as e:
                msg = str(e)
    curses.wrapper(run)


def main():
    global DRY, PORT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["serve", "tui", "status", "on", "off"])
    ap.add_argument("name", nargs="?", default="all")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token", default=os.environ.get("DEVCTL_TOKEN") or _rd(TOKEN_FILE))
    ap.add_argument("--dry-run", action="store_true", help="test mode: no sysfs writes")
    a = ap.parse_args()
    DRY, PORT = a.dry_run, a.port
    a.url = f"http://{HOST}:{a.port}"
    if a.cmd == "serve":
        serve()
    elif not a.token:
        sys.exit(f"no token (cannot read {TOKEN_FILE}); pass --token or set DEVCTL_TOKEN")
    elif a.cmd == "tui":
        tui(a)
    elif a.cmd == "status":
        for n, s in call("GET", "/status", a)[0].items():
            print(f"{n:<11} {'ON ' if s['on'] else 'OFF'}  {s['desc']}")
    else:
        st, e = call("POST", f"/{a.cmd}/{a.name}", a)
        print({n: s["on"] for n, s in st.items()}, e or "")


if __name__ == "__main__":
    main()

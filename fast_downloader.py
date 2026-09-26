"""Fast Downloader - segmented, multi-connection HTTP downloader with a Tkinter UI.

The file is split into byte ranges that are fetched in parallel and written
straight into a preallocated ``.part`` file, which is renamed when complete.

Each segment can travel over a different *route*, so segments can come from
different IP addresses (e.g. one over your VPN adapter, one over your normal
connection, others through SOCKS5 proxies offered by your VPN provider).

Route syntax (one per line in the UI):
    direct                         normal connection (system default)
    10.8.0.2                       bind to this local IP (e.g. a VPN adapter)
    http://host:port               HTTP proxy  (user:pass@ allowed)
    socks5://user:pass@host:port   SOCKS5 proxy (needs `pip install PySocks`)
    socks5h://host:port            SOCKS5, DNS resolved by the proxy
    pia                            Private Internet Access SOCKS5 proxy, using the
                                   credentials set in the "PIA…" dialog, or else
                                   PIA_SOCKS_USER / PIA_SOCKS_PASS from the
                                   environment or a .env file next to this script
                                   (see .env.example)

Standard library only, except PySocks for SOCKS routes.
"""

import collections
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

CHUNK = 64 * 1024
MIN_SEGMENT = 256 * 1024          # never split into pieces smaller than this
STEAL_MIN = 1024 * 1024           # only steal from segments with at least this much left
MAX_RETRIES = 5
TIMEOUT = 30
TICK_MS = 200
SPEED_WINDOW = 3.0                # seconds of history used for the speed readout
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FastDownloader/1.0"
IP_CHECK_URL = "https://api.ipify.org"
PIA_HOST = "proxy-nl.privateinternetaccess.com"
PIA_PORT = 1080
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".fast_downloader.json")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(seconds):
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def describe(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "download.bin"


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def default_dir():
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    return downloads if os.path.isdir(downloads) else os.getcwd()


def local_ipv4s():
    """Best-effort list of this machine's IPv4 addresses (VPN adapters included)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def load_dotenv(path):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes and 'export'.
    Variables already set in the real environment win."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def env_pia_configured():
    return bool(os.environ.get("PIA_SOCKS_USER") and os.environ.get("PIA_SOCKS_PASS"))


def pia_proxy_url(pia):
    """Build the SOCKS5 URL for PIA: dialog settings first, then environment / .env."""
    if pia.get("user") and pia.get("password"):
        user, password = pia["user"], pia["password"]
    else:
        user, password = os.environ.get("PIA_SOCKS_USER"), os.environ.get("PIA_SOCKS_PASS")
    if not user or not password:
        raise ValueError("the 'pia' route needs PIA SOCKS credentials - click 'PIA…' or use .env")
    host = pia.get("host") or os.environ.get("PIA_SOCKS_HOST") or PIA_HOST
    port = int(pia.get("port") or os.environ.get("PIA_SOCKS_PORT") or PIA_PORT)
    q = lambda v: urllib.parse.quote(v, safe="")
    # socks5 (not socks5h): PIA's proxy answers "host unreachable" when asked to
    # resolve hostnames itself, so names are resolved locally.
    return f"socks5://{q(user)}:{q(password)}@{host}:{port}"


def filename_from(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        return safe_name(urllib.parse.unquote(m.group(1).strip().strip('"')))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    name = m.group(1).strip() if m else os.path.basename(urllib.parse.urlparse(url).path)
    return safe_name(urllib.parse.unquote(name))


# --------------------------------------------------------------------------- #
# Routes: how a connection leaves this machine
# --------------------------------------------------------------------------- #

class _ConnHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, conn_cls, **conn_kw):
        super().__init__()
        self._cls, self._kw = conn_cls, conn_kw

    def http_open(self, req):
        return self.do_open(self._cls, req, **self._kw)


class _ConnHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, conn_cls, **conn_kw):
        super().__init__(context=ssl.create_default_context())
        self._cls, self._kw = conn_cls, conn_kw

    def https_open(self, req):
        return self.do_open(self._cls, req, context=self._context, **self._kw)


def _socks_connections(parsed):
    try:
        import socks  # PySocks
    except ImportError:
        raise ValueError("SOCKS routes need PySocks: pip install PySocks") from None

    scheme = parsed.scheme.lower()
    proxy = dict(
        proxy_type=socks.SOCKS5 if scheme.startswith("socks5") else socks.SOCKS4,
        proxy_addr=parsed.hostname,
        proxy_port=parsed.port or 1080,
        proxy_rdns=scheme in ("socks5h", "socks4a"),
        proxy_username=urllib.parse.unquote(parsed.username) if parsed.username else None,
        proxy_password=urllib.parse.unquote(parsed.password) if parsed.password else None,
    )

    def open_socket(conn):
        host = conn.host
        if not proxy["proxy_rdns"]:
            # resolve to IPv4 ourselves; proxies often can't reach IPv6 targets
            try:
                host = socket.getaddrinfo(host, conn.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            except OSError:
                pass
        # when proxy_addr resolves to several servers, PySocks tries each in turn
        return socks.create_connection((host, conn.port), timeout=conn.timeout, **proxy)

    class SocksHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = open_socket(self)

    class SocksHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            self.sock = self._context.wrap_socket(open_socket(self), server_hostname=self.host)

    return SocksHTTPConnection, SocksHTTPSConnection


class Route:
    """One way out to the internet. Each has its own urllib opener."""

    def __init__(self, spec, pia=None):
        self.spec = spec.strip()
        s = self.spec
        label = None
        if s.lower() == "pia":
            s = pia_proxy_url(pia or {})
            label = "PIA " + urllib.parse.urlparse(s).hostname.split(".")[0]
        no_env_proxy = urllib.request.ProxyHandler({})

        if s.lower() in ("", "direct"):
            self.label = "direct"
            self.opener = urllib.request.build_opener()
            return

        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            ip = None
        if ip is not None:
            self.label = f"bind {ip}"
            src = (str(ip), 0)
            self.opener = urllib.request.build_opener(
                no_env_proxy,
                _ConnHTTPHandler(http.client.HTTPConnection, source_address=src),
                _ConnHTTPSHandler(http.client.HTTPSConnection, source_address=src),
            )
            return

        parsed = urllib.parse.urlparse(s)
        scheme = parsed.scheme.lower()
        if not parsed.hostname:
            raise ValueError(f"Unrecognised route: {s!r}")
        self.label = label or f"{scheme}://{parsed.hostname}:{parsed.port or ''}".rstrip(":")
        if scheme in ("http", "https"):
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": s, "https": s}))
        elif scheme in ("socks5", "socks5h", "socks4", "socks4a"):
            http_cls, https_cls = _socks_connections(parsed)
            self.opener = urllib.request.build_opener(
                no_env_proxy, _ConnHTTPHandler(http_cls), _ConnHTTPSHandler(https_cls))
        else:
            raise ValueError(f"Unsupported proxy scheme: {scheme!r}")

    def open(self, url, start=None, end=None, timeout=TIMEOUT):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)

    def public_ip(self):
        with self.open(IP_CHECK_URL, timeout=15) as resp:
            return resp.read(64).decode().strip()


def parse_routes(text, pia=None):
    specs = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    return [Route(s, pia) for s in specs] or [Route("direct")]


def probe(route, url):
    """Return (final_url, size or None, supports_ranges, suggested_filename)."""
    try:
        with route.open(url, 0, 0) as resp:
            final = resp.geturl()
            name = filename_from(resp, final)
            if resp.status == 206:
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", resp.headers.get("Content-Range", ""))
                if m:
                    return final, int(m.group(1)), True, name
            length = resp.headers.get("Content-Length")
            return final, int(length) if length else None, False, name
    except urllib.error.HTTPError as e:
        if e.code != 416:  # 416 = empty file / range not satisfiable; retry without range
            raise
    with route.open(url) as resp:
        final = resp.geturl()
        length = resp.headers.get("Content-Length")
        return final, int(length) if length else None, False, filename_from(resp, final)


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #

class Segment:
    """A byte range of the file. `end` shrinks when another worker steals its tail."""

    def __init__(self, start, end):
        self.start, self.end = start, end   # end inclusive; None = unknown
        self.done = 0
        self.finished = False
        self.owner = None                   # Worker currently downloading it
        self.via = None                     # route index that wrote its latest bytes

    @property
    def length(self):
        return None if self.end is None else self.end - self.start + 1

    @property
    def remaining(self):
        return 0 if self.end is None else self.length - self.done

    @property
    def fraction(self):
        if self.finished:
            return 1.0
        return self.done / self.length if self.length else 0.0


class Worker:
    """One connection. Keeps pulling work - its own segment, then stolen halves -
    until nothing worth taking is left."""

    def __init__(self, idx, route):
        self.idx = idx
        self.route = route                  # index into Downloader.routes; rotates on failure
        self.seg = None
        self.bytes = 0
        self.status = "idle"                # active | retrying | idle | failed
        self.error = None


class Downloader:
    """States: probing -> downloading <-> pausing -> paused -> done | error | cancelled."""

    def __init__(self, url, folder, filename, connections, routes):
        self.url, self.folder, self.filename = url, folder, filename
        self.connections = connections
        self.routes = routes
        self.path = self.part = None
        self.size = None
        self.ranged = False
        self.segments = []
        self.workers = []
        self.state = "probing"
        self.error = None
        self._lock = threading.Lock()       # guards state transitions
        self._seg_lock = threading.Lock()   # guards segment ownership and splitting
        self._stop = threading.Event()
        self._cancelled = False
        self._threads = []

    @property
    def downloaded(self):
        return sum(s.done for s in list(self.segments))

    def route_label(self, route):
        return self.routes[route % len(self.routes)].label

    # -- public controls ---------------------------------------------------- #

    def start(self):
        threading.Thread(target=self._prepare, daemon=True).start()

    def pause(self):
        with self._lock:
            if self.state == "downloading":
                self.state = "pausing"
                self._stop.set()

    def resume(self):
        with self._lock:
            if self.state in ("paused", "error") and self.segments and os.path.exists(self.part):
                self._launch()
                return True
        return False

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self.state in ("downloading", "pausing"):
                self._stop.set()                  # supervisor will clean up
            elif self.state in ("paused", "error"):
                self._cleanup()
                self.state = "cancelled"

    def stop(self):
        self._stop.set()

    # -- internals ---------------------------------------------------------- #

    def _prepare(self):
        try:
            self.url, self.size, self.ranged, auto_name = probe(self.routes[0], self.url)
            self.path = unique_path(os.path.join(self.folder, safe_name(self.filename or auto_name)))
            self.filename = os.path.basename(self.path)
            self.part = self.path + ".part"
            self._plan()
            with open(self.part, "wb") as f:
                if self.size:
                    f.truncate(self.size)         # preallocate so segments can seek anywhere
        except Exception as e:
            with self._lock:
                if self._cancelled:
                    self._cleanup()
                    self.state = "cancelled"
                else:
                    self.error = f"Could not start download: {describe(e)}"
                    self.state = "error"
            return
        with self._lock:
            if self._cancelled:
                self._cleanup()
                self.state = "cancelled"
            else:
                self._launch()

    def _plan(self):
        if self.ranged and self.size:
            n = max(1, min(self.connections, self.size // MIN_SEGMENT))
            step = self.size // n
            self.segments = [Segment(i * step, self.size - 1 if i == n - 1 else (i + 1) * step - 1)
                             for i in range(n)]
        else:
            n = 1
            self.segments = [Segment(0, None if self.size is None else self.size - 1)]
        self.workers = [Worker(i, i % len(self.routes)) for i in range(n)]

    def _launch(self):
        # caller holds self._lock
        self._stop.clear()
        self.error = None
        self.state = "downloading"
        for s in self.segments:
            s.owner = None
        pending = [s for s in self.segments if not s.finished]
        for w in self.workers:
            w.status, w.error = "idle", None
            w.seg = pending.pop(0) if pending else None
            if w.seg:
                w.seg.owner = w
        self._threads = [threading.Thread(target=self._worker, args=(w,), daemon=True)
                         for w in self.workers]
        for t in self._threads:
            t.start()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        for t in self._threads:
            t.join()
        with self._lock:
            if self._cancelled:
                self._cleanup()
                self.state = "cancelled"
            elif all(s.finished for s in self.segments):
                try:
                    self._finalize()
                    self.state = "done"
                except OSError as e:
                    self.error = f"Could not save file: {e}"
                    self.state = "error"
            elif self._stop.is_set():
                self.state = "paused"
            else:                                 # every connection gave up
                self.error = next((w.error for w in self.workers if w.error),
                                  "some parts could not be downloaded")
                self.state = "error"

    def _next_segment(self, w):
        """Claim an orphaned segment, or split the biggest remaining one in half."""
        with self._seg_lock:
            for s in self.segments:
                if not s.finished and s.owner is None:
                    s.owner = w
                    return s
            if not (self.ranged and self.size):
                return None
            victim = max((s for s in self.segments if not s.finished and s.owner is not None),
                         key=lambda s: s.remaining, default=None)
            if victim is None or victim.remaining < STEAL_MIN:
                return None
            # The split point sits >= STEAL_MIN/2 ahead of the victim's position, far more
            # than one CHUNK, so the victim can't have written past it already.
            split = victim.start + victim.done + victim.remaining // 2
            new = Segment(split, victim.end)
            victim.end = split - 1
            new.owner = w
            self.segments.append(new)
            return new

    def _worker(self, w):
        seg = w.seg
        while not self._stop.is_set():
            if seg is None or seg.finished:
                seg = w.seg = self._next_segment(w)
                if seg is None:
                    w.status = "idle"
                    return
            if not self._fetch(w, seg):           # this connection gave up: hand the work back
                with self._seg_lock:
                    seg.owner = None
                w.seg, w.status = None, "failed"
                return

    def _fetch(self, w, seg):
        """Download `seg` until finished or stopped. Returns False if retries ran out."""
        retries = 0
        while not seg.finished and not self._stop.is_set():
            route_idx = w.route % len(self.routes)
            route = self.routes[route_idx]
            w.status = "active"
            try:
                if self.ranged:
                    resp = route.open(self.url, seg.start + seg.done, seg.end)
                    if resp.status != 206:
                        resp.close()
                        raise IOError("server ignored the range request")
                else:
                    seg.done = 0                  # no ranges: a retry must restart the stream
                    resp = route.open(self.url)
                with resp, open(self.part, "r+b") as f:
                    f.seek(seg.start + seg.done)
                    while not self._stop.is_set():
                        want = CHUNK if seg.length is None else min(CHUNK, seg.length - seg.done)
                        if want <= 0:
                            break                 # reached the end (possibly moved by a steal)
                        data = resp.read(want)
                        if not data:
                            break
                        if seg.length is not None:
                            data = data[:seg.length - seg.done]
                        f.write(data)
                        seg.done += len(data)
                        seg.via = route_idx
                        w.bytes += len(data)
                        retries = 0
                if self._stop.is_set():
                    break
                if seg.length is None or seg.done >= seg.length:
                    seg.finished = True
                else:
                    raise IOError("connection closed early")
            except Exception as e:
                if self._stop.is_set():
                    break
                retries += 1
                if retries > MAX_RETRIES:
                    w.error = f"{route.label}: {describe(e)}"
                    return False
                w.status = "retrying"
                w.route += 1                      # fail over to the next route
                delay = min(2 ** retries, 15)
                retry_after = getattr(e, "headers", None) and e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = min(int(retry_after), 60)
                self._stop.wait(delay)
        return True

    def _finalize(self):
        if self.size is None:
            with open(self.part, "r+b") as f:
                f.truncate(self.downloaded)
        os.replace(self.part, self.path)

    def _cleanup(self):
        if self.part:
            try:
                os.remove(self.part)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

TRACK, TEXT_C, MUTED_C, CANVAS_BG = "#2d2d2d", "#d4d4d4", "#8a8a8a", "#1e1e1e"
ROUTE_COLORS = ["#3b82f6", "#f59e0b", "#a855f7", "#22c55e", "#ec4899", "#06b6d4"]


def route_color(route):
    return ROUTE_COLORS[route % len(ROUTE_COLORS)]


class SpeedMeter:
    """Bytes/second over the last SPEED_WINDOW seconds."""

    def __init__(self):
        self.samples = collections.deque()

    def add(self, now, total):
        self.samples.append((now, total))
        while len(self.samples) > 2 and now - self.samples[0][0] > SPEED_WINDOW:
            self.samples.popleft()

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def clear(self):
        self.samples.clear()
ROUTES_HINT = ("# One route per line; connections are spread across them.\n"
               "# direct | pia | 10.8.0.2 (local/VPN IP) | http://host:port | socks5://user:pass@host:port\n"
               "direct\n")


class PiaDialog(tk.Toplevel):
    """Collects PIA SOCKS5 credentials. Sets self.result to a dict on Save."""

    def __init__(self, master, pia):
        super().__init__(master)
        self.title("PIA SOCKS5 proxy")
        self.resizable(False, False)
        self.transient(master)
        self.result = None

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, justify="left", wraplength=380, text=(
            "Use the SOCKS credentials generated in the PIA Client Control Panel "
            "(\"Generate PPTP/L2TP/SOCKS Password\"). They are different from your "
            "normal PIA login; the username usually starts with 'x'.")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.env = env_pia_configured()
        if self.env:
            ttk.Label(frm, foreground="#15803d", wraplength=380, text=(
                f"Credentials found in .env ({os.environ['PIA_SOCKS_USER']}). Leave the "
                "fields below blank to use them.")).grid(
                row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.vars = {
            "user": tk.StringVar(value=pia.get("user", "")),
            "password": tk.StringVar(value=pia.get("password", "")),
            "host": tk.StringVar(value=pia.get("host", PIA_HOST)),
            "port": tk.StringVar(value=str(pia.get("port", PIA_PORT))),
        }
        for row, (key, text) in enumerate(
                [("user", "Username"), ("password", "Password"), ("host", "Server"), ("port", "Port")], 1):
            ttk.Label(frm, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(frm, textvariable=self.vars[key], width=40,
                      show="•" if key == "password" else "").grid(row=row, column=1, sticky="ew", pady=3)

        self.remember = tk.BooleanVar(value=bool(pia.get("password")))
        ttk.Checkbutton(frm, variable=self.remember,
                        text=f"Remember password (stored unencrypted in {CONFIG_PATH})").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.test_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.test_var, wraplength=380).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.test_btn = ttk.Button(btns, text="Test", command=self.test)
        self.test_btn.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _settings(self):
        pia = {k: v.get().strip() for k, v in self.vars.items()}
        pia["remember"] = self.remember.get()
        return pia

    def test(self):
        pia = self._settings()
        self.test_btn.config(state="disabled")
        self.test_var.set("Testing…")

        def work():
            try:
                pia_ip = Route("pia", pia).public_ip()
                main_ip = Route("direct").public_ip()
                msg = f"Works. PIA IP: {pia_ip}   (main IP: {main_ip})"
                if pia_ip == main_ip:
                    msg += "\nSame as main IP - is the PIA app connected in full-tunnel mode?"
            except Exception as e:
                msg = f"Failed: {describe(e)}"
            self.after(0, lambda: self._tested(msg))

        threading.Thread(target=work, daemon=True).start()

    def _tested(self, msg):
        if self.winfo_exists():
            self.test_btn.config(state="normal")
            self.test_var.set(msg)

    def save(self):
        pia = self._settings()
        if (not pia["user"] or not pia["password"]) and not self.env:
            messagebox.showwarning("PIA", "Enter the SOCKS username and password.", parent=self)
            return
        if not pia["port"].isdigit():
            messagebox.showwarning("PIA", "Port must be a number.", parent=self)
            return
        self.result = pia
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fast Downloader")
        self.geometry("780x640")
        self.minsize(600, 520)
        self.dl = None
        self.speed = SpeedMeter()
        self.wspeed = collections.defaultdict(SpeedMeter)
        self._last_state = None
        self.cfg = load_config()
        self.pia = self.cfg.get("pia", {})
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fast_downloader.ico")
        if os.path.exists(icon):
            try:
                self.iconbitmap(default=icon)
            except tk.TclError:
                pass
        self._build()
        if env_pia_configured() and not self.cfg.get("env_pia_added"):
            self._append_routes(["direct", "pia"])    # once; removing it later sticks
            self.cfg["env_pia_added"] = True
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(TICK_MS, self._tick)

    def _build(self):
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="URL").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda e: self.start())
        url_entry.focus_set()

        ttk.Label(frm, text="Save to").grid(row=1, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=self.cfg.get("folder") or default_dir())
        ttk.Entry(frm, textvariable=self.dir_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self.browse).grid(row=1, column=2, sticky="ew", **pad)

        ttk.Label(frm, text="File name").grid(row=2, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.name_var).grid(row=2, column=1, sticky="ew", **pad)
        seg_box = ttk.Frame(frm)
        seg_box.grid(row=2, column=2, sticky="e", **pad)
        ttk.Label(seg_box, text="Segments").pack(side="left")
        self.seg_var = tk.StringVar(value=str(self.cfg.get("segments", 8)))
        ttk.Spinbox(seg_box, from_=1, to=32, width=4, textvariable=self.seg_var).pack(side="left", padx=(4, 0))

        ttk.Label(frm, text="Routes").grid(row=3, column=0, sticky="nw", **pad)
        self.routes_txt = tk.Text(frm, height=5, width=40, wrap="none", font=("Consolas", 9))
        self.routes_txt.insert("1.0", self.cfg.get("routes") or ROUTES_HINT)
        self.routes_txt.grid(row=3, column=1, sticky="ew", **pad)
        route_btns = ttk.Frame(frm)
        route_btns.grid(row=3, column=2, sticky="n", **pad)
        ttk.Button(route_btns, text="PIA…", command=self.pia_settings).pack(fill="x", pady=(0, 4))
        ttk.Button(route_btns, text="Add local IPs", command=self.add_local_ips).pack(fill="x", pady=(0, 4))
        self.check_btn = ttk.Button(route_btns, text="Check IPs", command=self.check_ips)
        self.check_btn.pack(fill="x")

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        self.start_btn = ttk.Button(btns, text="Download", command=self.start)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self.toggle_pause, state="disabled")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        for b in (self.start_btn, self.pause_btn, self.cancel_btn):
            b.pack(side="left", padx=(0, 6))

        self.progress = ttk.Progressbar(frm, maximum=1000)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

        self.status_var = tk.StringVar(value="Paste a URL and press Download.")
        self.status_lbl = ttk.Label(frm, textvariable=self.status_var)
        self.status_lbl.grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        self.canvas = tk.Canvas(frm, height=180, background=CANVAS_BG, highlightthickness=0)
        self.canvas.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(7, weight=1)

        self.info_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.info_var, foreground="#666").grid(
            row=8, column=0, columnspan=3, sticky="w", **pad)

    # -- actions ------------------------------------------------------------ #

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get() or default_dir())
        if folder:
            self.dir_var.set(folder)

    def _routes(self):
        try:
            return parse_routes(self.routes_txt.get("1.0", "end"), self.pia)
        except ValueError as e:
            messagebox.showerror("Fast Downloader", f"Bad route: {e}")
            return None

    def _append_routes(self, specs):
        existing = {ln.strip().lower() for ln in self.routes_txt.get("1.0", "end").splitlines()}
        new = [s for s in specs if s.lower() not in existing]
        if new:
            if self.routes_txt.get("end-2c", "end-1c") != "\n":
                self.routes_txt.insert("end-1c", "\n")
            self.routes_txt.insert("end-1c", "\n".join(new) + "\n")

    def add_local_ips(self):
        ips = local_ipv4s()
        if not ips:
            messagebox.showinfo("Fast Downloader", "No local IPv4 addresses found.")
            return
        self._append_routes(ips)

    def pia_settings(self):
        dlg = PiaDialog(self, self.pia)
        self.wait_window(dlg)
        if dlg.result:
            self.pia = dlg.result
            self._append_routes(["direct", "pia"])
            self._save_settings()

    def _save_settings(self):
        pia = dict(self.pia)
        if not pia.pop("remember", False):
            pia.pop("password", None)
        else:
            pia["remember"] = True
        self.cfg.update(
            pia=pia,
            routes=self.routes_txt.get("1.0", "end-1c"),
            folder=self.dir_var.get().strip(),
            segments=self.seg_var.get().strip(),
        )
        save_config(self.cfg)

    def check_ips(self):
        routes = self._routes()
        if not routes:
            return
        self.check_btn.config(state="disabled", text="Checking…")

        def work():
            lines = []
            for r in routes:
                try:
                    lines.append(f"{r.label:<32} →  {r.public_ip()}")
                except Exception as e:
                    lines.append(f"{r.label:<32} →  FAILED ({describe(e)})")
            self.after(0, lambda: self._show_ip_results(lines))

        threading.Thread(target=work, daemon=True).start()

    def _show_ip_results(self, lines):
        self.check_btn.config(state="normal", text="Check IPs")
        messagebox.showinfo("Public IP per route", "\n".join(lines))

    def start(self):
        if self.dl and self.dl.state in ("probing", "downloading", "pausing", "paused"):
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Fast Downloader", "Enter a URL first.")
            return
        if not re.match(r"https?://", url, re.I):
            url = "https://" + url
            self.url_var.set(url)
        folder = self.dir_var.get().strip() or default_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Fast Downloader", f"Cannot use folder:\n{e}")
            return
        try:
            n = max(1, min(32, int(self.seg_var.get())))
        except ValueError:
            n = 8
        routes = self._routes()
        if not routes:
            return
        self._reset_speed()
        self.dl = Downloader(url, folder, self.name_var.get().strip() or None, n, routes)
        self.dl.start()

    def toggle_pause(self):
        dl = self.dl
        if not dl:
            return
        if dl.state == "downloading":
            dl.pause()
        elif dl.state in ("paused", "error"):
            self._reset_speed()
            if not dl.resume():
                self.start()                      # failed before any data: start over

    def _reset_speed(self):
        self.speed.clear()
        self.wspeed.clear()

    def cancel(self):
        if self.dl:
            self.dl.cancel()

    def _on_close(self):
        if self.dl and self.dl.state in ("probing", "downloading", "pausing"):
            if not messagebox.askyesno("Fast Downloader", "A download is in progress. Quit anyway?"):
                return
            self.dl.stop()
        self._save_settings()
        self.destroy()

    # -- periodic UI refresh ------------------------------------------------ #

    def _tick(self):
        dl = self.dl
        if dl:
            if dl.state != self._last_state:
                self._last_state = dl.state
                self._on_state(dl)
            self._refresh(dl)
        self.after(TICK_MS, self._tick)

    def _on_state(self, dl):
        s = dl.state
        busy = s in ("probing", "downloading", "pausing", "paused")
        self.start_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy or s == "error" else "disabled")
        if s == "downloading":
            self.pause_btn.config(state="normal", text="Pause")
        elif s == "paused":
            self.pause_btn.config(state="normal", text="Resume")
        elif s == "error":
            self.pause_btn.config(state="normal", text="Retry")
        else:
            self.pause_btn.config(state="disabled", text="Pause")
        self.status_lbl.config(foreground="#c0392b" if s == "error" else "")
        if s == "done":
            self.progress.config(mode="determinate", value=1000)

    def _refresh(self, dl):
        now, done = time.monotonic(), dl.downloaded
        if dl.state == "downloading":
            self.speed.add(now, done)
            for w in dl.workers:
                self.wspeed[w.idx].add(now, w.bytes)
        speed = self.speed.rate()

        if dl.size:
            self.progress.config(mode="determinate", value=1000 * done / dl.size)
        elif dl.state == "downloading":
            self.progress.config(mode="indeterminate")
            self.progress.step(8)

        pct = f" ({100 * done / dl.size:.1f}%)" if dl.size else ""
        amount = f"{human_size(done)} / {human_size(dl.size)}{pct}"
        s = dl.state
        if s == "probing":
            text = "Contacting server…"
        elif s in ("downloading", "pausing"):
            eta = (dl.size - done) / speed if dl.size and speed > 0 else None
            text = f"{amount}   ·   {human_size(speed)}/s   ·   ETA {human_time(eta)}"
            if s == "pausing":
                text = "Pausing…   " + amount
        elif s == "paused":
            text = f"Paused at {amount}"
        elif s == "done":
            text = f"Done — saved to {dl.path}"
        elif s == "error":
            text = f"Error: {dl.error}"
        else:
            text = "Cancelled."
        self.status_var.set(text)

        if dl.workers:
            n_routes = len({w.route % len(dl.routes) for w in dl.workers})
            mode = ("resumable, idle connections take over slow parts" if dl.ranged
                    else "server doesn't support ranges — single connection")
            self.info_var.set(f"{dl.filename}  ·  {human_size(dl.size)}  ·  {len(dl.workers)} "
                              f"connection(s) over {n_routes} route(s)  ·  {mode}")
        self._draw(dl)

    def _draw(self, dl):
        c = self.canvas
        c.delete("all")
        if not dl.workers:
            return
        w, h = c.winfo_width(), c.winfo_height()
        small = ("Segoe UI", 8)
        mono = ("Consolas", 8)

        # File map: each downloaded range, coloured by the route that fetched it.
        x0, x1 = 8, w - 8
        c.create_text(x0, 9, text="File map", anchor="w", fill=MUTED_C, font=small)
        y, mh = 18, 14
        c.create_rectangle(x0, y, x1, y + mh, fill=TRACK, outline="")
        if dl.size:
            scale = (x1 - x0) / dl.size
            for sg in sorted(list(dl.segments), key=lambda s: s.start):
                sx = x0 + sg.start * scale
                if sg.done and sg.via is not None:
                    c.create_rectangle(sx, y, sx + sg.done * scale, y + mh,
                                       fill=route_color(sg.via), outline="")
                if sg.start:
                    c.create_line(sx, y, sx, y + mh, fill=CANVAS_BG)
        elif dl.downloaded:
            c.create_rectangle(x0, y, x1, y + mh, fill=route_color(0), outline="")

        # One row per connection: progress through its current piece, speed, total, route.
        top = y + mh + 22
        c.create_text(x0, top - 10, text="Connections", anchor="w", fill=MUTED_C, font=small)
        workers = dl.workers
        n = len(workers)
        gap = 4 if n <= 12 else 2
        row_h = max(2, min(20, (h - top - gap * (n + 1)) / n))
        bx0, bx1 = 40, max(60, w - 290)
        for i, wk in enumerate(workers):
            ry = top + gap + i * (row_h + gap)
            route = wk.route % len(dl.routes)
            seg = wk.seg
            c.create_rectangle(bx0, ry, bx1, ry + row_h, fill=TRACK, outline="")
            frac = seg.fraction if seg else 0.0
            if frac > 0 and wk.status != "failed":
                c.create_rectangle(bx0, ry, bx0 + (bx1 - bx0) * frac, ry + row_h,
                                   fill=route_color(route), outline="")
            if row_h < 11:
                continue
            mid = ry + row_h / 2
            c.create_text(8, mid, text=f"#{i + 1}", anchor="w", fill=TEXT_C, font=small)
            if wk.status == "active" and seg:
                state = f"{frac * 100:5.1f}%" if seg.length else "  ... "
            else:
                state = {"retrying": "retry ", "failed": "FAILED"}.get(wk.status, " idle ")
            rate = self.wspeed[wk.idx].rate() if wk.status == "active" else 0.0
            label = dl.route_label(route)
            label = label if len(label) <= 20 else label[:19] + "…"
            c.create_text(bx1 + 8, mid, anchor="w", fill=TEXT_C, font=mono,
                          text=f"{state} {human_size(rate) + '/s':>10} {human_size(wk.bytes):>9}")
            c.create_text(bx1 + 190, mid, anchor="w", fill=route_color(route), font=mono, text=label)


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    try:  # crisp text on high-DPI Windows; own taskbar icon instead of Python's
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FastDownloader")
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()

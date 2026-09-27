"""Fast Downloader - segmented, multi-connection HTTP downloader with a Tkinter UI.

The file is split into byte ranges that are fetched in parallel and written
straight into a preallocated ``.part`` file, which is renamed when complete.

Each segment can travel over a different *route*, so segments can come from
different IP addresses (e.g. your normal connection plus PIA VPN tunnels in
several regions).

Route syntax (one per line in the UI):
    direct              normal connection (system default)
    vpn:us_east         a PIA OpenVPN tunnel to that region (needs OpenVPN 2.7+ and
                        PIA_VPN_USER / PIA_VPN_PASS in .env; one UAC prompt per
                        connect - see README "PIA over OpenVPN")
    10.8.0.2            bind to this local IP (e.g. another network adapter)
    http://host:port    HTTP proxy  (user:pass@ allowed)

Standard library only.
"""

import collections
import http.client
import ipaddress
import itertools
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
STATE_SUFFIX = ".fdl"             # progress record: <name>.part.fdl next to <name>.part
STATE_SAVE_EVERY = 5.0            # seconds between progress-record saves while downloading
REPAIR_MARGIN = 1024 * 1024       # re-fetch this much at each edge of recovered ranges
MAX_CONNECTIONS = 128             # upper limit for the Segments setting
MAX_RETRIES = 5
TIMEOUT = 30
TICK_MS = 200
SPEED_WINDOW = 3.0                # seconds of history used for the speed readout
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FastDownloader/1.0"
IP_CHECK_URL = "https://api.ipify.org"
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
    """`path`, or `name (1).ext`, `name (2).ext`… - skipping names whose file *or*
    partial download (.part) already exists, so neither gets overwritten."""
    taken = lambda p: os.path.exists(p) or os.path.exists(p + ".part")
    if not taken(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while taken(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def allocated_ranges(path):
    """[(start, end_exclusive)] of the parts of a *sparse* file that hold data, or None.

    Windows only. Downloads are written into sparse files, so the ranges that were
    ever written are exactly the allocated ones - which lets a partial download
    that lost its progress record be recovered. Non-sparse files are fully
    allocated, so they return None (nothing can be told apart)."""
    if os.name != "nt":
        return None
    import ctypes, msvcrt, stat
    from ctypes import wintypes
    try:
        if not os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_SPARSE_FILE:
            return None
    except OSError:
        return None

    class Range(ctypes.Structure):
        _fields_ = [("offset", ctypes.c_longlong), ("length", ctypes.c_longlong)]

    FSCTL_QUERY_ALLOCATED_RANGES, ERROR_MORE_DATA = 0x940CF, 234
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = os.path.getsize(path)
    ranges, start = [], 0
    out = (Range * 1024)()
    with open(path, "rb") as f:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
        while start < size:
            query = Range(start, size - start)
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(handle, FSCTL_QUERY_ALLOCATED_RANGES,
                                          ctypes.byref(query), ctypes.sizeof(query),
                                          out, ctypes.sizeof(out), ctypes.byref(returned), None)
            n = returned.value // ctypes.sizeof(Range)
            ranges += [(out[i].offset, out[i].offset + out[i].length) for i in range(n)]
            if ok:
                break
            if ctypes.get_last_error() != ERROR_MORE_DATA or n == 0:
                return None
            start = ranges[-1][1]
    return ranges


def set_size_sparse(f, size):
    """Give file `f` its full size without writing it out.

    On Windows, truncate() extends a file by physically writing zeros (a 250 GB
    download would first write 250 GB of zeros), and writing far past the end of a
    normal NTFS file makes Windows zero-fill the gap. Marking the file sparse first
    means only the ranges actually written take disk space or time."""
    if os.name == "nt":
        import ctypes, msvcrt
        from ctypes import wintypes
        FSCTL_SET_SPARSE = 0x900C4
        returned = wintypes.DWORD()
        ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno())), FSCTL_SET_SPARSE,
            None, 0, None, 0, ctypes.byref(returned), None)
    f.seek(size - 1)
    f.write(b"\0")                         # sets the size; everything before it is a hole
    f.flush()


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


def _bound_opener(ip):
    """An opener whose connections leave from local address `ip`."""
    src = (str(ip), 0)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _ConnHTTPHandler(http.client.HTTPConnection, source_address=src),
        _ConnHTTPSHandler(http.client.HTTPSConnection, source_address=src),
    )


class Route:
    """One way out to the internet. Each has its own urllib opener."""

    def __init__(self, spec, vpn=None):
        self.spec = spec.strip()
        s = self.spec
        self.tunnel = None
        self.is_vpn = s.lower().startswith("vpn:")
        if self.is_vpn:
            if vpn is None:
                raise ValueError("VPN routes are only available in the app")
            self.tunnel = vpn.tunnel(s[4:].strip().lower())
            self.label = f"VPN {self.tunnel.region}"
            self._openers = {}                    # tunnel IP -> opener (IP can change on reconnect)
            return
        if s.lower() == "pia":
            raise ValueError("the PIA SOCKS route was removed - use 'VPN regions…' to add "
                             "vpn:<region> routes instead")

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
            self.opener = _bound_opener(ip)
            return

        parsed = urllib.parse.urlparse(s)
        scheme = parsed.scheme.lower()
        if not parsed.hostname:
            raise ValueError(f"Unrecognised route: {s!r}")
        if scheme not in ("http", "https"):
            raise ValueError(f"Unsupported proxy scheme: {scheme!r} (only http:// proxies)")
        self.label = f"{scheme}://{parsed.hostname}:{parsed.port or ''}".rstrip(":")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": s, "https": s}))

    def _current_opener(self):
        if self.tunnel is None:
            return self.opener
        ip = self.tunnel.ip
        if not ip:
            raise IOError(f"VPN {self.tunnel.region} is not connected")
        if ip not in self._openers:
            self._openers[ip] = _bound_opener(ip)
        return self._openers[ip]

    def open(self, url, start=None, end=None, timeout=TIMEOUT):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self._current_opener().open(urllib.request.Request(url, headers=headers), timeout=timeout)

    def public_ip(self):
        with self.open(IP_CHECK_URL, timeout=15) as resp:
            return resp.read(64).decode().strip()


def parse_routes(text, vpn=None):
    specs = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    return [Route(s, vpn) for s in specs] or [Route("direct")]


# --------------------------------------------------------------------------- #
# PIA over OpenVPN: "vpn:<region>" routes
# --------------------------------------------------------------------------- #
#
# Each region runs its own openvpn.exe (elevated - Windows only lets admins set up
# adapters and routes). The tunnel is told NOT to take over the default route:
# PIA's pushed redirect-gateway / IPv6 routes / DNS are filtered out and replaced by
# a 0.0.0.0/0 route with metric 9000, which normal traffic never prefers. Sockets
# bound to the tunnel's IP use that route (Windows picks routes on the interface
# that owns the source address), so only those connections go through PIA.
#
# The app talks to each openvpn over its localhost management interface
# (password-protected): it supplies the PIA login, watches state, reads the
# tunnel IP, and sends SIGTERM to disconnect. An elevated watchdog kills the
# openvpn processes if the app exits without disconnecting them.

OPENVPN_PATHS = [r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                 r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe"]
PIA_OVPN_ZIP = "https://www.privateinternetaccess.com/openvpn/openvpn.zip"
PIA_OVPN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pia_openvpn")
VPN_CONNECT_TIMEOUT = 90
MAX_VPN_TUNNELS = 8        # all tunnels start from one elevated command line; keep it well short
VPN_ARGS = [
    "--management-hold", "--management-query-passwords", "--auth-nocache",
    "--auth-retry", "none",
    "--pull-filter", "ignore", "redirect-gateway",
    "--pull-filter", "ignore", "route-ipv6",
    "--pull-filter", "ignore", "ifconfig-ipv6",
    "--pull-filter", "ignore", "dhcp-option",
    "--pull-filter", "ignore", "block-outside-dns",
    # OpenVPN's default socket buffers on Windows are small: measured to PIA US West,
    # 0.1 MB/s per connection with them vs 3.9 MB/s with 512 KB. Don't let the server
    # push them back down.
    "--sndbuf", "524288", "--rcvbuf", "524288",
    "--pull-filter", "ignore", "sndbuf",
    "--pull-filter", "ignore", "rcvbuf",
    "--route", "0.0.0.0", "0.0.0.0", "vpn_gateway", "9000",
    "--data-ciphers", "AES-128-GCM:AES-256-GCM:AES-128-CBC",
    "--data-ciphers-fallback", "AES-128-CBC",
    "--allow-compression", "asym",
    "--verb", "3",
]


def find_openvpn():
    for p in OPENVPN_PATHS:
        if os.path.exists(p):
            return p
    import shutil
    return shutil.which("openvpn")


def pia_regions():
    """{region: .ovpn path}, downloading PIA's config set on first use."""
    if not os.path.isdir(PIA_OVPN_DIR) or not any(n.endswith(".ovpn") for n in os.listdir(PIA_OVPN_DIR)):
        import io, zipfile
        req = urllib.request.Request(PIA_OVPN_ZIP, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        os.makedirs(PIA_OVPN_DIR, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if base.endswith((".ovpn", ".crt", ".pem")):
                    with open(os.path.join(PIA_OVPN_DIR, base), "wb") as f:
                        f.write(z.read(name))
    return {n[:-5].lower(): os.path.join(PIA_OVPN_DIR, n)
            for n in sorted(os.listdir(PIA_OVPN_DIR)) if n.endswith(".ovpn")}


def pia_vpn_credentials():
    user, password = os.environ.get("PIA_VPN_USER"), os.environ.get("PIA_VPN_PASS")
    if not user or not password:
        raise ValueError("VPN routes need your normal PIA login (p…) as "
                         "PIA_VPN_USER / PIA_VPN_PASS in .env")
    return user, password


def _mgmt_escape(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


class VpnTunnel:
    """One openvpn.exe process for one PIA region, driven over its management port."""

    def __init__(self, region, config):
        self.region, self.config = region, config
        self.state = "down"          # down | starting | connecting | connected | reconnecting | failed
        self.ip = None
        self.error = None
        self.adapter = None
        self.log = collections.deque(maxlen=60)     # recent OpenVPN log lines
        self._sock = None
        self._send_lock = threading.Lock()

    @property
    def running(self):
        return self.state in ("starting", "connecting", "connected", "reconnecting")

    def prepare(self, workdir, adapter):
        """Pick a management port and password; return the openvpn argument list."""
        self.adapter = adapter
        import secrets
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self._mgmt_pw = secrets.token_hex(16)
        self._pwfile = os.path.join(workdir, f"mgmt_{self.region}.pw")
        with open(self._pwfile, "wb") as f:          # binary: a \r would become part of the password
            f.write(self._mgmt_pw.encode() + b"\n")
        self.state, self.ip, self.error = "starting", None, None
        return ["--config", self.config,
                "--dev-node", adapter, "--disable-dco",
                "--management", "127.0.0.1", str(self.port), self._pwfile] + VPN_ARGS

    def attach(self, credentials):
        threading.Thread(target=self._run, args=(credentials,), daemon=True).start()

    def stop(self):
        self._send("signal SIGTERM")

    def _send(self, cmd):
        with self._send_lock:
            if self._sock:
                try:
                    self._sock.sendall((cmd + "\r\n").encode())
                except OSError:
                    pass

    def _fail(self, msg):
        if not self.error:
            self.error = msg
        self.state = "failed"
        self.ip = None
        self.stop()

    def _run(self, credentials):
        deadline = time.time() + 60                   # UAC prompt + process start
        while self._sock is None and time.time() < deadline and self.state == "starting":
            try:
                self._sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            except OSError:
                time.sleep(0.5)
        if self._sock is None:
            if self.state == "starting":
                self._fail("OpenVPN did not start")
            return
        try:
            self._session(credentials)
        except OSError:
            pass
        finally:
            try:
                os.remove(self._pwfile)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
            self.ip = None
            if self.state != "failed":
                self.state = "down"

    def _session(self, credentials):
        self.state = "connecting"
        self._sock.settimeout(10)
        self._sock.recv(100)                          # "ENTER PASSWORD:" (no newline)
        self._sock.settimeout(None)
        f = self._sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        # OpenVPN drops commands that arrive while it is still answering the previous
        # one, so they go out one at a time, the next after each SUCCESS/ERROR reply.
        queue = ["state on", "log on", "hold release"]
        waiting = True
        self._send(self._mgmt_pw)

        def next_cmd():
            nonlocal waiting
            waiting = bool(queue)
            if queue:
                self._send(queue.pop(0))

        for line in f:
            line = line.strip()
            if line.startswith(("SUCCESS:", "ERROR:")):
                if line.startswith("SUCCESS: password is correct"):
                    try:
                        os.remove(self._pwfile)
                    except OSError:
                        pass
                if waiting:
                    next_cmd()
            elif line.startswith(">LOG:"):
                self.log.append(line[5:].split(",", 2)[-1])
            elif line.startswith(">PASSWORD:Need 'Auth'"):
                user, password = credentials
                queue.extend([f'username "Auth" "{_mgmt_escape(user)}"',
                              f'password "Auth" "{_mgmt_escape(password)}"'])
                if not waiting:
                    next_cmd()
            elif line.startswith(">PASSWORD:Verification Failed"):
                self._fail("PIA rejected the login - PIA_VPN_USER / PIA_VPN_PASS must be "
                           "your normal PIA login (p…)")
            elif line.startswith(">FATAL:"):
                self._fail(line[7:])
            elif line.startswith(">STATE:"):
                parts = line[7:].split(",")
                st = parts[1] if len(parts) > 1 else ""
                if st == "CONNECTED":
                    if parts[2] == "SUCCESS" and len(parts) > 3 and parts[3]:
                        self.ip, self.state = parts[3], "connected"
                    else:
                        errors = [m for m in self.log if "ERROR" in m or "FAILED" in m or "failed" in m]
                        self._fail("connected with errors" + (f": {errors[-1]}" if errors else ""))
                elif st in ("RECONNECTING", "WAIT", "RESOLVE", "TCP_CONNECT", "AUTH", "GET_CONFIG",
                            "ASSIGN_IP", "ADD_ROUTES", "AUTH_PENDING"):
                    self.ip = None
                    if self.state == "connected":
                        self.state = "reconnecting"
                elif st == "EXITING":
                    self.ip = None


class VpnManager:
    """Owns the app's tunnels: starts them with one UAC prompt, stops them on exit."""

    def __init__(self):
        self.tunnels = {}
        self._regions = None
        self._lock = threading.Lock()

    def regions(self):
        if self._regions is None:
            self._regions = pia_regions()
        return self._regions

    def tunnel(self, region):
        try:
            regions = self.regions()
        except OSError as e:
            raise ValueError(f"could not download PIA's OpenVPN configs: {describe(e)}") from None
        if region not in regions:
            raise ValueError(f"unknown PIA region {region!r} - use 'VPN regions…' to pick one")
        return self.tunnels.setdefault(region, VpnTunnel(region, regions[region]))

    @property
    def active(self):
        return [t for t in self.tunnels.values() if t.running]

    def ensure(self, tunnels, cancelled=lambda: False):
        """Connect every tunnel in `tunnels` that isn't already up. Blocks; raises on failure."""
        with self._lock:
            start = [t for t in dict.fromkeys(tunnels) if not t.running]
            if start:
                self._launch(start)
            deadline = time.time() + VPN_CONNECT_TIMEOUT
            pending = list(dict.fromkeys(tunnels))
            while any(t.state in ("starting", "connecting") for t in pending):
                if cancelled():
                    return
                if time.time() > deadline:
                    for t in pending:
                        if t.state != "connected":
                            t._fail("timed out connecting")
                    break
                time.sleep(0.3)
            bad = [t for t in pending if t.state != "connected"]
            if bad:
                raise IOError("; ".join(f"VPN {t.region}: {t.error or t.state}" for t in bad))

    def _launch(self, tunnels):
        import base64, subprocess, tempfile
        openvpn = find_openvpn()
        if not openvpn:
            raise IOError("OpenVPN is not installed - get it from openvpn.net/community")
        credentials = pia_vpn_credentials()
        workdir = tempfile.mkdtemp(prefix="fastdl_vpn_")
        q = lambda v: "'" + v.replace("'", "''") + "'"
        lines = ["$ErrorActionPreference = 'SilentlyContinue'", "$p = @()", "$new = $false"]
        # PIA's configs rule out DCO (whose adapters OpenVPN 2.7 creates on demand), so each
        # tunnel gets its own tap-windows6 adapter, created once and reused afterwards.
        tapctl = os.path.join(os.path.dirname(openvpn), "tapctl.exe")
        busy = {t.adapter for t in self.tunnels.values() if t.running and t not in tunnels}
        free = (f"FastDL VPN {i}" for i in itertools.count(1) if f"FastDL VPN {i}" not in busy)
        for t in tunnels:
            adapter = t.adapter = next(free)
            lines.append(f"if (-not (Get-NetAdapter -Name {q(adapter)})) "
                         f"{{ & {q(tapctl)} create --hwid 'root\\tap0901' --name {q(adapter)} | Out-Null; $new = $true }}")
        lines.append("if ($new) { Start-Sleep -Seconds 3 }")   # let Windows finish setting up new adapters
        # Routes left behind by a tunnel that was killed rather than shut down cleanly
        clear_routes = [f"Get-NetRoute -InterfaceAlias {q(t.adapter)} -DestinationPrefix '0.0.0.0/0' | "
                        "Remove-NetRoute -Confirm:$false" for t in tunnels]
        lines += clear_routes
        for t in tunnels:
            args = subprocess.list2cmdline(t.prepare(workdir, t.adapter))
            lines.append(f"$p += Start-Process -FilePath {q(openvpn)} -ArgumentList {q(args)} "
                         f"-WorkingDirectory {q(os.path.dirname(t.config))} -WindowStyle Hidden -PassThru")
        # Watchdog: if the app goes away without disconnecting, take the tunnels down.
        lines += [f"Wait-Process -Id {os.getpid()}",
                  "Start-Sleep -Seconds 2",
                  "$p | Where-Object { -not $_.HasExited } | Stop-Process -Force",
                  "Start-Sleep -Seconds 1"] + clear_routes
        encoded = base64.b64encode("\n".join(lines).encode("utf-16-le")).decode()
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe",
            f"-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {encoded}",
            None, 0)
        if rc <= 32:
            for t in tunnels:
                t.state, t.error = "failed", "the administrator (UAC) prompt was declined"
                try:
                    os.remove(t._pwfile)
                except OSError:
                    pass
            raise IOError("the administrator (UAC) prompt was declined")
        for t in tunnels:
            t.attach(credentials)

    def disconnect_all(self):
        for t in self.tunnels.values():
            if t.running:
                t.stop()


ProbeResult = collections.namedtuple("ProbeResult", "url size ranged name etag modified")


def probe(route, url):
    """What the server says about `url`: final URL after redirects, size (or None),
    range support, a filename, and its version markers (ETag / Last-Modified)."""
    def result(resp, size, ranged):
        final = resp.geturl()
        return ProbeResult(final, size, ranged, filename_from(resp, final),
                           resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
    try:
        with route.open(url, 0, 0) as resp:
            if resp.status == 206:
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", resp.headers.get("Content-Range", ""))
                if m:
                    return result(resp, int(m.group(1)), True)
            length = resp.headers.get("Content-Length")
            return result(resp, int(length) if length else None, False)
    except urllib.error.HTTPError as e:
        if e.code != 416:  # 416 = empty file / range not satisfiable; retry without range
            raise
    with route.open(url) as resp:
        length = resp.headers.get("Content-Length")
        return result(resp, int(length) if length else None, False)


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


def segments_from_runs(size, runs, margin):
    """Segments covering [0, size): finished ones for the data `runs`, empty ones for the gaps.

    Each run is trimmed by `margin` at both ends (not at offset 0): a run's last write
    may have been cut short, and its start is rounded down to a cluster boundary, so
    the edges can hold zeros that were never downloaded."""
    done = []
    for a, b in sorted(runs):
        a, b = (a if a == 0 else a + margin), min(b, size) - margin
        if b > a:
            if done and a <= done[-1][1]:
                done[-1][1] = max(done[-1][1], b)
            else:
                done.append([a, b])
    segs, pos = [], 0
    for a, b in done + [[size, size]]:
        if a > pos:
            segs.append(Segment(pos, a - 1))                 # still to download
        if b > a:
            s = Segment(a, b - 1)
            s.done, s.finished = s.length, True
            segs.append(s)
        pos = max(pos, b)
    return segs


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
        self.move = False                   # set by the rebalancer: end this request, reconnect on self.route


THROTTLE_CODES = (429, 503)     # "too many requests" / "slow down"
RAMP_UP_AFTER = 20.0            # quiet seconds before a throttled route may add a connection
REBALANCE_EVERY = 3.0           # seconds between rebalancing checks
REBALANCE_RATIO = 0.6           # move a connection when a route runs < 60% of the best route's speed


class RouteGate:
    """Adaptive connection limit for one route (= one IP address).

    Servers limit connections per IP, each differently (e.g. ~9 vs ~3) and usually
    without saying so. So the limit is learned, like TCP congestion control: drop it
    to what the server accepted on a 429/503 (halve it if that keeps happening), add one
    back after RAMP_UP_AFTER seconds without one. A throttle also
    pauses the route (2, 4, 8… s, or the server's Retry-After) and spaces out its
    new requests, which matters for servers that count request rate, not just connections.
    """

    def __init__(self, start, cap):
        self.limit = start
        self.cap = cap
        self.active = 0
        self.cool_until = 0.0
        self.interval = 0.0              # minimum seconds between new requests
        self.last_start = 0.0
        self.last_throttle = -RAMP_UP_AFTER
        self.last_raise = 0.0
        self.strikes = 0                 # throttles in a row; drives the cooldown length
        self.throttles = 0               # total, for the UI

    def ready(self, now):
        return (self.active < self.limit and now >= self.cool_until
                and now - self.last_start >= self.interval)

    def throttled(self, now, retry_after=None):
        self.throttles += 1
        self.strikes += 1
        # `active` includes the rejected request, so active - 1 is what the server just
        # accepted - exact for connection caps. Repeated strikes mean something else (a
        # request-rate limit or a ban), so back off harder.
        current = min(self.limit, self.active)
        self.limit = max(1, current - 1 if self.strikes < 3 else current // 2)
        self.interval = min(2.0, max(0.25, self.interval * 2))
        self.cool_until = now + (retry_after or min(30, 2 ** self.strikes))
        self.last_throttle = now

    def accepted(self, now):
        self.strikes = 0
        self.interval *= 0.9
        if (self.limit < self.cap and now - self.last_throttle > RAMP_UP_AFTER
                and now - self.last_raise > RAMP_UP_AFTER / 2):
            self.limit += 1
            self.last_raise = now


class Downloader:
    """States: [connecting ->] probing -> downloading <-> pausing -> paused -> done | error | cancelled.

    `prestart`, if given, runs first on the worker thread (e.g. bringing up VPN tunnels)."""

    def __init__(self, url, folder, filename, connections, routes, prestart=None, resume_part=None):
        self.prestart = prestart
        self.url, self.folder, self.filename = url, folder, filename
        self.orig_url = url                 # as entered; self.url becomes the final URL after redirects
        self.resume_part = resume_part      # a specific .part the user asked to continue
        self.etag = self.modified = None
        self.note = ""                      # e.g. "resumed: 1.2 GB already downloaded"
        self._state_lock = threading.Lock()   # guards the progress record file
        self._state_final = False
        self.connections = connections
        self.routes = routes
        self.path = self.part = None
        self.size = None
        self.ranged = False
        self.segments = []
        self.workers = []
        self.route_bytes = collections.Counter()    # route index -> bytes fetched over it
        self.failovers = []                         # (route label, error) for each failed/throttled request
        self.route_rate = {}                        # route index -> measured bytes/s per connection
        self.moves = 0                              # connections moved to faster routes
        self.gates = []                             # one RouteGate per route
        self._gate_cond = threading.Condition()
        self.state = "connecting" if prestart else "probing"
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
            if not (self.state in ("paused", "error") and self.segments and os.path.exists(self.part)):
                return False
            if self.prestart:                     # e.g. a VPN tunnel may have dropped meanwhile
                self.state = "connecting"
                threading.Thread(target=self._resume_after_prestart, daemon=True).start()
            else:
                self._launch()
            return True

    def set_connections(self, n):
        """Change the number of connections while paused (takes effect on resume).
        The pieces are kept; idle connections split the remaining work between them."""
        with self._lock:
            if self.state not in ("paused", "error") or not self.segments or not self.ranged:
                return
            self.connections = n
            if n != len(self.workers):
                self._plan_workers(n)

    def _resume_after_prestart(self):
        try:
            self.prestart(lambda: self._cancelled)
            error = None
        except Exception as e:
            error = describe(e)
        with self._lock:
            if self._cancelled:
                self._cleanup()
                self.state = "cancelled"
            elif error:
                self.error = error
                self.state = "error"
            else:
                self._launch()

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
            if self.prestart:
                self.prestart(lambda: self._cancelled)
                if self._cancelled:
                    raise IOError("cancelled")
                self.state = "probing"
            for attempt in range(4):              # the probe can be throttled too
                try:
                    info = probe(self.routes[0], self.url)
                    break
                except urllib.error.HTTPError as e:
                    if e.code not in THROTTLE_CODES or attempt == 3 or self._stop.wait(2 ** (attempt + 1)):
                        raise
            self.url, self.size, self.ranged = info.url, info.size, info.ranged
            self.etag, self.modified = info.etag, info.modified

            if self.resume_part:                  # the user picked a partial file to continue
                target = self.resume_part[:-len(".part")]
            else:
                target = os.path.join(self.folder, safe_name(self.filename or info.name))
            segments, why_not = self._load_partial(target)
            if segments is None and self.resume_part:
                raise IOError(f"can't continue {os.path.basename(self.resume_part)}: {why_not}")
            if segments is None:
                if os.path.exists(target + ".part"):
                    self.note = f"existing partial file not reused ({why_not}) - saving as a new file"
                target = unique_path(target)
            done = sum(s.done for s in segments) if segments else 0

            if self.size:
                import shutil
                free = shutil.disk_usage(os.path.dirname(target) or ".").free
                if self.size - done > free:
                    raise IOError(f"not enough disk space: {human_size(self.size - done)} still to "
                                  f"download, {human_size(free)} free")
            self.path, self.part = target, target + ".part"
            self.folder, self.filename = os.path.dirname(target), os.path.basename(target)
            if segments is None:
                self._plan()
                with open(self.part, "wb") as f:
                    if self.size:
                        set_size_sparse(f, self.size)     # so segments can write anywhere
            else:
                self.segments = segments
                self._plan_workers(self.connections)
            self.save_state()
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

    # -- resuming ------------------------------------------------------------- #

    @property
    def state_path(self):
        return self.part + STATE_SUFFIX if self.part else None

    def save_state(self):
        """Write the progress record next to the .part so the download can be
        continued after the app closes. Only for resumable (ranged, sized) downloads."""
        with self._state_lock:
            if (self._state_final or not (self.part and self.segments and self.ranged and self.size)
                    or not os.path.exists(self.part)):
                return
            with self._seg_lock:
                segs = [[s.start, s.end, s.done] for s in self.segments]
            record = {"version": 1, "url": self.orig_url, "final_url": self.url, "size": self.size,
                      "etag": self.etag, "modified": self.modified, "filename": self.filename,
                      "segments": segs, "saved": time.time()}
            tmp = self.state_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(record, f)
                os.replace(tmp, self.state_path)      # atomic: never a half-written record
            except OSError:
                pass

    def _autosave(self):
        while not self._stop.wait(STATE_SAVE_EVERY):
            if self.state != "downloading":       # finished/cancelled: the supervisor has the last word
                return
            self.save_state()

    def _load_partial(self, target):
        """Segments for an existing `target`.part, or (None, reason) if it can't be continued.

        With a progress record (.part.fdl) the done ranges are exact. Without one - the
        app crashed, or the file came from an older version - the ranges Windows has
        allocated in the sparse .part are used instead, minus a safety margin at each
        end (a range's last write may have been cut short, and allocation is rounded
        to whole clusters)."""
        part = target + ".part"
        if not os.path.exists(part):
            return None, "no partial file"
        if not (self.ranged and self.size):
            return None, "the server doesn't support resuming (no byte ranges)"
        if os.path.getsize(part) != self.size:
            return None, (f"its size ({human_size(os.path.getsize(part))}) doesn't match "
                          f"the file on the server ({human_size(self.size)})")
        record = None
        try:
            with open(part + STATE_SUFFIX, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            pass
        if record:
            if record.get("size") != self.size:
                return None, "the progress record is for a different file size"
            for key, now in (("etag", self.etag), ("modified", self.modified)):
                if record.get(key) and now and record[key] != now:
                    return None, "the file on the server has changed since"
            segs = []
            for start, end, done in record["segments"]:
                s = Segment(start, end)
                s.done = max(0, min(done, s.length))
                s.finished = s.done >= s.length
                segs.append(s)
            self.note = f"resumed: {human_size(sum(s.done for s in segs))} already downloaded"
            return segs, None
        runs = allocated_ranges(part)
        if runs is None:
            return None, "it has no progress record and isn't a sparse file, so what's done can't be told"
        segs = segments_from_runs(self.size, runs, REPAIR_MARGIN)
        self.note = (f"recovered a broken download: {human_size(sum(s.done for s in segs))} "
                     "found intact, fetching the rest")
        return segs, None

    def _plan(self):
        if self.ranged and self.size:
            n = max(1, min(self.connections, self.size // MIN_SEGMENT))
            step = self.size // n
            self.segments = [Segment(i * step, self.size - 1 if i == n - 1 else (i + 1) * step - 1)
                             for i in range(n)]
        else:
            n = 1
            self.segments = [Segment(0, None if self.size is None else self.size - 1)]
        self._plan_workers(n)

    def _plan_workers(self, n):
        self.workers = [Worker(i, i % len(self.routes)) for i in range(n)]
        share = -(-n // len(self.routes))                 # ceil(n / routes)
        self.gates = [RouteGate(start=share, cap=n) for _ in self.routes]

    def throttle_note(self):
        """'' or a short note for the UI when servers have been rate-limiting us lately."""
        now = time.monotonic()
        hit = [(r, g) for r, g in zip(self.routes, self.gates) if now - g.last_throttle < 60]
        if not hit:
            return ""
        return "rate-limited, adapting: " + ", ".join(f"{r.label} ≤{g.limit}" for r, g in hit)

    def _acquire(self, w):
        """Wait for a free slot, preferring the worker's own route. Returns route index or None."""
        n = len(self.routes)
        with self._gate_cond:
            while not self._stop.is_set():
                now = time.monotonic()
                home = w.route % n
                order = [home] + sorted((i for i in range(n) if i != home),
                                        key=lambda i: self.gates[i].active / self.gates[i].limit)
                for i in order:
                    g = self.gates[i]
                    if g.ready(now):
                        g.active += 1
                        g.last_start = now
                        w.route = i
                        return i
                if (w.seg is None or w.seg.finished) and not self._work_left():
                    w.status = "idle"             # don't sit out a cooldown for nothing
                    return None
                w.status = "waiting"
                self._gate_cond.wait(0.2)
        return None

    def _work_left(self):
        """Is there anything an idle worker could still pick up?"""
        with self._seg_lock:
            for s in self.segments:
                if not s.finished and (s.owner is None or
                                       (self.ranged and self.size and s.remaining >= STEAL_MIN)):
                    return True
        return False

    def _release(self, i, throttled=False, retry_after=None):
        with self._gate_cond:
            g = self.gates[i]
            if throttled:
                g.throttled(time.monotonic(), retry_after)
            g.active -= 1
            self._gate_cond.notify_all()

    def _accepted(self, i):
        with self._gate_cond:
            self.gates[i].accepted(time.monotonic())

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
        threading.Thread(target=self._autosave, daemon=True).start()
        if len(self.routes) > 1:
            threading.Thread(target=self._rebalance, daemon=True).start()

    def _rebalance(self):
        """Shift connections toward the routes that are fastest per connection.

        Each connection stays on its route until its request ends, and with open-ended
        streams that can be the whole download, so a connection stuck on a slow route
        (e.g. a far-away VPN region) would stay slow. Every REBALANCE_EVERY seconds this
        measures each route's speed per connection and, if the slowest route with a
        connection to spare runs at under REBALANCE_RATIO of the best, moves one of its
        connections to the best route. One move per check avoids see-sawing, and every
        route keeps at least one connection, so its IP stays in use and keeps being measured.
        """
        n = len(self.routes)
        last_bytes = collections.Counter(self.route_bytes)
        last_t = time.monotonic()
        while not self._stop.wait(REBALANCE_EVERY):
            if self.state != "downloading":
                return
            now = time.monotonic()
            dt, cur = now - last_t, collections.Counter(self.route_bytes)
            active = collections.Counter(w.route % n for w in self.workers if w.status == "active")
            for i in range(n):
                if active[i]:
                    rate = (cur[i] - last_bytes[i]) / dt / active[i]
                    old = self.route_rate.get(i)
                    self.route_rate[i] = rate if old is None else 0.5 * old + 0.5 * rate
            last_bytes, last_t = cur, now

            gates = self.gates
            # A route can take another connection if it has a free slot, or if its limit may
            # grow: below the cap and not rate-limited lately. (Limits otherwise only grow on
            # new requests, which long-lived streams rarely make.)
            room = [i for i in range(n) if i in self.route_rate and now >= gates[i].cool_until
                    and (gates[i].active < gates[i].limit or
                         (gates[i].limit < gates[i].cap and now - gates[i].last_throttle > RAMP_UP_AFTER))]
            spare = [i for i in range(n) if active[i] >= 2 and i in self.route_rate]
            if not room or not spare:
                continue
            best = max(room, key=lambda i: self.route_rate[i])
            worst = min(spare, key=lambda i: self.route_rate[i])
            if worst == best or self.route_rate[worst] >= REBALANCE_RATIO * self.route_rate[best]:
                continue
            for w in self.workers:
                if w.status == "active" and w.route % n == worst and not w.move:
                    with self._gate_cond:
                        if gates[best].active >= gates[best].limit:
                            gates[best].limit += 1
                    w.route, w.move = best, True      # _acquire prefers w.route next time
                    self.moves += 1
                    break

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
                self.save_state()
            else:                                 # every connection gave up
                self.error = next((w.error for w in self.workers if w.error),
                                  "some parts could not be downloaded")
                self.state = "error"
                self.save_state()

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

    def _continue_into(self, w, seg):
        """`seg` is done and its stream is still open: claim the next piece of the file if
        nobody is working on it, so the same request keeps going (no new request needed -
        what matters most on servers that limit request rate)."""
        with self._seg_lock:
            for s in self.segments:
                if (s.start == seg.end + 1 and s.owner is None and not s.finished
                        and s.done == 0):
                    s.owner = w
                    return s
        return None

    def _worker(self, w):
        retries = 0
        while not self._stop.is_set():
            route_idx = self._acquire(w)          # 1. a connection slot on some route
            if route_idx is None:
                return
            seg = w.seg if w.seg is not None and not w.seg.finished else None
            if seg is None:                       # 2. some work
                seg = w.seg = self._next_segment(w)
            if seg is None:
                self._release(route_idx)
                w.status = "idle"
                return
            before = w.bytes
            outcome = self._fetch(w, seg, route_idx)   # 3. one request; releases the slot
            if w.bytes > before:
                retries = 0
            if outcome in ("ok", "stopped", "moved"):
                continue
            if outcome == "throttled":
                # Don't sit on the piece while waiting for a slot: a live stream can
                # run straight on into it.
                with self._seg_lock:
                    if w.seg is not None and w.seg.owner is w:
                        w.seg.owner = None
                w.seg = None
                continue
            label = self.routes[route_idx].label
            retries += 1
            if retries > MAX_RETRIES:             # this connection gives up: hand the work back
                w.error = f"{label}: {describe(outcome)}"
                with self._seg_lock:
                    if w.seg is not None and w.seg.owner is w:
                        w.seg.owner = None
                w.seg, w.status = None, "failed"
                return
            w.status = "retrying"
            self.failovers.append((label, describe(outcome)))
            w.route += 1                          # fail over to the next route
            self._stop.wait(min(2 ** retries, 15))    # without holding a slot

    def _fetch(self, w, seg, route_idx):
        """One request for `seg` (and whatever follows it) on an acquired slot.
        Returns 'ok', 'stopped', 'throttled', or the exception that ended it."""
        route = self.routes[route_idx]
        w.status = "active"
        throttled, retry_after = False, None
        w.move = False                            # this new request is already on the chosen route
        try:
            try:
                if self.ranged:
                    # Open-ended range: the stream can continue past this piece (see
                    # _continue_into); it's simply closed when there's nothing more to take.
                    resp = route.open(self.url, seg.start + seg.done, None)
                    if resp.status != 206:
                        resp.close()
                        raise IOError("server ignored the range request")
                else:
                    seg.done = 0                  # no ranges: a retry must restart the stream
                    resp = route.open(self.url)
            except urllib.error.HTTPError as e:
                if e.code not in THROTTLE_CODES:
                    raise
                # Rate-limited: not a failure. The gate lowers this route's limit and
                # pauses it; the worker hands its piece back and waits for a slot.
                throttled = True
                ra = (e.headers.get("Retry-After") or "").strip()
                retry_after = min(int(ra), 120) if ra.isdigit() else None
                self.failovers.append((route.label, describe(e)))
                e.close()
                return "throttled"
            self._accepted(route_idx)
            with resp, open(self.part, "r+b") as f:
                f.seek(seg.start + seg.done)
                while not self._stop.is_set():
                    want = CHUNK if seg.length is None else min(CHUNK, seg.length - seg.done)
                    if want <= 0:                 # this piece is complete (end may have moved by a steal)
                        nxt = self._continue_into(w, seg) if self.ranged else None
                        seg.finished = True
                        if nxt is None:
                            break
                        seg = w.seg = nxt         # contiguous: the stream is already at nxt.start
                        continue
                    if w.move:                    # rebalancer: reconnect on a faster route
                        break
                    data = resp.read(want)
                    if not data:
                        break
                    if seg.length is not None:
                        data = data[:seg.length - seg.done]
                    f.write(data)
                    seg.done += len(data)
                    seg.via = route_idx
                    w.bytes += len(data)
                    self.route_bytes[route_idx] += len(data)
            if self._stop.is_set():
                return "stopped"
            if seg.length is None or seg.done >= seg.length:
                seg.finished = True
                return "ok"
            if w.move:
                return "moved"                    # keeps its piece; reconnects on the new route
            raise IOError("connection closed early")
        except Exception as e:
            return "stopped" if self._stop.is_set() else e
        finally:
            self._release(route_idx, throttled, retry_after)

    def _finalize(self):
        if self.size is None:
            with open(self.part, "r+b") as f:
                f.truncate(self.downloaded)
        os.replace(self.part, self.path)
        self._remove_state()

    def _cleanup(self):
        if self.part:
            try:
                os.remove(self.part)
            except OSError:
                pass
            self._remove_state()

    def _remove_state(self):
        """Done or cancelled: delete the progress record, and never write it again."""
        with self._state_lock:
            self._state_final = True
            try:
                os.remove(self.state_path)
            except (OSError, TypeError):
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
               "# direct | vpn:us_east (use 'VPN regions…') | 10.8.0.2 | http://host:port\n"
               "direct\n")


HISTORY_MAX = 200                # finished downloads remembered


class HistoryDialog(tk.Toplevel):
    """Finished downloads, newest first. Edits `history` (a list) in place.
    Sets self.changed if entries were removed, self.reuse to an entry to download again."""

    def __init__(self, master, history):
        super().__init__(master)
        self.title("Download history")
        self.transient(master)
        self.geometry("720x380")
        self.history = history
        self.changed = False
        self.reuse = None

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        cols = ("finished", "file", "size", "folder")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="extended")
        for c, text, w in zip(cols, ("Finished", "File", "Size", "Folder"), (130, 250, 80, 240)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="e" if c == "size" else "w")
        sb = ttk.Scrollbar(frm, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        self.tree.bind("<Double-Button-1>", lambda e: self.open_folder())

        btns = ttk.Frame(frm)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for text, cmd in (("Open folder", self.open_folder), ("Download again", self.again),
                          ("Copy URL", self.copy_url), ("Remove", self.remove)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Clear all", command=self.clear).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda e: self.destroy())
        self._fill()
        self.grab_set()

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.history):
            path = e.get("path", "")
            name = os.path.basename(path) + ("" if os.path.exists(path) else "  (file missing)")
            self.tree.insert("", "end", iid=str(i), values=(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("finished", 0))),
                name, human_size(e.get("size")), os.path.dirname(path)))

    def _selected(self):
        return [self.history[int(i)] for i in self.tree.selection()]

    def open_folder(self):
        for e in self._selected()[:1]:
            path = e.get("path", "")
            if os.path.exists(path):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif os.path.isdir(os.path.dirname(path)):
                os.startfile(os.path.dirname(path))

    def again(self):
        sel = self._selected()
        if sel:
            self.reuse = sel[0]
            self.destroy()

    def copy_url(self):
        sel = self._selected()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(e["url"] for e in sel))

    def remove(self):
        for i in sorted((int(i) for i in self.tree.selection()), reverse=True):
            del self.history[i]
            self.changed = True
        self._fill()

    def clear(self):
        if self.history and messagebox.askyesno(
                "Download history", "Forget all finished downloads? (The files themselves are kept.)",
                parent=self):
            self.history.clear()
            self.changed = True
            self._fill()


class RegionDialog(tk.Toplevel):
    """Tick PIA OpenVPN regions, at most `max_count`. Sets self.result to the ticked list."""

    def __init__(self, master, regions, selected, max_count):
        super().__init__(master)
        self.title("PIA VPN regions")
        self.transient(master)
        self.geometry("380x520")
        self.max_count = max_count
        self.result = None
        self.vars = {}
        self.rows = {}

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, wraplength=350, justify="left", text=(
            f"Each ticked region becomes its own tunnel and IP. You can pick up to "
            f"{max_count}: one per connection (your Segments setting), at most "
            f"{MAX_VPN_TUNNELS}.")).pack(anchor="w", pady=(0, 6))

        self.filter_var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.filter_var)
        entry.pack(fill="x")
        entry.focus_set()
        self.filter_var.trace_add("write", lambda *a: self._filter())

        # scrollable column of checkboxes
        box = ttk.Frame(frm)
        box.pack(fill="both", expand=True, pady=6)
        canvas = tk.Canvas(box, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas = canvas
        self.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))

        chosen = set(selected)
        # ticked regions first, then the rest alphabetically
        for r in sorted(regions, key=lambda r: (r not in chosen, r)):
            var = tk.BooleanVar(value=r in chosen)
            cb = ttk.Checkbutton(self.inner, text=r, variable=var, command=self._update)
            cb.pack(anchor="w")
            self.vars[r], self.rows[r] = var, cb

        bottom = ttk.Frame(frm)
        bottom.pack(fill="x")
        self.count_var = tk.StringVar()
        self.count_lbl = ttk.Label(bottom, textvariable=self.count_var)
        self.count_lbl.pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", command=self.save).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="Clear", command=self.clear).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self._update()
        self.grab_set()

    def _checked(self):
        return [r for r, v in self.vars.items() if v.get()]

    def _update(self):
        n = len(self._checked())
        full = n >= self.max_count
        # the gatekeeper: at the limit, unticked boxes are disabled until one is unticked
        for r, cb in self.rows.items():
            cb.state(["disabled"] if full and not self.vars[r].get() else ["!disabled"])
        self.count_var.set(f"{n} / {self.max_count} selected" + ("  (limit reached)" if full else ""))
        self.count_lbl.configure(foreground="#b45309" if full else "")

    def _filter(self):
        words = self.filter_var.get().lower().split()
        for r, cb in self.rows.items():
            cb.pack_forget()
        for r, cb in self.rows.items():
            if all(w in r for w in words):
                cb.pack(anchor="w")
        self.canvas.yview_moveto(0)

    def clear(self):
        for v in self.vars.values():
            v.set(False)
        self._update()

    def save(self):
        self.result = self._checked()
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
        for key in ("pia", "env_pia_added"):              # settings of the removed SOCKS route
            self.cfg.pop(key, None)
        self.vpn = VpnManager()
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fast_downloader.ico")
        if os.path.exists(icon):
            try:
                self.iconbitmap(default=icon)
            except tk.TclError:
                pass
        self._build()
        self._announce_unfinished()
        self._drop_socks_routes()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(TICK_MS, self._tick)

    def _build(self):
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="URL").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar(value=self.cfg.get("last_url", ""))
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda e: self.start())
        url_entry.focus_set()

        ttk.Label(frm, text="Save to").grid(row=1, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=self.cfg.get("folder") or default_dir())
        ttk.Entry(frm, textvariable=self.dir_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self.browse).grid(row=1, column=2, sticky="ew", **pad)

        ttk.Label(frm, text="File name").grid(row=2, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar(value=self.cfg.get("last_name", ""))
        ttk.Entry(frm, textvariable=self.name_var).grid(row=2, column=1, sticky="ew", **pad)
        seg_box = ttk.Frame(frm)
        seg_box.grid(row=2, column=2, sticky="e", **pad)
        ttk.Label(seg_box, text="Segments").pack(side="left")
        self.seg_var = tk.StringVar(value=str(self.cfg.get("segments", 8)))
        ttk.Spinbox(seg_box, from_=1, to=MAX_CONNECTIONS, width=4, textvariable=self.seg_var).pack(side="left", padx=(4, 0))

        ttk.Label(frm, text="Routes").grid(row=3, column=0, sticky="nw", **pad)
        self.routes_txt = tk.Text(frm, height=5, width=40, wrap="none", font=("Consolas", 9))
        self.routes_txt.insert("1.0", self.cfg.get("routes") or ROUTES_HINT)
        self.routes_txt.grid(row=3, column=1, sticky="ew", **pad)
        route_btns = ttk.Frame(frm)
        route_btns.grid(row=3, column=2, sticky="n", **pad)
        ttk.Button(route_btns, text="VPN regions…", command=self.vpn_regions).pack(fill="x", pady=(0, 4))
        self.local_btn = ttk.Button(route_btns, text="Add local IPs", command=self.add_local_ips)
        self.local_btn.pack(fill="x", pady=(0, 4))
        self.check_btn = ttk.Button(route_btns, text="Check IPs", command=self.check_ips)
        self.check_btn.pack(fill="x", pady=(0, 4))
        self.vpn_btn = ttk.Button(route_btns, text="Disconnect VPN", command=self.vpn.disconnect_all,
                                  state="disabled")
        self.vpn_btn.pack(fill="x")
        self.vpn_var = tk.StringVar()
        ttk.Label(route_btns, textvariable=self.vpn_var, foreground="#666").pack(anchor="w")
        self.pia_only_var = tk.BooleanVar(value=bool(self.cfg.get("pia_only")))
        ttk.Checkbutton(route_btns, text="PIA only", variable=self.pia_only_var,
                        command=self._pia_only_changed).pack(anchor="w", pady=(4, 0))
        self._pia_only_changed()

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        self.start_btn = ttk.Button(btns, text="Download", command=self.start)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self.toggle_pause, state="disabled")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        self.resume_btn = ttk.Button(btns, text="Resume file…", command=self.resume_file)
        self.history_btn = ttk.Button(btns, text="History…", command=self.show_history)
        for b in (self.start_btn, self.pause_btn, self.cancel_btn, self.resume_btn, self.history_btn):
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
            text = self.routes_txt.get("1.0", "end")
            if self.pia_only_var.get():
                # Keep only vpn:* routes; every connection, the initial probe and any
                # failover then stay on PIA, so your own IP is never used.
                specs = [ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith("vpn:")]
                if not specs:
                    if not messagebox.askyesno(
                            "Fast Downloader",
                            "'PIA only' is ticked, but no VPN regions are in the route list yet.\n\n"
                            "Pick one or more PIA regions now?"):
                        return None
                    self.vpn_regions()
                    text = self.routes_txt.get("1.0", "end")
                    specs = [ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith("vpn:")]
                    if not specs:
                        return None
                text = "\n".join(specs)
            routes = parse_routes(text, self.vpn)
            regions = {r.tunnel.region for r in routes if r.tunnel}
            limit = self._max_regions()
            if len(regions) > limit:
                raise ValueError(f"{len(regions)} VPN regions, but at most {limit} can be used "
                                 f"(one per connection in Segments, max {MAX_VPN_TUNNELS}) - "
                                 "untick some in 'VPN regions…'")
            return routes
        except ValueError as e:
            messagebox.showerror("Fast Downloader", f"Bad route: {e}")
            return None

    def _connections(self, announce=False):
        """The Segments setting as a connection count, limited to 1..MAX_CONNECTIONS.
        With `announce`, an out-of-range value is corrected in the box and explained."""
        raw = self.seg_var.get().strip()
        try:
            wanted = int(raw)
        except ValueError:
            wanted = 8
        n = max(1, min(MAX_CONNECTIONS, wanted))
        if announce and str(n) != raw:
            self.seg_var.set(str(n))
            self.status_var.set(f"Segments set to {n} (allowed: 1–{MAX_CONNECTIONS}).")
        return n

    def _max_regions(self):
        """How many VPN regions may be ticked: one per connection, capped at MAX_VPN_TUNNELS."""
        return min(MAX_VPN_TUNNELS, self._connections())

    def _drop_socks_routes(self):
        """Remove 'pia' lines left over from the removed PIA SOCKS route."""
        lines = self.routes_txt.get("1.0", "end-1c").splitlines()
        kept = [ln for ln in lines if ln.strip().lower() != "pia"]
        if len(kept) != len(lines):
            self.routes_txt.delete("1.0", "end")
            self.routes_txt.insert("1.0", "\n".join(kept) + "\n")
            self.status_var.set("The PIA SOCKS route was removed - use 'VPN regions…' to add PIA regions.")

    def _prestart_for(self, routes):
        """If any route is a VPN tunnel, a callable that connects them (else None)."""
        tunnels = [r.tunnel for r in routes if r.tunnel]
        if not tunnels:
            return None
        return lambda cancelled: self.vpn.ensure(tunnels, cancelled)

    def _pia_only_changed(self):
        self.local_btn.config(state="disabled" if self.pia_only_var.get() else "normal")

    def vpn_regions(self):
        try:
            regions = list(self.vpn.regions())
        except OSError as e:
            messagebox.showerror("Fast Downloader", f"Could not download PIA's region list:\n{describe(e)}")
            return
        lines = self.routes_txt.get("1.0", "end-1c").splitlines()
        current = [ln.strip()[4:].strip().lower() for ln in lines if ln.strip().lower().startswith("vpn:")]
        dlg = RegionDialog(self, regions, current, self._max_regions())
        self.wait_window(dlg)
        if dlg.result is None:                        # cancelled
            return
        # The ticked set replaces the vpn: lines; other routes stay where they are.
        kept = [ln for ln in lines if not ln.strip().lower().startswith("vpn:")]
        while kept and not kept[-1].strip():
            kept.pop()
        self.routes_txt.delete("1.0", "end")
        self.routes_txt.insert("1.0", "\n".join(kept + [f"vpn:{r}" for r in dlg.result]) + "\n")

    def _append_routes(self, specs):
        existing = {ln.strip().lower() for ln in self.routes_txt.get("1.0", "end").splitlines()}
        new = [s for s in specs if s.lower() not in existing]
        if new:
            prev = self.routes_txt.cget("state")
            self.routes_txt.config(state="normal")    # insert is ignored while disabled
            if self.routes_txt.get("end-2c", "end-1c") != "\n":
                self.routes_txt.insert("end-1c", "\n")
            self.routes_txt.insert("end-1c", "\n".join(new) + "\n")
            self.routes_txt.config(state=prev)

    def add_local_ips(self):
        ips = local_ipv4s()
        if not ips:
            messagebox.showinfo("Fast Downloader", "No local IPv4 addresses found.")
            return
        self._append_routes(ips)

    def _save_settings(self):
        self.cfg.update(
            routes=self.routes_txt.get("1.0", "end-1c"),
            folder=self.dir_var.get().strip(),
            segments=self.seg_var.get().strip(),
            pia_only=self.pia_only_var.get(),
            last_url=self.url_var.get().strip(),
            last_name=self.name_var.get().strip(),
        )
        save_config(self.cfg)

    def check_ips(self):
        routes = self._routes()
        if not routes:
            return
        self.check_btn.config(state="disabled", text="Checking…")

        prestart = self._prestart_for(routes)

        def work():
            lines = []
            if prestart:
                try:
                    prestart(lambda: False)
                except Exception as e:
                    lines.append(f"VPN: {describe(e)}")
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

    def start(self, resume_part=None):
        if self.dl and self.dl.state in ("connecting", "probing", "downloading", "pausing", "paused"):
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Fast Downloader", "Enter a URL first.")
            return
        if not re.match(r"https?://", url, re.I):
            url = "https://" + url
            self.url_var.set(url)
        if not resume_part:
            done = self._finished_entry(url)
            if done and not messagebox.askyesno(
                    "Fast Downloader",
                    f"This URL was already downloaded on "
                    f"{time.strftime('%b %d, %Y %H:%M', time.localtime(done['finished']))} to\n"
                    f"{done['path']}\n\nDownload it again?"):
                return
        self._save_settings()                         # remember the URL/name even if the app dies
        folder = self.dir_var.get().strip() or default_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Fast Downloader", f"Cannot use folder:\n{e}")
            return
        n = self._connections(announce=True)
        routes = self._routes()
        if not routes:
            return
        self._reset_speed()
        self.dl = Downloader(url, folder, self.name_var.get().strip() or None, n, routes,
                             prestart=self._prestart_for(routes), resume_part=resume_part)
        self.dl.start()

    # -- history ------------------------------------------------------------ #

    def _history(self):
        return self.cfg.setdefault("history", [])

    def _finished_entry(self, url):
        """The most recent history entry for `url` whose file still exists, or None."""
        for e in self._history():
            if e.get("url") == url and os.path.exists(e.get("path", "")):
                return e
        return None

    def _record_finished(self, dl):
        """A download completed: add it to the history and clear the URL/name fields."""
        history = self._history()
        history.insert(0, {"url": dl.orig_url, "path": dl.path, "size": dl.size,
                           "finished": time.time()})
        del history[HISTORY_MAX:]
        if self.url_var.get().strip() == dl.orig_url:     # unless the user already typed a new one
            self.url_var.set("")
            self.name_var.set("")
        self._save_settings()

    def show_history(self):
        dlg = HistoryDialog(self, self._history())
        self.wait_window(dlg)
        if dlg.changed:
            self._save_settings()
        if dlg.reuse:
            self.url_var.set(dlg.reuse["url"])
            self.name_var.set(os.path.basename(dlg.reuse["path"]))

    def resume_file(self):
        """Continue a partial download (.part), with or without its progress record."""
        path = filedialog.askopenfilename(
            title="Pick a partial download to continue",
            initialdir=self.dir_var.get() or default_dir(),
            filetypes=[("Partial downloads", "*.part"), ("All files", "*.*")])
        if not path:
            return
        path = os.path.normpath(path)
        if not path.endswith(".part"):
            messagebox.showwarning("Fast Downloader", "Pick a .part file (an unfinished download).")
            return
        url = None
        try:
            with open(path + STATE_SUFFIX, encoding="utf-8") as f:
                url = json.load(f).get("url")
        except (OSError, ValueError):
            pass
        if url:
            self.url_var.set(url)
        elif not self.url_var.get().strip():
            messagebox.showinfo(
                "Fast Downloader",
                "This partial file has no progress record, so its URL isn't known.\n\n"
                "Paste the URL it was downloaded from into the URL box, then pick the file again. "
                "The parts already downloaded are found from the file itself.")
            return
        self.dir_var.set(os.path.dirname(path))
        self.name_var.set("")
        self.start(resume_part=path)

    def _announce_unfinished(self):
        """On launch, point out unfinished downloads in the save folder."""
        folder = self.dir_var.get().strip() or default_dir()
        try:
            parts = [n for n in os.listdir(folder) if n.endswith(".part")]
        except OSError:
            return
        if parts:
            names = ", ".join(n[:-5] for n in parts[:2]) + (" …" if len(parts) > 2 else "")
            self.status_var.set(f"{len(parts)} unfinished download(s) here ({names}). "
                                "Click Download with the same URL, or 'Resume file…', to continue.")

    def toggle_pause(self):
        dl = self.dl
        if not dl:
            return
        if dl.state == "downloading":
            dl.pause()
        elif dl.state in ("paused", "error"):
            self._reset_speed()
            dl.set_connections(self._connections(announce=True))   # Segments may have changed
            if not dl.resume():
                self.start()                      # failed before any data: start over

    def _reset_speed(self):
        self.speed.clear()
        self.wspeed.clear()

    def cancel(self):
        dl = self.dl
        if not dl:
            return
        if dl.downloaded > 0 and not messagebox.askyesno(
                "Fast Downloader",
                f"Cancel and delete the partial file ({human_size(dl.downloaded)} downloaded)?\n\n"
                "To stop for now and continue later, use Pause instead."):
            return
        dl.cancel()

    def _on_close(self):
        if self.dl and self.dl.state in ("connecting", "probing", "downloading", "pausing"):
            if not messagebox.askyesno(
                    "Fast Downloader",
                    "A download is in progress. Quit anyway?\n\nProgress is saved: click Download "
                    "with the same URL (or use 'Resume file…') to continue later."):
                return
            self.dl.stop()
        if self.dl:
            self.dl.save_state()
        self.vpn.disconnect_all()     # the elevated watchdog also stops them once we exit
        self._save_settings()
        self.destroy()

    # -- periodic UI refresh ------------------------------------------------ #

    def _tick(self):
        self._refresh_vpn()
        dl = self.dl
        if dl:
            if dl.state != self._last_state:
                self._last_state = dl.state
                self._on_state(dl)
            self._refresh(dl)
        self.after(TICK_MS, self._tick)

    def _refresh_vpn(self):
        tunnels = [t for t in self.vpn.tunnels.values() if t.running]
        up = sum(t.state == "connected" for t in tunnels)
        if not tunnels:
            text = ""
        elif up == len(tunnels):
            text = f"VPN: {up} connected"
        else:
            text = f"VPN: {up}/{len(tunnels)} connected…"
        self.vpn_var.set(text)
        self.vpn_btn.config(state="normal" if tunnels else "disabled")

    def _on_state(self, dl):
        s = dl.state
        busy = s in ("connecting", "probing", "downloading", "pausing", "paused")
        self.start_btn.config(state="disabled" if busy else "normal")
        self.resume_btn.config(state="disabled" if busy else "normal")
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
            self._record_finished(dl)

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
        if s == "connecting":
            text = "Connecting VPN… (approve the administrator prompt if Windows asks)"
        elif s == "probing":
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
            if all(r.is_vpn for r in dl.routes):
                mode = "PIA only  ·  " + mode
            note = dl.throttle_note()
            if note:
                mode = note + "  ·  " + mode
            if dl.moves:
                mode = f"{dl.moves} connection(s) moved to faster routes  ·  " + mode
            if dl.note:
                mode = dl.note + "  ·  " + mode
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
                state = {"retrying": "retry ", "failed": "FAILED", "waiting": " wait "}.get(wk.status, " idle ")
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

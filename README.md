# Fast Downloader

A segmented, multi-connection downloader with a desktop UI. Files are split into
pieces that download in parallel, optionally over different routes, so the pieces
come from different IP addresses. A route can be your normal connection, PIA
OpenVPN tunnels in any region (several at once), a local adapter or an HTTP
proxy. Idle connections take over the remaining work of
slow ones, so a slow route never holds up the finish. Each server's rate limits
are learned automatically.

## Running

- **Icon:** double-click **Fast Downloader** on the Desktop or in the Start Menu.
- **Terminal:** `python fast_downloader.py` (useful for seeing errors if it won't start).

If you move this folder or reinstall Python, recreate the shortcuts:

```powershell
powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
```

Requirements:

- Windows 10/11 and Python 3.9+ with Tkinter (included in standard Windows installs).
  Only the standard library is used.
- `vpn:` routes also need [OpenVPN Community](https://openvpn.net/community/) 2.7+ and
  administrator rights (one UAC prompt per connect).

## Routes

Enter one route per line in the **Routes** box. Connections are spread across
them in turn; list a route twice to give it more connections.

| Route | Meaning |
|---|---|
| `direct` | Your normal connection |
| `vpn:us_east` | A PIA OpenVPN tunnel to that region, one IP per region (see [PIA over OpenVPN](#pia-over-openvpn)) |
| `10.8.0.2` | Bind to this local IP, e.g. another network adapter (**Add local IPs** lists them) |
| `http://host:port` | HTTP proxy (`user:pass@` allowed) |

**Check IPs** shows the public IP each route actually gets.

**PIA only:** tick this to use only the `vpn:` routes in the list. It needs at least one.
Your own IP is never used for downloading, even if a tunnel fails; the download
stops with an error instead. Site names are still looked up by your own DNS.

## Pausing, resuming and fixing broken downloads

- **Pause / Resume** stops and continues the current download without losing anything.
- **Close the app mid-download** and the progress is kept. Click **Download** again with
  the same URL, or use **Resume file…**, and it continues where it stopped. On launch,
  the status line lists any unfinished downloads in the save folder.
- **Broken downloads** (the app crashed, the PC restarted, or the file came from an older
  version) are recovered too. Use the same URL, or **Resume file…** with the URL pasted in
  the URL box. The app works out which parts of the `.part` file were already
  downloaded and fetches only the rest.
- Before continuing, the app checks the file on the server is still the same (size and
  `ETag`/`Last-Modified`). If it changed, the old partial file is left alone and the
  download starts fresh under a new name, so stale and new data are never mixed.
- **Cancel** deletes the partial file (it asks first). To stop for now, use **Pause** or
  close the app.

Resuming needs a server that supports byte ranges (almost all do). A download from a
server without them can't be continued and restarts from the beginning.

## Rate limiting

Many servers cap how hard **one IP address** can hit them, and answer
`429 Too Many Requests` beyond that, usually without saying what the limit is. They
differ a lot. In testing, Hetzner's speed-test server allowed about 9 connections per
IP, while OVH's allowed about 3 at once and then only about one *new request* every
10–20 seconds.

The app adapts automatically, per route (each route is its own IP):

- **It learns each route's connection limit.** A 429 lowers that route's limit to what
  the server just accepted, pauses the route briefly (2, 4, 8… s, or the server's
  `Retry-After`) and spaces out its new requests. Repeated 429s halve the limit. After
  20 s without one, the limit goes back up by one.
- **A 429 isn't a failure.** The connection waits for a free slot, on any route, instead
  of using up its retries.
- **It makes as few requests as possible.** Each connection asks for "from here to the end
  of the file". When it finishes its own piece and the next piece is unclaimed, it keeps
  going over the same connection instead of opening a new one. A throttled connection
  hands its piece back so a running stream can carry on through it. On OVH this cut a
  100 MB download from 267 s to 6 s.

**Connections follow the speed.** Every 3 s the app measures each route's speed *per
connection*. If a route with more than one connection runs at under 60% of the fastest,
one of its connections moves to the fastest route. It keeps its piece and carries on
from the same byte. Every route keeps at least one connection, so all your IPs stay in
use and slow routes are still measured in case they recover. This only helps when
**Segments** is larger than the number of routes; with 8 regions and 8 segments each
route has just one connection to begin with. The bottom line shows `N connection(s)
moved to faster routes`.

The bottom status line shows `rate-limited, adapting: direct ≤3 …` while a server is
pushing back. You don't need to lower **Segments** by hand; it only sets the starting
point.

## PIA over OpenVPN

`vpn:<region>` routes connect real PIA VPN tunnels, in any of PIA's 166 regions,
several at once. Each tunnel carries only the download connections assigned to
it. Your normal internet traffic (and the `direct` route) stays on your own
connection.

### Setup

1. Install [OpenVPN Community](https://openvpn.net/community/) 2.7+
   (`winget install OpenVPNTechnologies.OpenVPN`).
2. Copy `.env.example` to `.env` and add your **normal** PIA login (the `p…` one you use
   for the PIA app):
   ```
   PIA_VPN_USER=p1234567
   PIA_VPN_PASS="your-normal-pia-password"
   ```
3. Click **VPN regions…**, type to filter (for example `us`), tick the regions you want,
   and click **Save**. Ticked regions become `vpn:<region>` lines, replacing any previous
   ones. You can tick **up to your Segments setting, at most 8**: each region needs at
   least one connection, and all tunnels start from one elevated command. At the
   limit, the remaining boxes are disabled until you untick one. PIA's config files are
   downloaded to `pia_openvpn/` the first time.
4. **Disconnect the PIA app** and turn off its killswitch. It conflicts with separate tunnels.

### Using it

- Tunnels connect when a download (or **Check IPs**) needs them. Windows shows **one
  administrator (UAC) prompt** per connect, because only administrators can set up
  network adapters and routes. The status line says *Connecting VPN…* meanwhile.
- Tunnels stay up between downloads (no further prompts) until you click **Disconnect VPN**
  or quit the app. If the app crashes, a watchdog takes them down within a few seconds.
- The first time you use N tunnels at once, N virtual adapters named `FastDL VPN 1…N`
  are created. They're reused afterwards and can be removed in Device Manager if you
  stop using this.

### How it works

Each region runs its own `openvpn.exe`. PIA's "send all traffic through the VPN"
instruction, its IPv6 routes and its DNS settings are ignored. Instead, the tunnel
gets a default route with a very high metric (9000), which normal traffic never
prefers. Download connections assigned to a tunnel are bound to the tunnel's IP,
and Windows then routes them through that tunnel only. The app controls each
OpenVPN process over its password-protected local management port: it supplies
your login, reads the tunnel IP and state, and disconnects it.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `the administrator (UAC) prompt was declined` | Approve the prompt; tunnels can't be created without it. |
| `PIA rejected the login` | `PIA_VPN_USER` / `PIA_VPN_PASS` must be your normal `p…` PIA login. |
| `OpenVPN is not installed` | Install OpenVPN Community (setup step 1). |
| `timed out connecting` | The region may be down or blocked on your network; try another region. Check the PIA app is disconnected. |
| Downloads hit `HTTP 429 Too Many Requests` | The server is rate-limiting your IP. The app adapts automatically (see [Rate limiting](#rate-limiting)). |

## Technical details

Everything is in one file, [`fast_downloader.py`](fast_downloader.py), in four layers:
helpers, routes, the download engine, and the Tkinter UI. The engine and routes have
no UI dependencies and can be scripted directly:

```python
import fast_downloader as fd
routes = fd.parse_routes("direct\n10.8.0.2")
dl = fd.Downloader(url, folder, None, 8, routes)
dl.start()          # runs on background threads; poll dl.state / dl.downloaded
```

### Download engine

| Piece | Role |
|---|---|
| `probe()` | One `Range: bytes=0-0` request finds the size (`Content-Range`), whether ranges work (`206`), the final URL after redirects and a filename (`Content-Disposition`, else the URL path). A `416` falls back to a plain GET. |
| `Segment` | A byte range `[start, end]` plus bytes done. `end` can shrink when another worker takes over its tail. |
| `Worker` | One thread = one connection. It loops: **get a slot → get a piece → make one request.** |
| `RouteGate` | Per-route adaptive limit: connection cap, cooldown and request spacing (see [Rate limiting](#rate-limiting)). |
| `Downloader` | Owns the above; states: `connecting → probing → downloading ⇄ pausing → paused → done / error / cancelled`. |

- **Storage:** the output is `<name>.part`, created as a **sparse file** at its full size
  (`set_size_sparse()`: `FSCTL_SET_SPARSE`, then one byte written at the end). On Windows,
  `truncate()` extends a file by writing zeros, so a 250 GB download would first write
  250 GB. Sparse, it's instant, and only the parts actually downloaded use disk space.
  Every worker opens the file itself and `seek`s to its own offset, so no merging is
  needed. The file is renamed with `os.replace` when every piece is done. Existing names
  get ` (1)`, ` (2)`… appended. Before starting, the app checks there's enough free disk
  space for the whole file.
- **Requests:** requests are open-ended (`Range: bytes=<pos>-`). When a worker reaches the end
  of its piece, `_continue_into()` claims the next piece if it starts exactly there, is
  unclaimed and untouched. The same stream then carries on with no new request.
- **Work stealing:** `_next_segment()`, in order:
  1. an unclaimed piece (orphaned by a throttled or failed worker);
  2. otherwise it splits the piece with the most left in half, if at least `STEAL_MIN` (1 MiB)
     remains.

  The split point is at least 512 KiB ahead of the owner's position (far more than one 64 KiB
  read), and each read is clamped to the piece's current `end`, so a shrinking piece can
  never be over-written.
- **Errors:** a `429`/`503` goes to the route's gate and isn't counted as a failure. Other
  errors retry with backoff (2, 4, 8… s, max 15 s) and switch the worker to the next route.
  After `MAX_RETRIES` (5) in a row without progress, the worker hands its piece back and
  stops. The download only fails if every worker has stopped this way.
- **Progress record:** `<name>.part.fdl` (JSON) holds the original and final URL, size,
  `ETag`, `Last-Modified` and every piece as `[start, end, done]`. It's written atomically
  (a temp file, then `os.replace`) every `STATE_SAVE_EVERY` (5) seconds while downloading,
  on pause or error, and when the app closes. It's deleted when the download completes or
  is cancelled. A lock plus a "final" flag stop a late autosave from re-creating it after
  completion.
- **Resuming:** after the probe, `_load_partial()` looks for `<target>.part`. The size must
  match, and any `ETag`/`Last-Modified` in the record must equal the server's. Pieces
  are rebuilt from the record and each continues at `start + done`. If nothing matches,
  `unique_path()` picks a name whose file *and* `.part` are both free.
- **Repair without a record:** the `.part` is sparse, so `FSCTL_QUERY_ALLOCATED_RANGES`
  (`allocated_ranges()`) returns exactly the ranges that were ever written.
  `segments_from_runs()` turns those into finished pieces and the gaps into pieces still
  to fetch. It trims `REPAIR_MARGIN` (1 MiB) from both ends of each range, because a
  range's last write may have been cut short and its start is rounded down to a cluster
  boundary. A non-sparse file can't be told apart this way, so it isn't reused.
- **Pause/resume** within a session keeps pieces, learned route limits and tunnels.
- **Servers without range support** get a single plain GET, restarted from zero on retry.

### Routes

Each `Route` wraps a `urllib` opener:

| Route | Mechanism |
|---|---|
| `direct` | the default opener (honours system proxy settings) |
| Local IP | custom `HTTPConnection`/`HTTPSConnection` handlers with `source_address=(ip, 0)` |
| HTTP proxy | `ProxyHandler` |
| `vpn:` | a bound opener built for the tunnel's current IP (rebuilt if the IP changes on reconnect) |

### PIA over OpenVPN internals

- **Configs:** `pia_regions()` downloads `privateinternetaccess.com/openvpn/openvpn.zip` into
  `pia_openvpn/` once (166 `.ovpn` files).
- **Launching:** `VpnManager._launch()` builds one PowerShell script, starts it elevated with
  `ShellExecuteW(…, "runas", …)` (the single UAC prompt) and passes it as `-EncodedCommand`.
  The script:
  1. creates any missing `FastDL VPN n` tap-windows6 adapters (`tapctl create --hwid root\tap0901`);
  2. clears stale `0.0.0.0/0` routes on them;
  3. starts one hidden `openvpn.exe` per region;
  4. then acts as a **watchdog**: `Wait-Process` on the app's PID, then kills its
     `openvpn.exe` processes and clears their routes.

  PIA's configs use `AES-128-CBC` as a fallback cipher, and that rules out DCO, OpenVPN 2.7's
  driver that creates adapters on demand. So each tunnel gets its own tap adapter.
- **OpenVPN options added:**
  - `--pull-filter ignore` for `redirect-gateway`, `route-ipv6`, `ifconfig-ipv6`,
    `dhcp-option` and `block-outside-dns`, so the server can't take over routing or DNS;
  - `--route 0.0.0.0 0.0.0.0 vpn_gateway 9000`;
  - `--sndbuf 524288 --rcvbuf 524288`, with pushed `sndbuf`/`rcvbuf` ignored. OpenVPN's small
    default buffers on Windows collapse throughput. Measured to PIA US West from
    archive.org: **0.1 MB/s per connection by default, 3.9 MB/s with 512 KB**
    (1.2 vs 6.5 MB/s with 4 connections);
  - `--data-ciphers AES-128-GCM:AES-256-GCM:AES-128-CBC` (PIA negotiates GCM);
  - `--allow-compression asym`;
  - `--management-hold --management-query-passwords --auth-nocache --auth-retry none`.
- **One session per server.** PIA gives an account the *same* tunnel IP on a given server,
  so two sessions to one server collide: the later one breaks the earlier one. Each region
  is a different server, so one tunnel per region is safe.
- **DCO doesn't work with PIA.** A DCO tunnel (AEAD only, compression removed) connects but
  passes no traffic, because PIA's servers use compression framing ("stub"), which DCO
  doesn't support. Tunnels stay on tap-windows6.
- **Routing:** Windows uses the strong host model, so a socket bound to the tunnel IP only
  uses routes on that interface. That makes the tunnel's metric-9000 default route
  apply only to bound sockets.
- **Management protocol** (`VpnTunnel._session`), over `127.0.0.1:<random port>`:
  - The management password is a random 32-hex token, written in binary without `\r`.
    OpenVPN keeps a trailing `\r` as part of the password.
  - It's deleted once accepted.
  - OpenVPN **drops commands sent before it has answered the previous one**, so commands
    are queued and sent one at a time, each after the `SUCCESS:`/`ERROR:` reply.
  - The PIA login is sent in answer to `>PASSWORD:Need 'Auth'`; `\` and `"` are escaped.
  - `>STATE:…,CONNECTED,SUCCESS,<ip>` gives the tunnel IP.
  - `>LOG:` lines are kept (last 60) for error messages.
  - `signal SIGTERM` disconnects the tunnel.
- **Log file:** OpenVPN's log file is locked to administrators, so the log is only read
  through the management port.

### Files and settings

| Path | Contents |
|---|---|
| `.env` (git-ignored) | `PIA_VPN_USER/PASS` (your `p…` PIA login). Parsed by `load_dotenv()`: `KEY=VALUE`, quotes, `export`, `#` comments. Real environment variables win. |
| `%USERPROFILE%\.fast_downloader.json` | routes, folder, segment count, PIA-only (no credentials) |
| `<name>.part` + `<name>.part.fdl` | an unfinished download (sparse file) and its progress record |
| `pia_openvpn/` (git-ignored) | cached PIA OpenVPN configs |
| `%TEMP%\fastdl_vpn_*` | per-connect management password files (deleted once used) |
| `fast_downloader.ico`, `create_shortcut.ps1` | icon; creates Desktop and Start Menu shortcuts running `pythonw.exe` (no console) |

Tunable constants are at the top of the file: `CHUNK`, `MIN_SEGMENT`, `STEAL_MIN`,
`MAX_RETRIES`, `TIMEOUT`, `RAMP_UP_AFTER`, `VPN_CONNECT_TIMEOUT` and `VPN_ARGS`.

### Findings

Measured or discovered while building this (Windows 11, 1 Gbps line, September 2026).
They explain several design choices above.

**Throughput**

| Test | Result |
|---|---|
| archive.org, direct, 1 / 4 / 8 connections | 7.2 / 16.7 / ~22 MB/s: per-connection speed is the limit, not the line (1 Gbps ≈ 125 MB/s) |
| PIA US West tunnel, OpenVPN default buffers, 1 / 4 connections | **0.1 / 1.2 MB/s** |
| Same, `--sndbuf/--rcvbuf 524288`, 1 / 4 connections | **3.9 / 6.5 MB/s**, repeatable on a second server |
| 8 regions (Europe, Canada, US East/South) × 1 connection each, default buffers | ~8.5 MB/s total, 0.27–1.5 MB/s per connection, to a California server |

Takeaways:
- **Buffers:** always raise OpenVPN's socket buffers on Windows.
- **Region choice:** pick regions near the file's server; every connection goes you → PIA → server.
- **Connections:** give each route several connections (Segments > number of routes), so rebalancing can shift them to the fast ones.

**Server rate limits** (probed with tiny parallel range requests)

| Server | Behaviour |
|---|---|
| Hetzner speed test (`fsn1-speed.hetzner.com`) | ~9 connections per IP; the rest get `429` |
| OVH (`proof.ovh.net`) | ~3 connections per IP, then about one *new request* per 10–20 s, for a while |

Neither sends `Retry-After` or rate-limit headers. That's why limits are learned per
route, and why requests are open-ended and streams carry on into the next piece. On OVH,
100 MB with 8 connections took 267 s with one request per piece and 6 s with streams
carrying on.

**PIA**

- **OpenVPN:**
  - **Regions:** OpenVPN works in all 166 regions, with your normal `p…` login.
  - **Cipher:** servers negotiate `AES-128-GCM`, but still require compression framing ("stub"). That rules out OpenVPN 2.7's DCO driver: a DCO tunnel connects but passes no traffic.
  - **Sessions:** one account gets the **same tunnel IP** on a given server, so parallel sessions to one server break each other. Use one tunnel per region, never two to the same server.
  - **Pushed settings:** servers push `redirect-gateway def1`, `route-ipv6 2000::/3` and a DNS server. All are filtered out so that only bound connections use the tunnel.
- **SOCKS5 proxy (removed):** Netherlands only (`proxy-nl…`, about 30 servers).
  - It needs separate `x…` credentials, which can be revoked.
  - It fails remote DNS (`socks5h`) with *0x04 Host unreachable*.
  - It was replaced by OpenVPN.

**OpenVPN on Windows**

- **Drivers:** 2.7 creates adapters on demand only for DCO. With tap-windows6 (needed here), each simultaneous tunnel needs its own adapter (`tapctl create --hwid root\tap0901`).
- **New adapters:** a freshly created tap adapter can make the first connect report `CONNECTED,ERROR`. A short pause after creating it avoids this.
- **Leftover routes:** a tunnel killed rather than stopped cleanly leaves its `0.0.0.0/0` route behind on the tap adapter. It's harmless because the adapter is disconnected, but it's cleared on the next launch and by the watchdog.
- **Management interface:**
  - It drops commands that arrive while it's still answering the previous one, so send one at a time and wait for `SUCCESS:`/`ERROR:`.
  - The password file must end in `\n`, not `\r\n`; the `\r` becomes part of the password.
- **Log file:** an elevated `openvpn.exe` creates its `--log` file readable only by administrators, so logs are read through the management interface (`log on`).

**Windows file I/O**

- **Preallocating:** `truncate()` extends a file by physically writing zeros. A 250 GB download wrote 35 GB of zeros before this was noticed. Marking the file sparse first (`FSCTL_SET_SPARSE`) makes setting its size instant.
- **Recovery:** a sparse file's allocated ranges (`FSCTL_QUERY_ALLOCATED_RANGES`) show exactly what was written. A 232 GB partial download that had lost its progress record came back as 8 runs, one per segment start, and 931 MB of it was recoverable.

### Known limitations

- Only one download at a time.
- Repairing a download that lost its progress record needs Windows and a sparse `.part`
  (every `.part` this version creates is sparse).
- Site names are always resolved by your own DNS, including for `vpn:` routes.
- `vpn:` routes are Windows-only and need administrator rights. They don't coexist with
  the PIA app while it's connected.

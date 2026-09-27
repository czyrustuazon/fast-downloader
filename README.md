# Fast Downloader

A segmented, multi-connection downloader with a desktop UI. Files are split into
pieces that download in parallel, optionally over different routes, so the pieces
come from different IP addresses. A route can be your normal connection, PIA
OpenVPN tunnels in any region (several at once), PIA's SOCKS5 proxy, a local
adapter or any HTTP/SOCKS proxy. Idle connections take over the remaining work of
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
  Otherwise only the standard library is used.
- SOCKS routes, including `pia`, also need `pip install PySocks`.
- `vpn:` routes also need [OpenVPN Community](https://openvpn.net/community/) 2.7+ and
  administrator rights (one UAC prompt per connect).

## Routes

Enter one route per line in the **Routes** box. Connections are spread across
them in turn; list a route twice to give it more connections.

| Route | Meaning |
|---|---|
| `direct` | Your normal connection |
| `pia` | PIA's SOCKS5 proxy, Netherlands only (see below) |
| `vpn:us_east` | A PIA OpenVPN tunnel to that region, one IP per region (see [PIA over OpenVPN](#pia-over-openvpn)) |
| `10.8.0.2` | Bind to this local IP, e.g. a VPN adapter (**Add local IPs** lists them) |
| `http://host:port` | HTTP proxy (`user:pass@` allowed) |
| `socks5://user:pass@host:port` | SOCKS5 proxy (`socks5h://` resolves DNS through the proxy) |

**Check IPs** shows the public IP each route actually gets.

**PIA only:** tick this to use only the PIA routes in the list (`pia` and any
`vpn:` regions), or `pia` if the list has none. Your own IP is never used for
downloading, even if PIA fails; the download stops with an error instead. Site
names are still looked up by your own DNS, and **PIA… → Test** still checks your
own IP for comparison.

## PIA setup

The `pia` route uses PIA's SOCKS5 proxy. You don't need the PIA app for this, and
it should **not** be connected in full-tunnel mode. If it is, your `direct`
route goes through the VPN too, and both routes get the same IP.

### 1. Get your SOCKS credentials

The proxy needs **separate credentials from your normal PIA login**:

| | Username looks like | Works for the proxy? |
|---|---|---|
| Normal PIA account login | `p1234567` | ❌ No: fails with *SOCKS5 authentication failed* |
| SOCKS credentials | `x1234567` | ✅ Yes |

To generate them:

1. Sign in to the [PIA Client Control Panel](https://www.privateinternetaccess.com/account/client-control-panel).
2. Go to **Downloads → VPN Settings → SOCKS**.
3. Click **Generate** (or **Regenerate**) and copy the username and password it shows.

Regenerating invalidates the previous SOCKS password.

### 2. Give them to the app

Use **either** method:

- **`.env` file (recommended):** copy `.env.example` to `.env` in this folder and fill in:
  ```
  PIA_SOCKS_USER=x1234567
  PIA_SOCKS_PASS="the-generated-password"
  ```
  Quote the password if it contains `#`, spaces or quotes. `.env` is git-ignored.
  On the first launch that finds these credentials, `direct` and `pia` are added to your routes.
- **PIA… button:** enter them in the dialog. Tick *Remember password* to keep them between
  sessions (stored unencrypted in `%USERPROFILE%\.fast_downloader.json`).
  Credentials saved in the dialog take priority over `.env`.

### 3. Check it works

Open **PIA… → Test**, or click **Check IPs**. You should see two different public
IPs: your ISP's for `direct`, and a Netherlands one for `pia`.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SOCKS5 authentication failed` | You used your normal `p…` login, or the SOCKS password was regenerated. Generate SOCKS credentials (step 1). Freshly generated credentials can take a while to reach every PIA server. The app moves on to the next server automatically, so this only shows if all of them reject you. |
| `0x04: Host unreachable` | PIA's proxy can't resolve hostnames itself. The `pia` route already resolves names on your PC. If you entered PIA as a custom `socks5h://` route, use `socks5://` instead. |
| `the 'pia' route needs PIA SOCKS credentials` | Credentials not found: check `.env` is named exactly `.env` (not `.env.txt`) and sits next to `fast_downloader.py`. |
| `SOCKS routes need PySocks` | Run `pip install PySocks`. |
| Both routes show the same IP | The PIA app is connected in full-tunnel mode. Disconnect it, or split-tunnel it. |

### Server location

PIA runs its SOCKS5 proxy only in the **Netherlands**
(`proxy-nl.privateinternetaccess.com:1080`). There is no US or other-country host.
DNS picks one of about 30 Netherlands servers per lookup. Because PIA's proxy
can't resolve hostnames, the names of the sites you download from are looked up
by your own DNS, not through PIA. For other countries, use
[`vpn:` routes](#pia-over-openvpn).

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
2. Add your **normal** PIA login (the `p…` one you use for the PIA app, *not* the SOCKS
   credentials) to `.env`:
   ```
   PIA_VPN_USER=p1234567
   PIA_VPN_PASS="your-normal-pia-password"
   ```
3. Click **VPN regions…**, filter (for example `us`), select one or more regions, and click **Add**.
   Each becomes a `vpn:<region>` line. PIA's config files are downloaded to `pia_openvpn/` the first time.
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

- **Storage:** the output is preallocated as `<name>.part` (`truncate(size)`). Every
  worker opens it itself and `seek`s to its own offset, so no merging is needed. It's
  renamed with `os.replace` when every piece is done. Existing names get ` (1)`, ` (2)`… appended.
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
- **Pause/resume** keeps pieces, learned route limits and tunnels. It works only while the
  app is open; progress isn't saved to disk.
- **Servers without range support** get a single plain GET, restarted from zero on retry.

### Routes

Each `Route` wraps a `urllib` opener:

| Route | Mechanism |
|---|---|
| `direct` | the default opener (honours system proxy settings) |
| Local IP | custom `HTTPConnection`/`HTTPSConnection` handlers with `source_address=(ip, 0)` |
| HTTP proxy | `ProxyHandler` |
| SOCKS | PySocks `create_connection` inside custom connection classes; TLS is wrapped on top with SNI. With `socks5://` (and `pia`), names are resolved locally to IPv4 first. When a proxy hostname resolves to several servers, PySocks tries each in turn. |
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
  - `--data-ciphers AES-128-GCM:AES-256-GCM:AES-128-CBC` (PIA negotiates GCM);
  - `--allow-compression asym`;
  - `--management-hold --management-query-passwords --auth-nocache --auth-retry none`.
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
| `.env` (git-ignored) | `PIA_SOCKS_USER/PASS` (`x…`), `PIA_VPN_USER/PASS` (`p…`), optional `PIA_SOCKS_HOST/PORT`. Parsed by `load_dotenv()`: `KEY=VALUE`, quotes, `export`, `#` comments. Real environment variables win. |
| `%USERPROFILE%\.fast_downloader.json` | routes, folder, segment count, PIA-only, PIA dialog settings (the password only if *Remember* is ticked) |
| `pia_openvpn/` (git-ignored) | cached PIA OpenVPN configs |
| `%TEMP%\fastdl_vpn_*` | per-connect management password files (deleted once used) |
| `fast_downloader.ico`, `create_shortcut.ps1` | icon; creates Desktop and Start Menu shortcuts running `pythonw.exe` (no console) |

Tunable constants are at the top of the file: `CHUNK`, `MIN_SEGMENT`, `STEAL_MIN`,
`MAX_RETRIES`, `TIMEOUT`, `RAMP_UP_AFTER`, `VPN_CONNECT_TIMEOUT` and `VPN_ARGS`.

### Known limitations

- Progress isn't saved to disk, so a download can't be resumed after the app closes.
- Only one download at a time.
- Site names are always resolved by your own DNS, including for `pia` and `vpn:` routes.
- `vpn:` routes are Windows-only and need administrator rights. They don't coexist with
  the PIA app while it's connected.

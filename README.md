# Fast Downloader

A segmented, multi-connection downloader with a desktop UI. Files are split into
pieces that download in parallel, optionally over different routes (your normal
connection, a VPN adapter, or proxies such as PIA's SOCKS5 proxy), so the pieces
come from different IP addresses. Idle connections take over the remaining work
of slow ones, so a slow route never holds up the finish.

## Running

- **Icon:** double-click **Fast Downloader** on the Desktop or in the Start Menu.
- **Terminal:** `python fast_downloader.py` (useful for seeing errors if it won't start).

If you move this folder or reinstall Python, recreate the shortcuts:

```powershell
powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
```

Requirements: Python 3.9+ with Tkinter (included in standard Windows installs).
SOCKS routes, including PIA, also need `pip install PySocks`.

## Routes

Enter one route per line in the **Routes** box. Connections are spread across
them in turn; list a route twice to give it more connections.

| Route | Meaning |
|---|---|
| `direct` | Your normal connection |
| `pia` | PIA's SOCKS5 proxy (see below) |
| `10.8.0.2` | Bind to this local IP, e.g. a VPN adapter (**Add local IPs** lists them) |
| `http://host:port` | HTTP proxy (`user:pass@` allowed) |
| `socks5://user:pass@host:port` | SOCKS5 proxy (`socks5h://` resolves DNS through the proxy) |

**Check IPs** shows the public IP each route actually gets.

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
by your own DNS, not through PIA. To exit in another
country, connect the PIA app to that region with split tunnelling, and add its
adapter IP as a route (untested; confirm with **Check IPs**).

# KiwiSDR Remote Client

A receive-only desktop and headless client for remote KiwiSDR receivers. Tune a
receiver, send its audio to speakers or a virtual cable, and record sessions while
fldigi, WSJT-X or JS8Call handles decoding.

This project builds on [jeffreycentex/kiwisdr-remote](https://github.com/jeffreycentex/kiwisdr-remote)
and uses a pinned copy of [jks-prv/kiwiclient](https://github.com/jks-prv/kiwiclient)
for the receiver protocol. This repository contains the compact interface and the
control, recording and security improvements developed from that codebase.

![Compact light-mode interface](docs/images/main-window.png)

## What it does

- **Tune:** USB, LSB and AM, with adjustable bandwidth and receiver-specific frequency limits.
- **Listen:** output to speakers or a virtual audio cable, with drift correction and resampling.
- **Record:** native-rate mono WAV, or AAC in M4A/MP4 when PyAV is installed.
- **Control:** hamlib NET rigctl on `127.0.0.1:4532`, plus an HTTP/JSON API on `127.0.0.1:7373`.
- **Run headless:** connect, tune and record without loading Qt.
- **Use a compact desktop:** a 680 × 600 light-mode window with Listen & record, Audio and Automation tabs. Smaller windows scroll; window geometry is saved.

The GUI, API and hamlib tuning share one service command queue. Recording has a
separate bounded writer, exclusive file creation, and WAV rotation at 1 GiB.
Existing recordings are never intentionally replaced, including numbered parts.

## Install and run

Requires Python 3.10 or newer. Install dependencies in a virtual environment:

```bash
git clone https://github.com/jeffreycentex/kiwisdr-remote-client.git
cd kiwisdr-remote-client
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run_kiwiremote.py
```

Linux also needs PortAudio (`sudo apt install libportaudio2` on Debian/Ubuntu)
and the usual Qt runtime libraries. `samplerate` provides higher-quality
resampling and is skipped on Windows; linear interpolation is the fallback.
PyAV supplies M4A/MP4 recording. WAV works without it. `certifi` supplements the
system TLS trust store.

The Python package and launchers retain their original names (`kiwiremote`,
`run_kiwiremote.py`, and `kiwiremote-cli`) for compatibility. The desktop app is
still named **KiwiSDR Remote**.

## Desktop quick start

1. Enter `host:port`, or `https://host[:port]` for a TLS receiver. Supply a receiver password only if needed, then choose **Connect**.
2. Set frequency in kHz, mode and bandwidth on **Listen & record**.
3. Choose an output device on **Audio**. For decoding software, select a virtual cable rather than speakers. See [virtual audio setup](docs/VIRTUAL_AUDIO.md).
4. Select a recording format and folder, then choose **Record**. **Open** reveals the output folder.
5. Use **Automation** to configure rigctld, the API and session limits.

Keyboard shortcuts use Ctrl on Windows/Linux and Command on macOS:

| Shortcut | Action |
|---|---|
| Ctrl/Command+L | Focus receiver address |
| Ctrl/Command+F | Focus frequency |
| Ctrl/Command+R | Start or stop recording |
| Ctrl/Command+Return | Connect or disconnect |

## Hamlib, API and headless mode

For WSJT-X or JS8Call, choose rig **Hamlib NET rigctl** and network server
`127.0.0.1:4532`. This app is receive-only; PTT commands are accepted and ignored.
For fldigi, enable Hamlib with the same rig and address.

```bash
rigctl -m 2 -r 127.0.0.1:4532 F 7074000
rigctl -m 2 -r 127.0.0.1:4532 M USB 2800

# Start the service in one terminal (API always enabled in headless mode).
python run_kiwiremote.py --headless --record-dir ./recordings

# Control it from another terminal.
python run_kiwiremote.py ctl connect kiwi.example.net:8073 --wait 10
python run_kiwiremote.py ctl tune --khz 7074 --mode USB
python run_kiwiremote.py ctl record-start --name "{date}_net" --max-seconds 3600
python run_kiwiremote.py ctl record-stop
python run_kiwiremote.py ctl disconnect
```

The GUI enables the API on the Automation tab. All interfaces share tuning and
recording state. See [the API reference](docs/API.md) for endpoints, authentication,
error codes, filename templates and resource limits.

## Connections, recordings and security

- Connections retry after 5 seconds, or after 15 seconds when the receiver is busy. A bad password or invalid TLS certificate stops retries.
- TLS verifies certificates. Redirects cannot downgrade TLS to plaintext or forward a password to a different hostname or port. Connect explicitly to the destination if you trust it.
- Both control services default to loopback. The HTTP API requires a token for non-loopback binding, but remains plaintext; rigctld has no authentication. Use an authenticated encrypted tunnel for remote access.
- Browser protections require JSON POST bodies and an allowed Host header; the API does not enable CORS. Request sizes, client counts and WebSocket messages are bounded.
- Recording filenames are reserved atomically. A sample-rate change creates a new part; WAV also rotates at 1 GiB of PCM data. M4A/MP4 uses mono AAC at 48 kHz and 64 kbit/s.
- WAV reconnect gaps are filled with silence, up to 30 minutes per gap. M4A/MP4 does not insert reconnect silence. Storage overload stops recording with an error rather than silently dropping queued audio.
- Receiver passwords are neither saved nor included in application status or authentication logs. An optional API token is stored in Qt settings; protect access to your user account and settings files.

Default session policy: sessions without a password end after 45 minutes;
passworded sessions have no session limit. A periodic reset disconnects for
15 seconds every 120 minutes, retaining a running recording. Local receivers
(loopback, private or link-local connected peer addresses) are exempt. Per-connect
overrides take precedence; zero disables a limit or reset. See the API reference
for exact timer behavior.

## Tests and validation

```bash
python -m pip install numpy
python -m unittest discover -s tests -v
```

The minimal suite uses a fake KiwiSDR and requires no audio hardware. Real Qt GUI
and PyAV recording tests run when those dependencies are installed; otherwise they
are skipped. Qt tests run offscreen with isolated settings. CI runs the suite on
Linux, Windows and macOS with Python 3.10 and 3.13.

This client has been validated locally on macOS with real Qt and PyAV, a standalone
app build, and live tuning/WAV recording against
`newbraunfels.proxy.kiwisdr.com:8073`. That verifies receiving and recording;
physical audio devices and decoder applications still need testing on each system.

## Build standalone apps

```bash
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm kiwiremote.spec
```

The pinned kiwiclient sources are included; downloading them again is unnecessary.
Use `python fetch_kiwiclient.py` only when intentionally refreshing that copy.
PyInstaller builds for the current OS; it does not cross-compile.

Outputs include a windowed **KiwiSDR Remote** and console **kiwiremote-cli** in
`dist/`. On macOS, the GUI is `dist/KiwiSDR Remote.app`. Local macOS builds are
ad-hoc signed and not notarized. GitHub Actions can build Windows x64, macOS Apple
silicon and Linux x64 archives through the **Build** workflow. Publishing a `v*`
tag triggers a release; normal pushes do not create releases.

## Code layout

| Path | Purpose |
|---|---|
| `kiwiremote/gui.py` | Light-mode desktop, settings and queued Qt events |
| `kiwiremote/service.py` | Qt-free session policy and application operations |
| `kiwiremote/commands.py` | Bounded command queue with one owner thread |
| `kiwiremote/radio.py` | Kiwi transport, TLS, redirects, reconnects and audio delivery |
| `kiwiremote/sinks.py` | Playback, buffered recording, file parts and resampling integration |
| `kiwiremote/api.py`, `ctl.py`, `headless.py` | HTTP server, CLI client and headless launcher |
| `kiwiremote/rigctld.py`, `network.py` | Hamlib adapter and bounded control connections |
| `kiwiremote/state.py`, `resample.py` | Tune/server models and streaming resampling |
| `tests/` | Fake receiver and unit/integration tests |
| `AGENTS.md` | Contributor and agent guidance |

## License and attribution

Licensed under **GPL-3.0-or-later**; see [LICENSE](LICENSE). This project retains
history and attribution from [KiwiSDR Remote](https://github.com/jeffreycentex/kiwisdr-remote).
Vendored [kiwiclient](https://github.com/jks-prv/kiwiclient) sources retain their own
licenses: parts are GPL-2.0-or-later and `mod_pywebsocket` is BSD. Keep upstream
copyright and license notices when redistributing source or binaries.

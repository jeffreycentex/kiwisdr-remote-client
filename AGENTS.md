# AGENTS.md

Guidance for contributors and coding agents working on KiwiSDR Remote Client.

## Repository boundaries
- This project is `jeffreycentex/kiwisdr-remote-client`, derived from `jeffreycentex/kiwisdr-remote`. Preserve upstream history, copyright and license notices.
- Do not commit or push to the original `kiwisdr-remote` repository. Publishing to this client repository requires user authorization; creation of its initial public version was explicitly authorized. Do not treat that as permission for future releases.
- Never commit recordings, credentials, personal Qt settings, virtual environments or generated app bundles. Test TLS private keys in `tests/tls/` are public fixtures, never production credentials.
- Keep package/launcher names and the v1 API identity compatible unless an intentional migration is requested.

## What this is
KiwiSDR Remote is a desktop app (Windows, macOS, Linux; PySide6 GUI) for listening to a remote KiwiSDR. It tunes the Kiwi, plays its audio to an output device (usually a virtual cable feeding fldigi, WSJT-X or JS8Call), records WAV or M4A, and offers two control interfaces:
- a hamlib **rigctld** server (port 4532), for frequency and mode
- an **HTTP/JSON API** (`/api/v1`, port 7373), for server selection, recording and session handling

A **headless mode** runs everything except the GUI. A companion project, [fldigi-net-scheduler](https://github.com/jeffreycentex/fldigi-net-scheduler), drives this app through the API and through fldigi → rigctld.

The app is **receive-only**. rigctld accepts PTT commands and ignores them.

## Layout
- `kiwiremote/state.py`: tune and server models, mode aliases, passband rules. Frequencies are in **Hz**; the Kiwi wire protocol uses kHz, and conversion happens only in `radio.py`.
- `kiwiremote/radio.py`: `RadioController`. One worker thread owns the Kiwi websocket. It handles reconnects, connection states (`idle/connecting/connected/reconnecting/busy/failed`), audio sinks and gap filling. `_KiwiAudioStream` overrides parts of kiwiclient (redirect handling, password masking, the per-packet compression flag).
- `kiwiremote/service.py`: `KiwiService`. **Qt-free.** It is the single entry point for connect, disconnect, tune and record, and it enforces the session policy (limits, periodic reset, local exemption) and the recording `max_seconds`. The GUI, API and headless mode all call it. Put testable logic here.
- `kiwiremote/commands.py`: bounded synchronous submission to one service owner thread; `@command` serializes mutations. Connect readiness waits happen outside the queue.
- `kiwiremote/network.py`: shared cap on simultaneous control clients.
- `kiwiremote/api.py`: stdlib `ThreadingHTTPServer`. Routes are a dict of `(method, path) -> handler`.
- `kiwiremote/ctl.py`: the `ctl` CLI, plus `ApiClient` (stdlib only, so other projects can copy it).
- `kiwiremote/headless.py`: the `--headless` entry point. It must never import PySide6.
- `kiwiremote/gui.py`: GUI only. Events arrive from other threads through `_Bridge` (a queued Qt signal). Buttons only *request* actions; widget state is updated from events (`state`, `recording`, `session`, `tune`), so changes made by the API and rigctld show up in the GUI.
- `kiwiremote/sinks.py`: `SoundCardSink` (drift-locked resampling), `BufferedFileSink`, `WavSink`, `M4aSink`, and Linux PulseAudio/PipeWire helpers.
- `kiwiremote/rigctld.py`: the NET rigctl (model 2) subset that fldigi and WSJT-X use.
- `third_party/kiwiclient`: a pinned copy of jks-prv/kiwiclient (GPL). Don't edit it. Override its behaviour in `radio.py` instead, and update it with `fetch_kiwiclient.py`.
- `tests/fake_kiwi.py`: a fake KiwiSDR websocket server that sends ADPCM or raw audio and supports busy and password modes. Tests run against it. `TlsFront` puts an https front on it, using the self-signed test certificate in `tests/tls/` (for localhost and 127.0.0.1; test use only).

## Run / test
```bash
python -m pip install -r requirements.txt
python run_kiwiremote.py [--debug]                 # GUI
python run_kiwiremote.py --headless --help          # no window
python run_kiwiremote.py ctl status                 # API client
python -m unittest discover -s tests -v             # needs only numpy
```
- The minimal suite needs only numpy and no audio hardware. Real offscreen Qt tests and PyAV tests run when installed, otherwise skip. Keep both dependency sets working. They must pass on Linux, Windows and macOS, because CI runs them on all three (`.github/workflows/ci.yml`).
- In a sandbox without PySide6, GUI logic can be exercised with a stub PySide6 package on `sys.path` together with `fake_kiwi`. That catches logic errors, not Qt API misuse. Say so when the GUI hasn't run under real Qt.
- Builds: `.github/workflows/build.yml` runs PyInstaller with `kiwiremote.spec`. That produces a windowed `KiwiSDR Remote` and a console `kiwiremote-cli` sharing one bundle. Pushing a `v*` tag creates a release.

## Threading and locking rules
- GUI/API/hamlib mutations go through `KiwiService`; construct `RigctldServer` with `service=...` in app entry points. Avoid direct radio mutation from new UI actions.
- The command queue owns control mutations. Radio and recording threads must not synchronously submit service commands: the owner may be joining them. Events from workers must stay observational/nonblocking; Qt forwards them through queued signals.
- Recording I/O runs on `BufferedFileSink`'s bounded writer. Do not silently discard recorded audio on overload; surface an error and finalize files. Stop must drain pending audio.
- Only the worker thread touches the Kiwi socket. Tune changes set a dirty flag that the worker applies between messages.
- `RadioController.disconnect()` joins the worker thread. Never call `radio.connect()` or `radio.disconnect()` while holding `KiwiService._lock`, and never take that lock in `_on_radio_event`, which runs on the worker thread. Doing either deadlocks.
- `KiwiService` takes an injectable `clock` and `start_ticker=False`, so tests can call `tick()` on simulated time. Use that for anything time-based; don't sleep for minutes in tests.

## Session policy (specified by the user; don't change it silently)
- With a password there is no limit: the session keeps going and reconnects at each periodic reset. Without a password the limit is 45 min. A limit is total connected time; resets don't restart it. When it's reached, the recording is closed and the app disconnects. (Until v0.2.0 the password limit was 120 min; `gui.py` migrates saved settings via `policy_version`.)
- The periodic reset is every 120 min of connection: disconnect, wait 15 s, reconnect. The recording continues, and silence is inserted for the gap.
- Kiwis on loopback, private or link-local **peer** addresses are exempt from both. The user's own Kiwi is on the LAN.
- Per-connect overrides (`session_limit_min`, `reset_interval_min`) win, even for local Kiwis. `0` means off.

## API rules
- The API stays versioned under `/api/v1`. Additive changes only: new optional fields and new endpoints. A breaking change goes in `/api/v2`. Keep `docs/API.md` in step with the code, and keep error codes stable.
- Local-only by default. Keep the three browser protections: JSON content type required, loopback `Host` header, no CORS. Binding to a non-loopback address requires a token.
- The API records **WAV only**; only the GUI may pass `formats` that allow M4A.
- Never log or return Kiwi passwords. Entire `SET auth` contents are redacted in debug logs.
- Reject TLS downgrades and password forwarding across hostname/port boundaries in both redirect paths. Enforce frame/message, body, line, connection and queue limits.
- Reject non-finite numbers before mutating state. Validate combined tuning requests before applying any part.
- HTTP remains plaintext and rigctld unauthenticated. Document encrypted tunnels for remote access; a bearer token does not encrypt traffic.

## Conventions
- License: GPL-3.0-or-later. New source files get the `# SPDX-License-Identifier: GPL-3.0-or-later` header.
- Use the standard library for the API and CLI. Runtime dependencies are PySide6, numpy and sounddevice; samplerate, av and certifi are optional. `samplerate` is skipped on Windows through a requirements marker.
- Reserve every recording file with exclusive creation, including numbered parts; never rely on exists-then-open for overwrite protection. WAV rotates at 1 GiB of PCM, and duration must remain correct across sample-rate changes.
- Desktop defaults to light mode and a compact 680 × 600 layout. Keep recording controls visible at the default size, allow scrolling on smaller screens, and preserve keyboard/accessibility behavior. GUI tests must isolate QSettings and avoid opening physical audio devices.
- Python 3.10 or newer (CI tests 3.10 and 3.13).
- Before publishing, run appropriate tests and `git diff --check`; inspect staged files and the push destination. Use descriptive commits only when authorized. Do not push tags or create releases without an explicit request.

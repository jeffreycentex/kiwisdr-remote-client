# Virtual audio setup


The app plays into the *input* side of the virtual cable. The decoding program records from the *output* side.

| OS | Install | In KiwiSDR Remote, play to | In WSJT-X / fldigi, input from |
|---|---|---|---|
| Windows | [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) | `CABLE Input (VB-Audio Virtual Cable)` | `CABLE Output (VB-Audio Virtual Cable)` |
| macOS | [BlackHole](https://existential.audio/blackhole/) (`brew install blackhole-2ch`) | `BlackHole 2ch` | `BlackHole 2ch` |
| Linux | none. Click **Create 'KiwiSDR' virtual cable**, or run the commands below | `PulseAudio/PipeWire sink: KiwiSDR` | `KiwiSDR-RX` |

Windows lists every device once per host API (MME, DirectSound, WASAPI). MME or WASAPI both work. The app always opens a device at its default sample rate and resamples to it.

On first start, the app pre-selects a device whose name looks like a virtual cable.

### Linux virtual cable

A Linux virtual cable has two parts:

- A **null sink** called `KiwiSDR`. KiwiSDR Remote plays into it.
- A **remapped source** called `KiwiSDR_RX`, shown as `KiwiSDR-RX`. It is built from the sink's monitor and is what WSJT-X, JS8Call or fldigi select as **Input**.

The remapped source is needed because WSJT-X and JS8Call list devices through Qt Multimedia, which often hides monitor sources such as "Monitor of KiwiSDR". Without it, the KiwiSDR sink appears under Output but nothing usable appears under Input. For listen-only use, Output can be any device, because nothing is transmitted.

These commands create the cable until the next logout or reboot. The GUI button runs the same commands and skips any part that already exists:

```bash
pactl load-module module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR
pactl load-module module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX
```

Restart WSJT-X or JS8Call afterwards, because they only list audio devices at startup.

To make it permanent on **PipeWire** (Fedora, Ubuntu 22.10+), create the file `~/.config/pipewire/pipewire-pulse.conf.d/kiwisdr.conf`, then run `systemctl --user restart pipewire-pulse`:

```
pulse.cmd = [
  { cmd = "load-module" args = "module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR" flags = [ ] }
  { cmd = "load-module" args = "module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX" flags = [ ] }
]
```

To make it permanent on **PulseAudio**, add these lines to `~/.config/pulse/default.pa`:

```
.include /etc/pulse/default.pa
load-module module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR
load-module module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX
```

If WSJT-X still shows **no input devices at all**:

- `pactl list short sources` should list `KiwiSDR_RX`. If it doesn't, the module didn't load.
- A **Flatpak** build needs sound-server access: `flatpak override --user --socket=pulseaudio <app-id>`. A **snap** build needs its `audio-record` interface connected.
- If Qt uses ALSA rather than Pulse, install `pipewire-alsa`, or `alsa-plugins-pulseaudio` / `libasound2-plugins` on PulseAudio. The `pulse`/`default` input then appears, and you can route it to `KiwiSDR-RX` in `pavucontrol` → Recording.

PortAudio cannot address individual Pulse sinks. The app works around this by opening PortAudio's `pulse` device and setting `PULSE_SINK` to the selected sink. Plain ALSA devices, including `snd-aloop` loopback, appear in the list as normal devices.

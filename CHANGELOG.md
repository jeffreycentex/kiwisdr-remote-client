# Changelog

## 0.1 — digitalmodes branch

- Added embedded MFSK16, MFSK32, MFSK64, and Contestia 4-250 decoding on the Data tab.
- Added live decoder squelch, a quality meter, and RSID Auto mode for the four supported modes.
- Added audio-file decoding alongside receiver audio.
- Added FLAMP file assembly and saving, including CRC validation, missing-block tracking, repeated transmissions, binary encodings, and LZMA decompression.
- Included the pinned GPL decoder source, license notices, native build tooling, and automated known-text tests.

## 1.1

- Added AMN, AMW, and FMN tuning modes.
- Added up to 15 named Quick Tune buttons from `quicktune.txt`. The buttons update when the file is saved and disappear when it is deleted.
- Added a DSP tab with a KiwiSDR Noise Blanker, Auto Notch, and Noise Reduction, plus local Audio Bandpass and Manual Notch filters.
- Added a Log tab with plain-text export and moved Record to file to the Audio tab.
- Fixed the macOS app bundle so it appears in the Dock and can receive keyboard input.

## 1.01

- Added a radio waveform icon to the Windows GUI executable and macOS app bundle.

## 1.0

- Packaged the Windows and Linux GUI and command-line tools as standalone executables with their dependencies bundled inside.

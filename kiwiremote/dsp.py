# SPDX-License-Identifier: GPL-3.0-or-later
"""Streaming audio filters for the receiver worker."""

import numpy as np

try:
    from scipy import signal
except ImportError:
    signal = None

HAVE_SCIPY = signal is not None
DEFAULT_AUDIO_BANDPASS = (False, 1500, 2400)
DEFAULT_AUDIO_NOTCH = (False, 1000, 50)


class AudioFilter:
    def __init__(self, *, notch=False):
        self._notch = notch
        self._key = None
        self._sos = None
        self._delays = None

    def process(self, samples, rate, settings):
        enabled, center, width = settings
        key = (rate, settings)
        if key != self._key:
            self._key = key
            self._sos = self._delays = None
            if enabled and HAVE_SCIPY:
                low, high = center - width / 2, center + width / 2
                if not 0 < low < high < rate / 2:
                    name = "notch" if self._notch else "bandpass"
                    raise ValueError("Audio %s bypassed: %.1f–%.1f Hz must be above 0 "
                                     "and below %.1f Hz at this sample rate"
                                     % (name, low, high, rate / 2))
                if self._notch:
                    b, a = signal.iirnotch(center, center / width, fs=rate)
                    self._sos = signal.tf2sos(b, a)
                else:
                    self._sos = signal.butter(4, (low, high), btype="bandpass", fs=rate, output="sos")
                self._delays = np.zeros((len(self._sos), 2))
        if self._sos is None or not len(samples):
            return samples
        filtered, self._delays = signal.sosfilt(self._sos, samples, zi=self._delays)
        return np.clip(np.rint(filtered), -32768, 32767).astype(np.int16)

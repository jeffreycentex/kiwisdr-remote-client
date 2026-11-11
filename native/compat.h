// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

// Desktop transmit timing is unused by the bundled receive-only adapter.
// The generated CW source also includes this header on MinGW.
#ifdef __cplusplus
inline unsigned int timeBeginPeriod(unsigned int) { return 0; }
inline unsigned int timeEndPeriod(unsigned int) { return 0; }
#endif

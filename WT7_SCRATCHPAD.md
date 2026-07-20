# WT7 Scratchpad

This file is for observations, concerns, and follow-up items that should not be lost during WT7 development.

## Scan Calibration On Weak Sources

Status: open

Observation:

- Scan calibration results on weak sources do not yet feel trustworthy.
- Averaging appears to affect the fitted scan result in ways that are not fully understood.
- Changing averaging settings can make the apparent boresight position shift, even when the antenna pointing has not physically changed.

Current hypothesis:

- Some of the behaviour may be caused by the relationship between B210 sample collection, GUI smoothing, dwell time, scan step timing, and source signal-to-noise ratio.
- The recent change separates scan dwell averaging from GUI smoothing, but weak-source behaviour still needs field testing.

Things To Revisit:

- Define exactly what the scan point power value should represent.
- Confirm whether each scan point should use raw B210 samples, per-point averaged B210 readings, or a longer integration independent of the GUI refresh rate.
- Decide whether the scan dialog should expose separate scan integration parameters instead of reusing B210 display averaging.
- Add diagnostic metadata to scan CSV files, including B210 update rate, number of raw readings per dwell, dwell start/end time, antenna settled position, and whether any stale/late samples were discarded.
- Compare scans of the same source using different dwell times, increments, B210 rates, and averaging values.
- Consider plotting error bars or point variance for weak-source scans.
- Consider fitting only after optional baseline removal and with user-visible fit confidence metrics.

Test Notes:

- Record source, antenna, scan axis, scan direction, span, increment, dwell, B210 rate, GUI Hz, Avg, gain, bandwidth, and fitted centre.
- Repeat scans without changing antenna calibration to check whether fitted centre moves with averaging or integration settings.
- Compare high-to-low and low-to-high azimuth scans separately so backlash is not confused with averaging effects.

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

## OLED Display Alignment

Status: open

Observation:

- The controller OLED displays need to be re-aligned with the current WT7 app behaviour and terminology.
- There may now be a significant mismatch because OLED behaviour has not been checked closely for a while during GUI, scan, B210, Y factor, and tracking development.
- Any displayed status, source name, target AZ/EL, antenna AZ/EL, scan/tracking/slewing/parking state, and stopped/offline/fault state should be checked against what the main app shows.

Things To Revisit:

- Audit every place WT7 writes to the OLED displays.
- Compare OLED output with GUI state during connect, disconnect, stopped, manual jog, tracking, slewing, parking, scan calibration, peak calibration, Y factor, fault, and stop-all workflows.
- Confirm that East and West OLED displays show only their own antenna state unless a shared target/source value is intentionally displayed.
- Confirm that OLED source labels remain correct during scan and Y factor workflows.
- Decide whether OLED layout needs a refreshed standard format for WT7.

## Y Factor Workflow

Status: open

Observation:

- During Y Factor dwell time, the app may not continue tracking the selected source closely enough.
- This could bias hot/cold power measurements if the antenna drifts or the target position changes during a long dwell.
- The Y Factor popup currently appears coupled to the main app window and cannot be moved independently.

Things To Revisit:

- Confirm whether the selected antenna continues source tracking corrections during every Y Factor dwell phase.
- Ensure hot and cold target positions are refreshed continuously during Y Factor slews and dwell periods.
- Confirm that the non-selected antenna remains stopped, as intended, during the measurement.
- Decouple the Y Factor dialog from the main window so it can be moved independently while the main app remains visible and usable.

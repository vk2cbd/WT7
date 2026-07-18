# WT7

WT7 is the PyQt5 alpha of the two-antenna radio astronomy controller. It keeps the established safety/protocol backend, B210 dual-channel power primitives, source position calculations, event logging, and configuration format, while introducing a new Qt main operating surface.

## Install

```bash
python3 -m pip install -r requirements.txt
```

On Ubuntu, also install the system packages required for serial access and B210 support as before: pyserial access through the `dialout` group, UHD/SoapySDR for the B210, and the correct persistent `/dev/serial/by-id/...` paths in the ini file.

## Configure

```bash
cp wt7_ubuntu.ini.example wt7_ubuntu.ini
nano wt7_ubuntu.ini
```

Existing prior-version `.ini` files can normally be copied to `wt7_ubuntu.ini`; section names are unchanged. Check antenna ports, B210 frequency/rate/gain, observer location, limits, park positions, and selected source before driving antennas.

## Run

```bash
python3 wt7_ubuntu_gui.py --config wt7_ubuntu.ini
```

## Alpha Notes

The PyQt5 main control surface currently includes connect/disconnect, guarded manual jogs, Sun/Moon/source tracking, park, live Sun/Moon/time reference, live antenna positions, and B210 dual-channel power display/logging.

The full renamed Tkinter implementation is retained as `wt7_tk_legacy_gui.py` during the transition for secondary dialogs that still need detailed PyQt5 porting.

## Key Files

- `wt7_ubuntu_gui.py` - PyQt5 main operator interface alpha
- `wt7_tk_legacy_gui.py` - renamed legacy Tkinter GUI fallback
- `wt7_antenna.py` - Arduino protocol, controller session, and guarded motion
- `wt7_config.py` - `.ini` loading/saving
- `wt7_b210_power.py` - B210 dual-channel power meter primitives
- `wt7_astro.py` / `wt7_solar.py` - source, Sun, and Moon position calculations
- `wt7_logging.py` - JSON-lines event log
- `WT7_REQUIREMENTS.md` - carried-forward system requirements/specification

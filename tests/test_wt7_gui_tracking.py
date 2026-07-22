import os
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from wt7_antenna import Axis
from wt7_astro import TargetPosition
from wt7_config import ScanConfig
from wt7_b210_power import B210PowerReading
from wt7_ubuntu_gui import WT7App


class _FakeLimits:
    def assert_position_allowed(self, azimuth, elevation):
        if elevation < 0.0:
            raise RuntimeError(f"Elevation {elevation:0.2f} outside limits")

    def azimuth_delta_to_target(self, current, target):
        return ((target - current + 540.0) % 360.0) - 180.0


class _FakeSession:
    def __init__(self):
        self.config = SimpleNamespace(limits=_FakeLimits())
        self.stopped = False

    def stop_all(self):
        self.stopped = True


class TrackingStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def make_app(self):
        content = """
[site]
latitude = -32.724000
longitude = 152.130167
selected_source = Bad Source

[antenna:East]
port = loop://
baud = 9600

[source:Bad Source]
ra_hours = 0.0
dec_degrees = -80.0
"""
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "wt7.ini"
        path.write_text(content.strip(), encoding="utf-8")
        app = WT7App(path)
        self.addCleanup(app.close)
        self.addCleanup(tmp.cleanup)
        return app

    def test_invalid_source_does_not_start_tracking_worker(self):
        app = self.make_app()
        calls = []
        app.run_thread = lambda fn, name="": calls.append((fn, name))
        app.sessions = {"East": _FakeSession()}
        app.current_tracking_target = lambda kind: TargetPosition("Bad Source", 10.0, -5.0)

        app.start_tracking("source")

        self.assertEqual(app.tracking_kind, "")
        self.assertEqual(calls, [])

    def test_replaced_tracking_worker_keeps_its_original_stop_event(self):
        app = self.make_app()
        calls = []
        seen = []
        app.run_thread = lambda fn, name="": calls.append((fn, name))
        app.sessions = {"East": _FakeSession()}
        app.current_tracking_target = lambda kind: TargetPosition(kind.title(), 10.0, 20.0)
        app.tracking_loop = lambda kind, stop: seen.append((kind, stop is app.tracking_stop))

        app.start_tracking("sun")
        first_worker = calls[0][0]
        app.start_tracking("moon")

        first_worker()

        self.assertEqual(seen, [("sun", False)])


    def test_scan_preload_uses_antenna_compensation(self):
        app = self.make_app()
        app.configs["East"].az_low_to_high_compensation = 0.5
        high_to_low = ScanConfig(span_degrees=5.0, increment_degrees=0.5, antenna_name="East", az_scan_high_to_low=True)
        low_to_high = ScanConfig(span_degrees=5.0, increment_degrees=0.5, antenna_name="East", az_scan_high_to_low=False)

        self.assertAlmostEqual(app.scan_preload_offset(Axis.AZIMUTH, high_to_low, 5.0), 5.5)
        self.assertAlmostEqual(app.scan_preload_offset(Axis.AZIMUTH, low_to_high, -5.0), -5.5)


    def test_tracking_compensation_uses_source_trend_across_north(self):
        app = self.make_app()
        session = _FakeSession()
        app.tracking_kind = "sun"

        self.assertIsNone(app.az_lh_compensation_for_tracking("East", session, TargetPosition("Sun", 359.8, 45.0), "TRACKING"))
        self.assertTrue(app.az_lh_compensation_for_tracking("East", session, TargetPosition("Sun", 0.1, 45.0), "TRACKING"))
        self.assertTrue(app.az_lh_compensation_for_tracking("East", session, TargetPosition("Sun", 0.12, 45.0), "TRACKING"))
        self.assertFalse(app.az_lh_compensation_for_tracking("East", session, TargetPosition("Sun", 0.05, 45.0), "TRACKING"))


    def test_b210_clear_reading_blanks_display_and_measurement(self):
        app = self.make_app()
        app.power.set_reading(B210PowerReading(datetime.now(timezone.utc), -25.8, -25.3, 1024))
        self.assertIsNotNone(app.power.current_power_measurement("East"))

        app.power.clear_reading("SDR RELEASED")

        self.assertEqual(app.power.a_val.text(), "--.-")
        self.assertEqual(app.power.b_val.text(), "--.-")
        self.assertIsNone(app.power.current_power_measurement("East"))
        self.assertIsNone(app.power.current_power_measurement("West"))

    def test_b210_raw_measurement_is_not_gui_smoothed(self):
        app = self.make_app()
        app.power.avg.setText("2")

        app.power.set_reading(B210PowerReading(datetime.now(timezone.utc), -10.0, -20.0, 1024))
        first_sequence = app.power.power_sequence
        app.power.set_reading(B210PowerReading(datetime.now(timezone.utc), -30.0, -40.0, 1024))

        smoothed = app.power.current_power_measurement("East")
        raw = app.power.current_raw_power_measurement("East", first_sequence)

        self.assertAlmostEqual(smoothed["power_dbfs"], -20.0)
        self.assertAlmostEqual(raw["power_dbfs"], -30.0)
        self.assertIsNone(app.power.current_raw_power_measurement("East", app.power.power_sequence))


    def test_stop_scan_clears_offset_and_stops_active_antenna(self):
        app = self.make_app()
        session = _FakeSession()
        app.sessions = {"East": session}
        app.active_scan_antenna = "East"
        app.scan_stop.clear()
        app.set_scan_offset("East", Axis.AZIMUTH, 2.0)
        app.run_thread = lambda fn, name="": fn()

        app.stop_scan()

        self.assertTrue(app.scan_stop.is_set())
        self.assertTrue(session.stopped)
        self.assertEqual(app.scan_antenna_name, "")
        self.assertIsNone(app.scan_axis)
        self.assertEqual(app.scan_offset_degrees, 0.0)

    def test_yfactor_phase_target_and_error_helpers(self):
        app = self.make_app()
        session = _FakeSession()
        app.yfactor_hot_target = lambda label: TargetPosition(label, 359.9, 45.0)

        hot = app.yfactor_phase_target("hot", "Sun", "AZ/EL", 12.0, 80.0, 0.0, 0.0)
        cold = app.yfactor_phase_target("cold", "Sun", "AZ/EL", 12.0, 80.0, 0.0, 0.0)
        az_error, el_error = app.yfactor_position_error(session, SimpleNamespace(azimuth=0.1, elevation=44.9), hot)

        self.assertEqual((hot.azimuth, hot.elevation), (359.9, 45.0))
        self.assertEqual((cold.azimuth, cold.elevation), (12.0, 80.0))
        self.assertAlmostEqual(az_error, -0.2)
        self.assertAlmostEqual(el_error, 0.1)

    def test_card_target_override_does_not_change_source_pane(self):
        app = self.make_app()
        hot = TargetPosition("Sun", 10.0, 20.0)
        cold = TargetPosition("Cold Sky", 10.0, 80.0)
        app.positions = {"East": None}

        app.apply_target(hot)
        app.set_antenna_target("East", cold)

        self.assertEqual(app.current_target.name, "Sun")
        self.assertEqual(app.source_name.text(), "Sun")
        self.assertEqual(app.card_targets["East"].name, "Cold Sky")

        app.set_antenna_target("East", None)

        self.assertNotIn("East", app.card_targets)

    def test_reference_update_uses_same_sun_target_for_source_pane(self):
        app = self.make_app()
        app.tracking_kind = "sun"
        app.target_for_kind = lambda kind: TargetPosition("Sun", 12.34, 56.78) if kind == "sun" else TargetPosition("Moon", 98.76, 12.34)

        app.update_reference()

        self.assertEqual(app.sun.text(), "SUN AZ 012.34 EL 056.78")
        self.assertEqual(app.source_name.text(), "Sun")
        self.assertEqual(app.source_az.text(), "012.34")
        self.assertEqual(app.source_el.text(), "056.78")



if __name__ == "__main__":
    unittest.main()

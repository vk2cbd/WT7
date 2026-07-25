import os
import tempfile
import time
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from wt7_antenna import Axis, Position
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

    def test_antenna_card_shows_compensated_drive_target_not_source_target(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.positions = {"East": SimpleNamespace(azimuth=20.0, elevation=45.0)}
        app.configs["East"].az_low_to_high_compensation = 0.5
        app.tracking_kind = "source"

        app.apply_target(TargetPosition("Test", 30.0, 45.0))

        self.assertEqual(app.source_az.text(), "030.00")
        self.assertEqual(app.cards["East"].target.text(), "030.50 / 45.00")
        self.assertEqual(app.cards["East"].az_err.text(), "+10.50")
        self.assertEqual(app.cards["East"].az_comp.text(), "AZ comp +0.50")

    def test_card_target_override_suppresses_compensation_label(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.positions = {"East": SimpleNamespace(azimuth=20.0, elevation=45.0)}
        app.configs["East"].az_low_to_high_compensation = 0.5
        app.tracking_kind = "sun"
        app.apply_target(TargetPosition("Sun", 30.0, 45.0))

        app.set_antenna_target("East", TargetPosition("Cold Sky", 50.0, 80.0))

        self.assertEqual(app.source_name.text(), "Sun")
        self.assertEqual(app.cards["East"].target.text(), "050.00 / 80.00")
        self.assertEqual(app.cards["East"].az_comp.text(), "AZ comp --")

    def test_antenna_card_shows_no_compensation_during_high_to_low_tracking(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.positions = {"East": SimpleNamespace(azimuth=40.0, elevation=45.0)}
        app.configs["East"].az_low_to_high_compensation = 0.5
        app.tracking_kind = "source"
        app.tracking_az_comp_force["East"] = False

        app.apply_target(TargetPosition("Test", 30.0, 45.0))

        self.assertEqual(app.cards["East"].target.text(), "030.00 / 45.00")
        self.assertEqual(app.cards["East"].az_comp.text(), "AZ comp none")

    def test_az_comp_field_starts_visible_with_muted_style(self):
        app = self.make_app()

        self.assertEqual(app.cards["East"].az_comp.text(), "AZ comp --")
        self.assertEqual(app.cards["East"].az_comp.objectName(), "muted")

    def test_rate_fields_start_visible_with_muted_style(self):
        app = self.make_app()

        self.assertEqual(app.cards["East"].az_rate.text(), "AZ rate  --.-- deg/s")
        self.assertEqual(app.cards["East"].el_rate.text(), "EL rate  --.-- deg/s")
        self.assertEqual(app.cards["East"].az_rate.objectName(), "muted")
        self.assertEqual(app.cards["East"].el_rate.objectName(), "muted")

    def test_recent_events_keeps_scrollable_session_history(self):
        app = self.make_app()

        app.set_status("First event.")
        app.set_status("Second event.")
        app.set_status("Third event.")

        text = app.event_view.toPlainText()
        self.assertIn("First event.", text)
        self.assertIn("Second event.", text)
        self.assertIn("Third event.", text)
        self.assertEqual(text.splitlines()[0].split("  ", 1)[1], "Third event.")
        self.assertEqual(app.event_history[0].split("  ", 1)[1], "Third event.")

    def test_position_update_populates_rates_when_drive_active(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.positions = {"East": Position(0.0, 0.0, 10.0, 20.0)}
        app.cards["East"].set_state("SLEWING")
        app.position_rate_state["East"] = (Position(0.0, 0.0, 10.0, 20.0), time.monotonic() - 2.0)

        app.update_position("East", Position(0.0, 0.0, 20.0, 24.0))

        self.assertIn("AZ rate  5.", app.cards["East"].az_rate.text())
        self.assertIn("EL rate  2.", app.cards["East"].el_rate.text())

    def test_position_rate_ignores_too_close_samples(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.cards["East"].set_state("SLEWING")
        app.cards["East"].set_rates(0.4, 0.2)
        previous = Position(0.0, 0.0, 10.0, 20.0)
        app.position_rate_state["East"] = (previous, time.monotonic() - 0.001)

        app.update_position("East", Position(0.0, 0.0, 10.5, 20.5))

        self.assertEqual(app.position_rate_state["East"][0], previous)
        self.assertEqual(app.cards["East"].az_rate.text(), "AZ rate  0.40 deg/s")
        self.assertEqual(app.cards["East"].el_rate.text(), "EL rate  0.20 deg/s")

    def test_position_rate_averages_recent_samples(self):
        app = self.make_app()
        app.site.rate_average_samples = 3
        app.sessions = {"East": _FakeSession()}
        app.cards["East"].set_state("SLEWING")
        app.position_rate_state["East"] = (
            Position(0.0, 0.0, 0.0, 0.0),
            time.monotonic() - 1.0,
            [1.0, 2.0],
            [3.0, 4.0],
        )

        app.update_position("East", Position(0.0, 0.0, 6.0, 6.0))

        self.assertEqual(app.cards["East"].az_rate.text(), "AZ rate  3.00 deg/s")
        self.assertEqual(app.cards["East"].el_rate.text(), "EL rate  4.33 deg/s")

    def test_position_update_clears_rates_when_drive_inactive(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.cards["East"].set_state("STOPPED")

        app.update_position("East", Position(0.0, 0.0, 20.0, 24.0))

        self.assertEqual(app.cards["East"].az_rate.text(), "AZ rate  --.-- deg/s")
        self.assertEqual(app.cards["East"].el_rate.text(), "EL rate  --.-- deg/s")

    def test_tracking_state_uses_compensated_drive_target(self):
        app = self.make_app()
        session = _FakeSession()
        app.sessions = {"East": session}
        app.positions = {"East": SimpleNamespace(azimuth=205.15, elevation=42.69)}
        app.configs["East"].az_low_to_high_compensation = 0.5
        app.tracking_kind = "source"
        app.tracking_az_comp_force["East"] = True
        app.site.az_slow_threshold_degrees = 0.3
        app.site.el_slow_threshold_degrees = 0.3

        state = app.movement_display_state("East", session, TargetPosition("LMC", 204.63, 42.64), "TRACKING")

        self.assertEqual(state, "TRACKING")


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
        app.cards["East"].set_state("SCAN")
        app.scan_stop.clear()
        app.set_scan_offset("East", Axis.AZIMUTH, 2.0)
        app.run_thread = lambda fn, name="": fn()

        app.stop_scan()

        self.assertTrue(app.scan_stop.is_set())
        self.assertTrue(session.stopped)
        self.assertEqual(app.scan_antenna_name, "")
        self.assertIsNone(app.scan_axis)
        self.assertEqual(app.scan_offset_degrees, 0.0)
        self.assertEqual(app.cards["East"].state.text(), "STOPPED")

    def test_scan_dwell_refreshes_tracking_target_during_measurement(self):
        app = self.make_app()
        app.site.track_interval_seconds = 0.1
        calls = []
        nominal = TargetPosition("Sun", 10.0, 20.0)
        target = TargetPosition("Sun", 11.0, 20.0)
        app.power.current_raw_power_measurement = lambda antenna, last_sequence: {
            "power_value": -30.0,
            "power_dbfs": -30.0,
            "power_unit": "dBFS",
            "power_channel": "A",
        }

        def refresh(axis, offset, antenna):
            calls.append((axis, offset, antenna))
            return nominal, target

        app.refresh_scan_dwell_tracking = refresh

        row = app.collect_power_point(Axis.AZIMUTH, 1.0, 0.25, nominal, target, "East", 1)

        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0], (Axis.AZIMUTH, 1.0, "East"))
        self.assertEqual(row["nominal_az"], 10.0)
        self.assertEqual(row["target_az"], 11.0)

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

    def test_yfactor_dwell_refreshes_tracking_target_during_measurement(self):
        app = self.make_app()
        app.site.track_interval_seconds = 0.1
        target = TargetPosition("Moon", 30.0, 40.0)
        hot = TargetPosition("Moon", 30.0, 40.0)
        slews = []

        class _YFactorSession(_FakeSession):
            def __init__(self):
                super().__init__()
                self.config.az_track_speed = 100
                self.config.el_track_speed = 100

            def guarded_slew_to(self, azimuth, elevation, *args, **kwargs):
                slews.append((azimuth, elevation, kwargs.get("target_callback")))

            def read_position(self):
                return Position(0.0, 0.0, target.azimuth, target.elevation)

        app.power.current_power_measurement = lambda antenna: {
            "power_value": -30.0,
            "power_dbfs": -30.0,
            "power_unit": "dBFS",
            "power_channel": "A",
        }

        result = app.collect_yfactor_power(
            "East",
            0.25,
            _YFactorSession(),
            lambda: target,
            hot_target_func=lambda: hot,
        )

        self.assertGreaterEqual(len(slews), 1)
        self.assertEqual(slews[0][0:2], (30.0, 40.0))
        self.assertEqual(result["power_unit"], "dBFS")

    def test_yfactor_dwell_refreshes_displayed_cold_target(self):
        app = self.make_app()
        app.site.track_interval_seconds = 0.1
        targets = [
            TargetPosition("Cold Sky", 94.75, 80.0),
            TargetPosition("Cold Sky", 94.50, 80.0),
        ]

        class _YFactorSession(_FakeSession):
            def __init__(self):
                super().__init__()
                self.config.az_track_speed = 100
                self.config.el_track_speed = 100

            def guarded_slew_to(self, azimuth, elevation, *args, **kwargs):
                pass

            def read_position(self):
                return Position(0.0, 0.0, targets[-1].azimuth, targets[-1].elevation)

        app.power.current_power_measurement = lambda antenna: {
            "power_value": -30.0,
            "power_dbfs": -30.0,
            "power_unit": "dBFS",
            "power_channel": "A",
        }
        app.card_targets["East"] = TargetPosition("Cold Sky", 94.98, 80.0)
        call_count = {"n": 0}

        def target_func():
            call_count["n"] += 1
            return targets[min(call_count["n"] - 1, len(targets) - 1)]

        app.collect_yfactor_power(
            "East",
            0.45,
            _YFactorSession(),
            target_func,
            hot_target_func=lambda: TargetPosition("Moon", 94.50, 50.0),
            card_target=app.card_targets["East"],
        )
        app.process_events()

        self.assertAlmostEqual(app.card_targets["East"].azimuth, 94.50)
        self.assertEqual(app.card_targets["East"].elevation, 80.0)

    def test_yfactor_dialog_opens_modeless(self):
        app = self.make_app()

        app.open_yfactor_dialog()

        self.assertEqual(len(app.modeless_dialogs), 1)
        dialog = app.modeless_dialogs[0]
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.windowModality(), Qt.NonModal)

    def test_finish_yfactor_resumes_tracking_after_completed_measurement(self):
        app = self.make_app()
        calls = []
        app.start_tracking = lambda kind: calls.append(kind)
        app.card_targets["East"] = TargetPosition("Cold Sky", 10.0, 80.0)
        app.yfactor_stop.set()
        app.yfactor_hot_label = "Moon"
        app.cards["East"].set_state("YFACTOR")

        app.finish_yfactor_state("East", "moon", True)

        self.assertEqual(calls, ["moon"])
        self.assertFalse(app.yfactor_stop.is_set())
        self.assertEqual(app.yfactor_hot_label, "")
        self.assertNotIn("East", app.card_targets)

    def test_finish_yfactor_stop_does_not_resume_tracking(self):
        app = self.make_app()
        calls = []
        app.start_tracking = lambda kind: calls.append(kind)
        app.cards["East"].set_state("YFACTOR")

        app.finish_yfactor_state("East", "moon", False)

        self.assertEqual(calls, [])
        self.assertEqual(app.cards["East"].state.text(), "STOPPED")

    def test_reference_update_uses_same_sun_target_for_source_pane(self):
        app = self.make_app()
        app.tracking_kind = "sun"
        app.target_for_kind = lambda kind: TargetPosition("Sun", 12.34, 56.78) if kind == "sun" else TargetPosition("Moon", 98.76, 12.34)

        app.update_reference()

        self.assertEqual(app.sun.text(), "SUN AZ 012.34 EL 056.78")
        self.assertEqual(app.source_name.text(), "Sun")
        self.assertEqual(app.source_az.text(), "012.34")
        self.assertEqual(app.source_el.text(), "056.78")

    def test_tracking_fault_cleanup_marks_non_faulted_antennas_stopped(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.cards["East"].set_state("SLEWING")

        app.finish_tracking_fault_states()

        self.assertEqual(app.cards["East"].state.text(), "STOPPED")

    def test_tracking_fault_cleanup_preserves_slew_timeout(self):
        app = self.make_app()
        app.sessions = {"East": _FakeSession()}
        app.cards["East"].set_state("SLEW TIMEOUT")

        app.finish_tracking_fault_states()

        self.assertEqual(app.cards["East"].state.text(), "SLEW TIMEOUT")

    def test_slew_timeout_error_gets_specific_state(self):
        app = self.make_app()

        app.mark_motion_exception("East", "Slew timed out after 360.0s")

        self.assertEqual(app.cards["East"].state.text(), "SLEW TIMEOUT")
        self.assertIn("slew timeout", app.event_view.toPlainText().lower())

    def test_non_timeout_motion_error_remains_fault(self):
        app = self.make_app()

        app.mark_motion_exception("East", "azimuth no progress")

        self.assertEqual(app.cards["East"].state.text(), "FAULT")



if __name__ == "__main__":
    unittest.main()

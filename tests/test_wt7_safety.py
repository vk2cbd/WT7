import unittest

from types import SimpleNamespace

from wt7_antenna import Direction, SafeAntenna, SafetyError, SafetyLimits, WinTrakController


class SafetyLimitTests(unittest.TestCase):
    def test_wrap_dead_zone_uses_allowed_arc(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        self.assertAlmostEqual(limits.azimuth_delta_to_target(240.0, 52.0), -188.0)

    def test_target_inside_dead_zone_is_rejected(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        with self.assertRaises(SafetyError):
            limits.azimuth_delta_to_target(240.0, 267.0)

    def test_elevation_above_90_is_rejected(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        with self.assertRaises(SafetyError):
            limits.assert_position_allowed(20.0, 91.0)

    def test_margin_stops_near_upper_elevation(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0, el_margin=0.5)
        with self.assertRaises(SafetyError):
            limits.assert_move_allowed(Direction.EL_UP, 20.0, 89.8)

    def test_low_to_high_compensation_offsets_target(self):
        antenna = object.__new__(SafeAntenna)
        antenna.config = SimpleNamespace(
            az_low_to_high_compensation=0.5,
            limits=SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0),
        )
        self.assertAlmostEqual(antenna._az_target_with_low_to_high_compensation(20.0, 30.0, 45.0, True), 30.5)
        self.assertAlmostEqual(antenna._az_target_with_low_to_high_compensation(30.0, 20.0, 45.0, True), 20.0)
        self.assertAlmostEqual(antenna._az_target_with_low_to_high_compensation(20.0, 30.0, 45.0, False), 30.0)
        self.assertAlmostEqual(antenna._az_target_with_low_to_high_compensation(0.6, 0.12, 45.0, True, True), 0.62)
        self.assertAlmostEqual(antenna._az_target_with_low_to_high_compensation(0.6, 0.12, 45.0, True, False), 0.12)

    def test_oled_connected_uses_simple_connected_page(self):
        controller = object.__new__(WinTrakController)
        writes = []
        controller.oled_write = lambda prefix, column, row, text, width=None: writes.append((prefix, column, row, text, width))

        controller.oled_connected("East")

        self.assertEqual(len(writes), 11)
        self.assertIn((0xF0, 0, 0, "WT7", 8), writes)
        self.assertIn((0xF0, 0, 2, "EAST", 8), writes)
        self.assertIn((0xF0, 0, 4, "CONNECTED", 9), writes)


if __name__ == "__main__":
    unittest.main()




import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from wt7_astro import TargetPosition
from wt7_ubuntu_gui import WT7App


class _FakeLimits:
    def assert_position_allowed(self, azimuth, elevation):
        if elevation < 0.0:
            raise RuntimeError(f"Elevation {elevation:0.2f} outside limits")


class _FakeSession:
    def __init__(self):
        self.config = SimpleNamespace(limits=_FakeLimits())


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



if __name__ == "__main__":
    unittest.main()

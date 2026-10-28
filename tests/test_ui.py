"""X11 interaction tests. Run with DISPLAY set (or under xvfb-run)."""
import os
import time
import tkinter as tk
import unittest

from cwm_panel.app import DEFAULTS, Panel
from cwm_panel.system import Demo


class RecordingDemo(Demo):
    def __init__(self):
        super().__init__({})
        self.writes = []

    def volume(self, value):
        self.writes.append(('volume', value))
        super().volume(value)


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires an X11 display')
class UITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(className='Cairn')
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.host = RecordingDemo()
        self.panel = Panel(self.root, self.host, DEFAULTS, True)
        self.pump(.15)

    def tearDown(self):
        self.panel.quit()
        self.assertEqual(self.errors, [])

    def pump(self, seconds):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(.01)

    def test_open_and_refresh_do_not_write_to_hardware(self):
        self.panel.toggle('settings')
        self.pump(.1)
        self.panel.update_values()
        self.pump(.35)
        self.assertEqual(self.host.writes, [])
        self.assertLessEqual(self.panel.popup.winfo_height(), self.root.winfo_screenheight()-34)

    def test_slider_coalesces_and_commits_changes(self):
        self.panel.toggle('settings')
        self.pump(.1)
        self.panel.volume_scale.change(20)
        self.panel.volume_scale.change(31)
        self.pump(.45)
        self.assertEqual(self.host.writes, [('volume', 31)])
        self.assertEqual(self.panel.state.volume, 31)

    def test_network_scan_and_calendar_navigation(self):
        self.panel.toggle('settings')
        self.panel.show_networks()
        self.panel.scan('iwm0')
        self.pump(.2)
        self.assertEqual(len(self.panel.scan_results), 3)
        self.panel.toggle('calendar')
        month = self.panel.month
        self.panel.change_month(1)
        self.panel.change_month(-1)
        self.assertEqual(self.panel.month, month)
        self.panel.today()
        self.panel.close_popup()
        self.assertIsNone(self.panel.popup)

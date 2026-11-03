# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Qt integration, skipped by the minimal headless CI environment."""
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 not installed')
class TestGui(unittest.TestCase):
    def test_tuning_recording_and_layout(self):
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from kiwiremote import gui
        from fake_kiwi import FakeKiwi
        app = QApplication.instance() or QApplication([])
        dark = QPalette()
        dark.setColor(QPalette.Window, QColor("#101010"))
        app.setPalette(dark)
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(os.path.join(directory, 'test.ini'), QSettings.IniFormat)
            settings.setValue('rig_enable', False)
            settings.setValue('rec_dir', directory)
            with patch.object(gui, 'QSettings', return_value=settings), patch.object(gui, 'list_output_devices', return_value=[]):
                window = gui.MainWindow()
            kiwi = FakeKiwi()
            try:
                window.show()
                app.processEvents()
                self.assertEqual(window.tabs.count(), 3)
                self.assertLessEqual(window.width(), 680)
                self.assertLessEqual(window.height(), 600)
                self.assertGreater(app.palette().color(QPalette.Window).lightness(), 230)
                viewport = window.tabs.widget(0).viewport()
                self.assertLess(window.rec_btn.mapTo(viewport, window.rec_btn.rect().bottomRight()).y(), viewport.height())
                window.service.connect('127.0.0.1:%d' % kiwi.port, wait=5)
                window.service.tune(freq_hz=7100000, mode='LSB')
                app.processEvents()
                self.assertEqual(window.freq.value(), 7100)
                self.assertTrue(window.mode_buttons['LSB'].isChecked())
                self.assertEqual(window.state_badge.text(), 'Connected')
                window.service.start_recording(name='gui')
                app.processEvents()
                self.assertTrue(window.rec_btn.isChecked())
                self.assertFalse(window.rec_format.isEnabled())
                window.service.stop_recording()
                app.processEvents()
                self.assertFalse(window.rec_btn.isChecked())
                self.assertTrue(window.rec_format.isEnabled())
                window._focus_control(window.freq)
                self.assertEqual(window.tabs.currentIndex(), 0)
                if os.environ.get('KIWIREMOTE_GUI_PREVIEW'):
                    self.assertTrue(window.grab().save(os.environ['KIWIREMOTE_GUI_PREVIEW']))
            finally:
                window.close()
                kiwi.stop()
                app.processEvents()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Qt integration, skipped by the minimal headless CI environment."""
import importlib.util
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
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
            with (patch.object(gui, 'QSettings', return_value=settings),
                  patch.object(gui.QStandardPaths, 'writableLocation', return_value=directory),
                  patch.object(gui, 'list_output_devices', return_value=[])):
                window = gui.MainWindow()
            kiwi = FakeKiwi(password="test password%23")
            try:
                window.show()
                app.processEvents()
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())],
                                 ["Listen", "Audio", "DSP", "Automation", "Data", "FLAMP", "Log"])
                self.assertEqual(window.data_mode.currentText(), "MFSK32")
                self.assertEqual(window.data_sweetspot.value(), 1500)
                self.assertTrue(window.data_squelch.isChecked())
                self.assertEqual(window.data_threshold.value(), 20)
                window.data_mode.setCurrentText("Auto (RSID)")
                self.assertFalse(window.data_sweetspot.isEnabled())
                window.data_mode.setCurrentText("MFSK32")
                window.data_source.setCurrentIndex(1)
                self.assertTrue(window.data_choose_file.isEnabled())
                window.data_source.setCurrentIndex(0)
                window._on_event("data_state", {"session": 1, "active": True, "message": "Decoding MFSK32"})
                window._on_event("data_text", {"session": 1, "text": "HELLO X\bKIWI"})
                self.assertEqual(window.data_text.toPlainText(), "HELLO KIWI")
                window._on_event("data_quality", {"session": 1, "value": 80})
                self.assertEqual(window.data_quality.value(), 80)
                self.assertEqual(window.data_gate.text(), "Open")
                window._on_event("data_text", {"session": 0, "text": "stale"})
                self.assertEqual(window.data_text.toPlainText(), "HELLO KIWI")
                window._on_event("data_state", {"session": 1, "active": False, "message": "Stopped"})
                from kiwiremote.flamp import Transfer
                transfer = Transfer("ABCD", filename="received.txt", size=(32, 2, 16),
                                    blocks={1: b"x" * 16}, received_bytes=16)
                window._on_event("flamp_transfers", [transfer.info()])
                self.assertEqual(window.flamp_table.item(0, 1).text(), "1 / 2")
                self.assertEqual(window.flamp_table.item(0, 2).text(), "2")
                self.assertFalse(window.flamp_save.isEnabled())
                transfer.blocks[2] = b"y" * 16
                transfer.payload = b"x" * 16 + b"y" * 16
                window._on_event("flamp_transfers", [transfer.info()])
                self.assertTrue(window.flamp_save.isEnabled())
                self.assertLessEqual(window.width(), 680)
                self.assertLessEqual(window.height(), 600)
                self.assertGreater(app.palette().color(QPalette.Window).lightness(), 230)
                window.tabs.setCurrentIndex(1)
                app.processEvents()
                viewport = window.tabs.widget(1).viewport()
                self.assertTrue(viewport.isAncestorOf(window.rec_btn))
                self.assertLess(window.rec_btn.mapTo(viewport, window.rec_btn.rect().bottomRight()).y(), viewport.height())
                window.tabs.setCurrentIndex(0)
                self.assertTrue(window.quick_tune_box.isHidden())
                presets = Path(directory) / "quicktune.txt"
                presets.write_text("# Presets\nInvalid, inf\nA long preset name, 7101.125\n"
                                   + "Other, 7074\n" * 16, encoding="utf-8")
                window._tick()
                self.assertEqual(sum(not b.isHidden() for b in window.quick_tune_buttons), 15)
                self.assertEqual(window.quick_tune_buttons[0].text(), "A long pre")
                window.mode_buttons["AMN"].click()
                window.quick_tune_buttons[0].click()
                self.assertEqual(window.radio.tune.freq_hz, 7_101_125)
                self.assertEqual(window.radio.tune.mode, "AMN")
                presets.write_text("New, 7200\n", encoding="utf-8")
                window._tick()
                self.assertEqual(window.quick_tune_buttons[0].text(), "New")
                self.assertTrue(window.quick_tune_buttons[1].isHidden())
                presets.unlink()
                window._tick()
                self.assertTrue(window.quick_tune_box.isHidden())

                window.nb_gate.setValue(300)
                window.nb_threshold.setValue(40)
                window.nb_enabled.setChecked(True)
                self.assertEqual(window.radio.noise_blanker, (True, 300, 40))
                window.nb_enabled.setChecked(False)
                if gui.HAVE_SCIPY:
                    window.bandpass_center.setValue(1200)
                    window.bandpass_width.setValue(800)
                    window.bandpass_enabled.setChecked(True)
                    self.assertEqual(window.radio.audio_filters[0], (True, 1200, 800))
                    window.bandpass_enabled.setChecked(False)
                    window.notch_frequency.setValue(750)
                    window.notch_enabled.setChecked(True)
                    self.assertEqual(window.radio.audio_filters[1], (True, 750, 50))
                    window.notch_enabled.setChecked(False)
                window.auto_notch.setChecked(True)
                self.assertEqual(window.radio.noise_filters, (False, True))
                window.noise_reduction.setChecked(True)
                self.assertEqual(window.radio.noise_filters, (True, True))
                logging.getLogger("kiwiremote.test").warning("Test log export")
                exported = Path(directory) / "export.log"
                with patch.object(gui.QFileDialog, "getSaveFileName", return_value=(str(exported), "")):
                    window._export_log()
                self.assertIn("Test log export", exported.read_text(encoding="utf-8"))
                window.server_combo.setCurrentText('127.0.0.1:%d' % kiwi.port)
                window.password.setText("test password%23")
                window.connect_btn.click()
                deadline = time.monotonic() + 5
                while not window.radio.connected and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(window.radio.connected)
                self.assertIn("SET auth t=kiwi p=test%20password%2523 ipl=test%20password%2523", kiwi.commands)
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

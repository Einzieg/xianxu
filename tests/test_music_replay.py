"""Exercise repeated GUI starts against a DD DLL that permits initialization only once."""

import time
import unittest
from unittest.mock import Mock, patch

import customtkinter as ctk

from main import DDInputBackend, USER32
from music_player import MusicPlayerPanel
from music_score import DEFAULT_MAPPING


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.root = ctk.CTk()
        self.root.withdraw()
        self.dll = Mock()
        initialized = False

        def initialize(button):
            nonlocal initialized
            self.assertEqual(button, 0)
            if initialized:
                return 0
            initialized = True
            return 1

        self.dll.DD_btn.side_effect = initialize
        self.dll.DD_todc.side_effect = lambda vk: vk
        self.dll.DD_key.return_value = 1
        self.dll_patch = patch("main.ctypes.WinDLL", return_value=self.dll)
        self.dll_patch.start()
        self.sleep_patch = patch("main.time.sleep")
        self.sleep_patch.start()
        self.factory = Mock(side_effect=lambda mode: DDInputBackend())
        self.panel = MusicPlayerPanel(self.root, self.factory, USER32, listen=False)
        self.panel._fill_mapping(DEFAULT_MAPPING)
        self.panel.target = Mock()
        self.panel.target.state.return_value = "ready"
        self.panel._set_text("3:1/4", "Replay regression")
        for name, value in {"delay": "0", "bpm": "240", "speed": "1", "transpose": "0", "octave": "4"}.items():
            self.panel.fields[name].delete(0, "end")
            self.panel.fields[name].insert(0, value)

    def tearDown(self):
        self.panel.player.stop()
        self.panel.closed = True
        for timer in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", timer)
        self.root.destroy()
        self.sleep_patch.stop()
        self.dll_patch.stop()

    def finish_playback(self):
        deadline = time.monotonic() + 2
        while self.panel.player.running and time.monotonic() < deadline:
            self.panel.player._thread.join(0.02)
        self.assertFalse(self.panel.player.running)
        self.assertEqual(self.panel.player.snapshot().state, "finished", self.panel.status.cget("text"))
        self.assertFalse(self.panel.player.snapshot().pressed)

    def test_finished_performance_can_restart_three_times(self):
        for iteration in range(3):
            self.panel.start()
            self.finish_playback()
            self.assertEqual(self.dll.DD_key.call_count, (iteration + 1) * 2)
        self.factory.assert_called_once_with("DD 驱动")
        self.dll.DD_btn.assert_called_once_with(0)

    def test_preview_between_performances_keeps_driver_session(self):
        self.panel.start()
        self.finish_playback()
        self.panel.start(preview=True)
        self.finish_playback()
        self.panel.start()
        self.finish_playback()
        self.assertEqual(self.dll.DD_key.call_count, 4)
        self.dll.DD_btn.assert_called_once_with(0)

    def test_calibration_and_performance_share_driver(self):
        def measured(keys, sink, target, output_dir, stop, device, progress):
            sink.key_down("F")
            sink.key_up("F")
            return []

        self.panel.start()
        self.finish_playback()
        with patch("music_audio.calibrate", side_effect=measured):
            self.panel.start_calibration()
            self.panel.job.join(2)
        self.assertFalse(self.panel.job.is_alive())
        self.panel.after_cancel(self.panel.poll_timer)
        self.panel._poll()
        self.panel.start()
        self.finish_playback()
        self.assertEqual(self.dll.DD_key.call_count, 6)
        self.factory.assert_called_once_with("DD 驱动")
        self.dll.DD_btn.assert_called_once_with(0)

    def test_stopped_performance_can_restart(self):
        self.panel.start()
        self.assertTrue(self.panel.player.stop())
        self.panel.start()
        self.finish_playback()
        self.assertFalse(self.panel.player._sink.held)
        self.dll.DD_btn.assert_called_once_with(0)

    def test_mode_switch_reuses_cached_dd_instance(self):
        first = self.panel.get_backend()
        self.panel.mode.set("Win32 兼容")
        self.assertIsNot(self.panel.get_backend(), first)
        self.panel.mode.set("DD 驱动")
        self.assertIs(self.panel.get_backend(), first)
        self.assertEqual(self.factory.call_count, 2)

    def test_adaptation_preview_apply_and_restore_never_sends_input(self):
        original = self.panel.current_score()
        self.panel.analyze_adaptation()
        self.panel.job.join(2)
        self.assertFalse(self.panel.job.is_alive())
        self.panel.after_cancel(self.panel.poll_timer)
        self.panel._poll()
        self.assertIsNotNone(self.panel.pending_adaptation)
        self.assertEqual(self.panel.current_score(), original)
        self.panel.apply_adaptation()
        self.assertEqual(self.panel.make_plan().skipped, 0)
        self.assertIs(self.panel.current_score(), self.panel.pending_adaptation.score)
        self.panel.restore_adaptation_source()
        self.assertEqual(self.panel.current_score(), original)
        self.factory.assert_not_called()

    def test_mapping_change_invalidates_adaptation(self):
        from music_adapt import adapt_score

        self.panel._show_adaptation(adapt_score(self.panel.current_score(), DEFAULT_MAPPING), DEFAULT_MAPPING)
        pitch_entry = self.panel.mapping_rows[0][0]
        pitch_entry.delete(0, "end")
        pitch_entry.insert(0, "A2")
        self.panel.apply_adaptation()
        self.assertTrue(self.panel.text_mode)
        self.assertIn("键位已改变", self.panel.status.cget("text"))
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

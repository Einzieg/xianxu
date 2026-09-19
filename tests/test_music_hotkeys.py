import threading
import ctypes
import sys
import unittest
from unittest.mock import Mock

from music_hotkeys import TransportHotkeys


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.down = set()
        self.events = []
        self.read = []
        def key_state(vk):
            self.read.append(vk)
            return vk in self.down
        self.keys = TransportHotkeys(self.events.append, read_key=key_state, report=lambda _: None)
        self.keys.poll()

    def test_only_transport_keys_are_read(self):
        self.assertEqual(set(self.read), {0x77, 0x78, 0x79})

    def test_hold_fires_once_and_release_allows_next_press(self):
        self.down.add(0x78)
        for _ in range(4):
            self.keys.poll()
        self.assertEqual(self.events, ["pause"])
        self.down.clear()
        self.keys.poll()
        self.down.add(0x78)
        self.keys.poll()
        self.assertEqual(self.events, ["pause", "pause"])

    def test_stop_wins_over_simultaneous_play_and_pause(self):
        self.down.update((0x77, 0x78, 0x79))
        self.keys.poll()
        self.assertEqual(self.events, ["stop"])
        self.down.remove(0x79)
        self.keys.poll()
        self.assertEqual(self.events, ["stop"])

    def test_startup_held_key_does_not_start_playback(self):
        events = []
        keys = TransportHotkeys(events.append, read_key=lambda _: True)
        keys.poll()
        keys.poll()
        self.assertEqual(events, [])

    def test_stopped_monitor_cannot_dispatch_again(self):
        self.keys.start()
        self.keys.stop()
        self.down.add(0x77)
        threading.Event().wait(.04)
        self.assertEqual(self.events, [])
        self.assertFalse(self.keys.thread.is_alive())

    def test_raw_key_works_when_async_state_is_always_zero(self):
        self.keys.raw_event(0x79, True, 1)
        self.assertEqual(self.events, ["stop"])
        self.assertEqual(self.down, set())

    def test_raw_autorepeat_does_not_toggle_pause_repeatedly(self):
        for _ in range(5):
            self.keys.raw_event(0x78, True, 1)
        self.assertEqual(self.events, ["pause"])
        self.keys.raw_event(0x78, False, 1)
        self.keys.raw_event(0x78, True, 1)
        self.assertEqual(self.events, ["pause", "pause"])

    def test_raw_stop_held_blocks_play_and_pause(self):
        self.keys.raw_event(0x79, True, 1)
        self.keys.raw_event(0x77, True, 1)
        self.keys.raw_event(0x78, True, 1)
        self.keys.raw_event(0x79, False, 1)
        self.assertEqual(self.events, ["stop"])

    def test_raw_multiple_devices_do_not_duplicate_action(self):
        self.keys.raw_event(0x78, True, 1)
        self.keys.raw_event(0x78, True, 2)
        self.keys.raw_event(0x78, False, 1)
        self.keys.raw_event(0x78, True, 1)
        self.assertEqual(self.events, ["pause"])
        self.keys.device_removed(1)
        self.keys.device_removed(2)
        self.keys.raw_event(0x78, True, 3)
        self.assertEqual(self.events, ["pause", "pause"])

    def test_raw_ignores_unrelated_keys_and_events_after_stop(self):
        self.keys.raw_event(0x41, True, 1)
        self.assertEqual(self.keys.raw_held, set())
        self.keys.stop()
        self.keys.raw_event(0x79, True, 1)
        self.assertEqual(self.events, [])

    def test_raw_startup_held_key_requires_release(self):
        self.down.add(0x78)
        self.keys.held = None
        self.keys.poll()
        self.keys.startup_held = set(self.keys.held)
        self.keys.raw_event(0x78, True, 1)
        self.assertEqual(self.events, [])
        self.keys.raw_event(0x78, False, 1)
        self.keys.raw_event(0x78, True, 1)
        self.assertEqual(self.events, ["pause"])

    def test_failed_registration_has_visible_polling_fallback(self):
        raw = Mock()
        raw.start.side_effect = OSError("registration failed")
        self.keys.raw_factory = Mock(return_value=raw)
        self.keys.report = Mock()
        self.keys.start()
        try:
            self.assertEqual(self.keys.mode, "polling")
            self.assertIsNone(self.keys.raw)
            raw.stop.assert_called_once()
            self.assertIn("registration failed", self.keys.report.call_args.args[0])
        finally:
            self.keys.stop()


@unittest.skipUnless(sys.platform == "win32", "Win32 Raw Input structures")
class RawPacketTests(unittest.TestCase):
    def packet(self, vk, flags=0, kind=1):
        from music_raw_input import RawInputHeader, RawKeyboard
        header = RawInputHeader(kind, ctypes.sizeof(RawInputHeader) + ctypes.sizeof(RawKeyboard), 123, 0)
        return bytes(header) + bytes(RawKeyboard(0x44, flags, 0, vk, 0x100, 0))

    def test_packet_decodes_only_transport_make_and_break(self):
        from music_raw_input import decode_transport_packet
        self.assertEqual(decode_transport_packet(self.packet(0x79)), (0x79, True, 123))
        self.assertEqual(decode_transport_packet(self.packet(0x79, 1)), (0x79, False, 123))
        self.assertIsNone(decode_transport_packet(self.packet(0x41)))
        self.assertIsNone(decode_transport_packet(self.packet(0x79, kind=0)))
        self.assertIsNone(decode_transport_packet(self.packet(0x79)[:-1]))
        self.assertIsNone(decode_transport_packet(b""))

    def test_native_structure_sizes(self):
        from music_raw_input import RawInputDevice, RawInputHeader, RawKeyboard
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(RawInputDevice), 8 + pointer_size)
        self.assertEqual(ctypes.sizeof(RawInputHeader), 8 + 2 * pointer_size)
        self.assertEqual(ctypes.sizeof(RawKeyboard), 16)


if __name__ == "__main__":
    unittest.main()

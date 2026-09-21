import unittest
import ctypes
from ctypes import wintypes
from pathlib import Path
from unittest.mock import Mock, patch

from dd_init_guard import suppress_start_menu, _WinKeyHook
from main import DDInputBackend, InputBackendError


class GuardTests(unittest.TestCase):
    def test_filters_only_windows_keys(self):
        hook = _WinKeyHook()
        hook.user32 = Mock()
        hook.user32.CallNextHookEx.return_value = 17
        for message in (0x100, 0x101, 0x104, 0x105):
            for vk in (0x5B, 0x5C):
                data = wintypes.DWORD(vk)
                self.assertEqual(hook._filter(0, message, ctypes.addressof(data)), 1)
        hook.user32.CallNextHookEx.assert_not_called()
        for vk in (0x1B, 0x41, 0x79, 0x12):
            data = wintypes.DWORD(vk)
            self.assertEqual(hook._filter(0, 0x100, ctypes.addressof(data)), 17)
        self.assertEqual(hook._filter(-1, 0, 0), 17)

    def test_always_stops(self):
        for fail in (False, True):
            with self.subTest(fail=fail), patch('dd_init_guard._WinKeyHook') as factory, \
                    patch('dd_init_guard.threading.Event'):
                listener = factory.return_value
                try:
                    with suppress_start_menu():
                        listener.start.assert_called_once()
                        if fail:
                            raise RuntimeError('initialization failed')
                except RuntimeError:
                    if not fail:
                        raise
                listener.stop.assert_called_once()

    def test_ready_only_after_hook_installed_and_unhooks(self):
        api, kernel = Mock(), Mock()
        api.SetWindowsHookExW.return_value = 123
        api.GetMessageW.return_value = 0
        hook = _WinKeyHook()
        hook.ready = Mock()
        hook.ready.set.side_effect = lambda: api.SetWindowsHookExW.assert_called_once()
        with patch('dd_init_guard.ctypes.WinDLL', side_effect=[api, kernel]):
            hook._run()
        self.assertIsNone(hook.error)
        api.UnhookWindowsHookEx.assert_called_once_with(123)

    def test_guard_covers_load_retry_and_failure_but_not_reuse(self):
        for result in (1, 0):
            with self.subTest(result=result), patch('main.suppress_start_menu') as guard, \
                    patch('main.ctypes.WinDLL') as load, patch('main.time.sleep'):
                active = []
                guard.return_value.__enter__.side_effect = lambda: active.append(True)
                guard.return_value.__exit__.side_effect = lambda *args: active.pop() and False
                dll = Mock()
                def initialize(_):
                    self.assertTrue(active)
                    return result
                def load_dll(_):
                    self.assertTrue(active)
                    return dll
                load.side_effect = load_dll
                dll.DD_btn.side_effect = initialize
                backend = DDInputBackend()
                backend.candidate_path = Mock(return_value=Path('fake.dll'))
                if result:
                    backend.ensure_ready()
                    backend.ensure_ready()
                    guard.assert_called_once()
                    dll.DD_btn.assert_called_once_with(0)
                else:
                    with self.assertRaises(InputBackendError):
                        backend.ensure_ready()
                    self.assertIsNone(backend.dll)
                self.assertFalse(active)
                dll.DD_key.assert_not_called()


if __name__ == '__main__':
    unittest.main()

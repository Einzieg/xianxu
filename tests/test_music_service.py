"""No-input integration tests for library, queue, jobs, and JSON-lines IPC."""

import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from music_library import PlaylistNavigator, ScoreLibrary, decode_score, mapping_from_settings
from music_score import Note, Score, compile_score
from music_service import MusicService, ROOT


class NavigationTests(unittest.TestCase):
    def test_order_stops_and_repeat_all_wraps(self):
        nav = PlaylistNavigator()
        self.assertEqual(nav.next(["a", "b"], "a", "order"), "b")
        self.assertIsNone(nav.next(["a", "b"], "b", "order"))
        self.assertEqual(nav.next(["a", "b"], "b", "repeat_all"), "a")

    def test_repeat_one_only_repeats_on_natural_completion(self):
        nav = PlaylistNavigator()
        self.assertEqual(nav.next(["a", "b"], "a", "repeat_one"), "a")
        self.assertEqual(nav.next(["a", "b"], "a", "repeat_one", automatic=False), "b")

    def test_shuffle_bag_visits_other_tracks_before_repeating(self):
        nav = PlaylistNavigator(random.Random(42))
        current = "a"
        seen = [current]
        for _ in range(3):
            current = nav.next(["a", "b", "c", "d"], current, "random")
            seen.append(current)
        self.assertEqual(len(set(seen)), 4)
        self.assertEqual(nav.previous(["a", "b", "c", "d"], current, "random"), seen[-2])
        self.assertEqual(nav.next(["only"], "only", "random"), "only")
        self.assertIsNone(nav.next([], None, "order"))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = Mock()
        self.backend.prepare_keyboard_action.side_effect = lambda vk, key: key
        self.backend.press_keyboard_action.return_value = True
        self.backend.release_keyboard_action.return_value = True
        self.factory = Mock(return_value=self.backend)
        self.service = MusicService(self.temp.name, backend_factory=self.factory, monitor=False)
        self.service.handle("set_settings", {"delay": 0})
        self.service.target = Mock(title="Sandbox game")
        self.service.target.state.return_value = "ready"
        self.a = self.add("First")
        self.b = self.add("Second")
        self.service.handle("select", {"id": self.a})

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def add(self, title):
        score = Score(title, (Note(0, 0.025, 60),), 0.035)
        return self.service.library.add(self.service.library.arrange(score, "fixture"))["id"]

    def finish(self):
        self.service.player._thread.join(2)
        self.assertFalse(self.service.player.running)

    def wait_state(self, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.service.player.snapshot().state == state:
                return
            time.sleep(.005)
        self.fail(f"Expected {state}, got {self.service.player.snapshot()}")

    def start_background_session(self):
        source = Score("Resume", (Note(0, .1, 60), Note(.3, .1, 62)), .5)
        item = self.service.library.arrange(source, "fixture")
        self.service.library.add(item)
        self.service.target.state.return_value = "background"
        self.service.handle("play", {"id": item["id"]})
        self.wait_state("paused")

    def test_explicit_pause_is_idempotent_after_focus_loss(self):
        self.start_background_session()
        self.service.handle("pause", {"paused": True})
        self.service.target.state.return_value = "ready"
        time.sleep(.04)
        self.assertEqual(self.service.player.snapshot().state, "paused")
        self.backend.press_keyboard_action.assert_not_called()

    def test_focus_handoff_uses_actual_engine_pid_without_input(self):
        self.assertEqual(self.service.handle("get_process_id"), {"pid": os.getpid()})
        self.factory.assert_not_called()

    def test_gui_resume_refocuses_then_continues_same_session(self):
        self.start_background_session()
        worker = self.service.player._thread
        item_id = self.service.library.current_id
        def activate():
            self.service.target.state.return_value = "ready"
            return True
        self.service.target.activate.side_effect = activate
        self.service.handle("resume", {"activate_target": True})
        self.finish()
        self.assertIs(self.service.player._thread, worker)
        self.assertEqual(self.service.library.current_id, item_id)
        self.assertEqual(self.service.player.snapshot().state, "finished")
        self.assertEqual(self.backend.press_keyboard_action.call_count, 2)
        self.assertEqual(self.backend.release_keyboard_action.call_count, 2)
        self.factory.assert_called_once()
        self.service.target.activate.assert_called_once()

    def test_failed_resume_activation_leaves_paused_and_sends_nothing(self):
        self.start_background_session()
        self.service.target.activate.return_value = True  # Result alone is not proof of focus.
        with self.assertRaisesRegex(ValueError, "无法切换"):
            self.service.handle("resume", {"activate_target": True})
        self.assertEqual(self.service.player.snapshot().state, "paused")
        self.backend.press_keyboard_action.assert_not_called()

    def test_closed_resume_target_is_not_activated(self):
        self.start_background_session()
        self.service.target.state.return_value = "closed"
        with self.assertRaisesRegex(ValueError, "重新捕获"):
            self.service.handle("resume", {"activate_target": True})
        self.service.target.activate.assert_not_called()
        self.backend.press_keyboard_action.assert_not_called()

    def test_stop_during_focus_handoff_cannot_resume(self):
        self.start_background_session()
        def stop_during_activate():
            self.service.abort_queue.set()
            self.service.player.request_stop()
            self.service.target.state.return_value = "ready"
        self.service.target.activate.side_effect = stop_during_activate
        self.service.handle("resume", {"activate_target": True})
        self.finish()
        self.service._tick()
        self.assertEqual(self.service.player.snapshot().state, "stopped")
        self.assertFalse(self.service.queue_active)
        self.backend.press_keyboard_action.assert_not_called()

    def test_delayed_play_after_window_resize_cannot_override_stop(self):
        self.service.handle("stop")
        with self.assertRaisesRegex(ValueError, "停止操作"):
            self.service.handle("play", {"activate_target": True, "stop_sequence": 0})
        self.service.target.activate.assert_not_called()
        self.factory.assert_not_called()

    def test_gui_play_checks_focus_before_starting(self):
        self.service.target.state.return_value = "background"
        self.service.target.activate.side_effect = lambda: setattr(self.service.target.state, "return_value", "ready")
        self.service.handle("play", {"activate_target": True})
        self.finish()
        self.assertEqual(self.service.player.snapshot().state, "finished")
        self.service.target.activate.assert_called_once()
        self.assertEqual(self.backend.press_keyboard_action.call_count, 1)

    def test_delayed_foreground_activation_is_confirmed_before_input(self):
        self.service.target.state.return_value = "background"
        def activate():
            self.backend.press_keyboard_action.assert_not_called()
            timer = threading.Timer(.04, lambda: setattr(self.service.target.state, "return_value", "ready"))
            timer.start()
            return False
        self.service.target.activate.side_effect = activate
        self.service.handle("play", {"activate_target": True})
        self.finish()
        self.assertEqual(self.service.player.snapshot().state, "finished")
        self.backend.press_keyboard_action.assert_called_once()

    def test_stop_interrupts_foreground_confirmation_wait_without_input(self):
        self.service.target.state.return_value = "background"
        def activate():
            timer = threading.Timer(.02, self.service.abort_queue.set)
            timer.start()
            return False
        self.service.target.activate.side_effect = activate
        self.service.handle("play", {"activate_target": True})
        self.assertFalse(self.service.player.running)
        self.backend.press_keyboard_action.assert_not_called()

    def test_failed_play_focus_handoff_does_not_start_worker(self):
        self.service.target.state.return_value = "background"
        with self.assertRaisesRegex(ValueError, "无法切换"):
            self.service.handle("play", {"activate_target": True})
        self.assertFalse(self.service.player.running)
        self.assertFalse(self.service.queue_active)
        self.backend.press_keyboard_action.assert_not_called()

    def test_preview_resume_never_activates_a_target_or_initializes_dd(self):
        self.service.handle("set_settings", {"speed": .25})
        self.service.handle("play", {"preview": True, "activate_target": True})
        self.service.handle("pause", {"paused": True})
        self.wait_state("paused")
        self.service.handle("resume", {"activate_target": True})
        self.finish()
        self.service.target.activate.assert_not_called()
        self.factory.assert_not_called()

    def test_hotkey_resume_requires_existing_game_focus(self):
        self.start_background_session()
        with self.assertRaisesRegex(ValueError, "切回游戏"):
            self.service.handle("pause")
        self.service.target.activate.assert_not_called()
        self.service.target.state.return_value = "ready"
        self.service.handle("pause")
        self.finish()
        self.assertEqual(self.service.player.snapshot().state, "finished")

    def test_hotkey_stop_latches_without_waiting_for_service_lock(self):
        with patch("music_hotkeys.TransportHotkeys") as listener:
            self.service.listen_hotkeys()
        pressed = listener.call_args.args[0]
        sink = Mock()
        score = Score("Stop", (Note(0, 1, 60),), 2)
        self.service.player.start(compile_score(score, {60: "1"}, hold_ms=150), sink, delay=0)
        self.service.queue_active = True
        deadline = time.monotonic() + 1
        while not self.service.player.snapshot().pressed and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertTrue(self.service.player.snapshot().pressed)
        with self.service.lock:
            receiver = threading.Thread(target=lambda: pressed("stop"))
            receiver.start()
            receiver.join(.5)
            self.assertFalse(receiver.is_alive())
            self.assertTrue(self.service.abort_queue.is_set())
            self.assertTrue(self.service.job_stop.is_set())
            self.finish()
        self.assertEqual(self.service.player.snapshot().state, "stopped")
        self.assertFalse(self.service.player.snapshot().pressed)
        sink.key_up.assert_called_once_with("1")
        self.service.handle(*self.service.actions.get_nowait())
        self.assertFalse(self.service.queue_active)
        self.assertEqual(self.service.stop_sequence, 1)

    def test_natural_finish_advances_order_and_stops_at_end(self):
        self.service.handle("play", {"preview": True})
        self.finish()
        self.service._tick()
        self.assertEqual(self.service.library.current_id, self.b)
        self.finish()
        self.service._tick()
        self.assertFalse(self.service.queue_active)
        self.factory.assert_not_called()

    def test_stop_during_repeat_never_restarts(self):
        self.service.handle("set_mode", {"mode": "repeat_one"})
        self.service.handle("play", {"preview": True})
        self.service.handle("stop")
        for _ in range(3):
            self.service._tick()
        self.assertFalse(self.service.player.running)
        self.assertFalse(self.service.queue_active)
        self.assertEqual(self.service.library.current_id, self.a)

    def test_backend_is_reused_across_tracks_and_restarts(self):
        for _ in range(3):
            self.service.handle("play", {"id": self.a, "preview": False})
            self.finish()
            self.service.handle("stop")
        self.factory.assert_called_once_with("DD")
        self.assertEqual(self.backend.press_keyboard_action.call_count, 3)
        self.assertEqual(self.backend.release_keyboard_action.call_count, 3)

    def test_backend_error_does_not_advance_queue(self):
        self.backend.press_keyboard_action.return_value = False
        self.service.handle("play", {"preview": False})
        self.finish()
        self.service._tick()
        self.assertFalse(self.service.queue_active)
        self.assertEqual(self.service.library.current_id, self.a)
        self.assertEqual(self.service.player.snapshot().state, "error")
        self.assertFalse(self.service.player.snapshot().pressed)

    def test_emergency_stop_during_backend_initialization_cannot_be_cleared_by_start(self):
        self.backend.ensure_ready.side_effect = self.service.abort_queue.set
        self.service.handle("play", {"preview": False})
        self.service._tick()
        self.assertFalse(self.service.player.running)
        self.assertFalse(self.service.queue_active)
        self.backend.press_keyboard_action.assert_not_called()

    def test_focus_loss_between_tracks_blocks_autoplay(self):
        self.service.handle("play", {"preview": False})
        self.finish()
        self.service.target.state.return_value = "background"
        self.service._tick()
        self.assertFalse(self.service.queue_active)
        self.assertEqual(self.backend.press_keyboard_action.call_count, 1)

    def test_paused_track_never_advances(self):
        self.service.target.state.return_value = "background"
        self.service.handle("play", {"preview": False})
        threading.Event().wait(0.04)
        self.service._tick()
        self.assertEqual(self.service.player.snapshot().state, "paused")
        self.assertEqual(self.service.library.current_id, self.a)
        self.backend.press_keyboard_action.assert_not_called()

    def test_play_requires_target_and_validated_settings(self):
        self.service.target = None
        with self.assertRaisesRegex(ValueError, "捕获"):
            self.service.handle("play")
        for field, value in (("speed", float("nan")), ("hold", 500), ("octave", 2.5)):
            with self.assertRaises(ValueError):
                self.service.handle("set_settings", {field: value})
        self.factory.assert_not_called()

    def test_preview_preference_is_shared_with_hotkey_state(self):
        self.service.handle("set_preview", {"preview": True})
        self.assertTrue(self.service.handle("get_state")["preview"])
        with self.assertRaises(ValueError):
            self.service.handle("set_preview", {"preview": "false"})

    def test_audio_audition_and_stop_sequence_never_initialize_driver(self):
        result = self.service.handle("audition", {"id": self.a, "kind": "original"})
        self.assertTrue(result["data_url"].startswith("data:audio/wav;base64,"))
        before = self.service.handle("get_state")["stop_sequence"]
        self.service.handle("stop")
        self.assertEqual(self.service.handle("get_state")["stop_sequence"], before + 1)
        self.factory.assert_not_called()

    def test_default_transcription_selects_neural_engine_and_stores_provenance(self):
        source = Score("Model", (Note(0, 0.2, 72),), 0.2)
        path = Path(self.temp.name) / "fixture.wav"
        path.touch()
        with patch("music_service.transcribe_audio", return_value=source) as transcribe:
            self.service.handle("transcribe", {"path": str(path), "register": "high"})
            self.service.job_thread.join(2)
        self.assertEqual(transcribe.call_args.kwargs["engine"], "instrument")
        self.assertEqual(transcribe.call_args.kwargs["register"], "high")
        self.assertEqual(transcribe.call_args.kwargs["texture"], "melody")
        item = self.service.library.get(self.service.library.current_id)
        from music_neural import TRANSCRIPTION_REVISION
        self.assertEqual(item["transcription"], {"engine": "instrument", "register": "high", "texture": "melody",
                                               "revision": TRANSCRIPTION_REVISION})
        restored = ScoreLibrary(self.temp.name)
        self.assertEqual(restored.describe(restored.get(item["id"]))["transcription"], item["transcription"])

    def test_chord_transcription_persists_polyphony_and_survives_mapping_change(self):
        source = Score("Model chords", tuple(Note(0, .5, p) for p in (52, 55, 59)), 1)
        path = Path(self.temp.name) / "chords.wav"
        path.touch()
        with patch("music_service.transcribe_audio", return_value=source) as transcribe:
            self.service.handle("transcribe", {"path": str(path), "texture": "chords"})
            self.service.job_thread.join(2)
        self.assertEqual(self.service.job["state"], "done")
        self.assertEqual(transcribe.call_args.kwargs["texture"], "chords")
        item = self.service.library.get(self.service.library.current_id)
        self.assertEqual(item["transcription"]["revision"], "chords-v1")
        self.assertEqual(item["texture"], "chords")
        self.assertEqual(len(decode_score(item["score"]).notes), 3)
        rows = [{"pitch": row["pitch"] + 12, "key": row["key"]}
                for row in self.service.library.settings["mapping"]]
        self.service.handle("set_settings", {"mapping": rows})
        detail = self.service.handle("get_score", {"id": item["id"]})
        self.assertEqual(len(detail["notes"]), 3)
        self.assertEqual(len({n["pitch"] for n in detail["notes"]}), 3)
        restored = ScoreLibrary(self.temp.name).get(item["id"])
        self.assertEqual(restored["texture"], "chords")
        self.assertEqual(restored["transcription"]["texture"], "chords")
        self.factory.assert_not_called()

    def test_text_chords_remain_simultaneous_through_import_and_mock_playback(self):
        item = self.service.handle("add_text", {"title": "Triad", "text": "[3 5 7]"})
        record = self.service.library.get(item["id"])
        self.assertEqual(len(decode_score(record["score"]).notes), 3)
        self.service.handle("play", {"id": item["id"], "preview": False})
        self.finish()
        self.assertEqual(self.backend.press_keyboard_action.call_count, 3)
        self.assertEqual(self.backend.release_keyboard_action.call_count, 3)
        self.assertFalse(self.service.player.snapshot().pressed)

    def test_non_model_chord_mode_and_unknown_texture_rejected_before_job(self):
        for extra in ({"engine": "yin", "texture": "chords"},
                      {"engine": "melody", "texture": "chords"}, {"texture": "invalid"}):
            with self.assertRaisesRegex(ValueError, "和弦"):
                self.service.handle("transcribe", {"path": "absent.wav", **extra})
        self.factory.assert_not_called()

    def test_calibration_release_failure_blocks_restart_until_stop_retries(self):
        sink = Mock()
        sink.release_all.side_effect = [RuntimeError("release failed"), None]
        self.service.calibration_sink = sink
        with self.assertRaisesRegex(ValueError, "未释放"):
            self.service.handle("play", {"preview": False})
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            self.service.handle("stop")
        self.assertIs(self.service.calibration_sink, sink)
        self.service.handle("stop")
        self.assertIsNone(self.service.calibration_sink)
        self.factory.assert_not_called()

    def test_library_persists_original_and_adapted_notes(self):
        item = self.service.handle("add_text", {"title": "High", "text": "1'' 3'' 5''"})
        restored = ScoreLibrary(self.temp.name)
        record = restored.get(item["id"])
        self.assertEqual(len(decode_score(record["original"]).notes), 3)
        plan = compile_score(decode_score(record["score"]), mapping_from_settings(restored.settings))
        self.assertEqual(plan.skipped, 0)
        self.assertEqual(restored.current_id, item["id"])

    def test_remove_noncurrent_persists_and_keeps_source_file(self):
        source = Path(self.temp.name) / "original.txt"
        source.write_text("1 2 3", encoding="utf-8")
        self.service.library.get(self.b)["source"] = str(source)
        self.service.library.save()
        self.service.navigator.bag = [self.b]
        self.service.navigator.history = [self.b]
        state = self.service.handle("remove", {"id": self.b})
        self.assertEqual([item["id"] for item in state["library"]], [self.a])
        self.assertEqual(state["current_id"], self.a)
        self.assertEqual(source.read_text(encoding="utf-8"), "1 2 3")
        self.assertEqual(self.service.navigator.bag, [])
        self.assertEqual(self.service.navigator.history, [])
        restored = ScoreLibrary(self.temp.name)
        self.assertEqual([item["id"] for item in restored.items], [self.a])
        self.assertEqual(restored.current_id, self.a)
        self.factory.assert_not_called()

    def test_remove_current_then_last_clears_selection_and_queue(self):
        state = self.service.handle("remove", {"id": self.a})
        self.assertEqual(state["current_id"], self.b)
        state = self.service.handle("remove", {"id": self.b})
        self.assertEqual(state["library"], [])
        self.assertIsNone(state["current_id"])
        self.assertFalse(state["queue_active"])
        self.service._tick()
        restored = ScoreLibrary(self.temp.name)
        self.assertEqual(restored.items, [])
        self.assertIsNone(restored.current_id)
        self.factory.assert_not_called()

    def test_remove_duplicate_title_uses_id_and_missing_id_is_nonmutating(self):
        duplicate = self.add("First")
        self.service.handle("remove", {"id": self.a})
        self.assertEqual(self.service.library.get(duplicate)["title"], "First")
        before = self.service.library.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.service.handle("remove", {"id": self.a})
        self.assertEqual(self.service.library.path.read_bytes(), before)
        self.assertEqual(self.service.library.current_id, duplicate)

    def test_remove_save_failure_rolls_back_memory_selection_and_navigation(self):
        before = self.service.library.path.read_bytes()
        self.service.navigator.bag = [self.b]
        self.service.navigator.history = [self.a]
        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                self.service.handle("remove", {"id": self.a})
        self.assertEqual(self.service.library.path.read_bytes(), before)
        self.assertEqual([item["id"] for item in self.service.library.items], [self.a, self.b])
        self.assertEqual(self.service.library.current_id, self.a)
        self.assertEqual(self.service.navigator.bag, [self.b])
        self.assertEqual(self.service.navigator.history, [self.a])
        self.service.handle("remove", {"id": self.a})
        self.assertEqual(ScoreLibrary(self.temp.name).current_id, self.b)

    def test_remove_blocked_during_countdown_pause_and_queue_until_stopped(self):
        self.service.handle("set_settings", {"delay": 20})
        self.service.handle("play", {"id": self.a, "preview": True})
        for pause in (False, True):
            if pause:
                self.service.handle("pause")
            with self.subTest(paused=pause), self.assertRaisesRegex(ValueError, "停止演奏"):
                self.service.handle("remove", {"id": self.a})
        self.service.handle("stop")
        self.service.queue_active = True
        with self.assertRaisesRegex(ValueError, "停止演奏"):
            self.service.handle("remove", {"id": self.b})
        self.service.handle("stop")
        self.service.handle("remove", {"id": self.a})
        self.assertFalse(self.service.player.snapshot().pressed)
        self.assertFalse(self.service.queue_active)
        self.factory.assert_not_called()

    def test_remove_blocked_by_background_job_or_unreleased_calibration(self):
        with patch.object(self.service, "_busy", return_value=True):
            with self.assertRaisesRegex(ValueError, "当前任务"):
                self.service.handle("remove", {"id": self.a})
        sink = Mock()
        self.service.calibration_sink = sink
        with self.assertRaisesRegex(ValueError, "未释放"):
            self.service.handle("remove", {"id": self.a})
        self.assertEqual([item["id"] for item in self.service.library.items], [self.a, self.b])
        self.service.handle("stop")
        sink.release_all.assert_called_once()
        self.factory.assert_not_called()

    def test_changed_mapping_readapts_from_original(self):
        rows = [{"pitch": row["pitch"] + 12, "key": row["key"]} for row in self.service.library.settings["mapping"]]
        self.service.handle("set_settings", {"mapping": rows})
        details = self.service.handle("get_score", {"id": self.a})
        self.assertTrue(all(note["pitch"] in {row["pitch"] for row in rows} for note in details["notes"]))
        self.assertEqual(details["original_notes"][0]["pitch"], 60)

    def test_melody_priority_and_explicit_lead_track_survive_mapping_change(self):
        from music_adapt import adapt_score
        source = Score("Priority", (Note(0, .4, 72, 0), Note(.5, .4, 71, 0),
                                    Note(0, .4, 48, 1), Note(.5, .4, 53, 1)), 1,
                       ((0, "Piano right hand"), (1, "Lead-labelled backing")))
        item = self.service.library.arrange(source, "fixture", texture="melody_chords", melody_track=0)
        item_id = self.service.library.add(item)["id"]
        reloaded = ScoreLibrary(self.temp.name).get(item_id)
        self.assertEqual(reloaded["texture"], "melody_chords")
        self.assertEqual(reloaded["melody_track"], 0)
        original = reloaded["original"]
        rows = [{"pitch": row["pitch"] + 12, "key": row["key"]}
                for row in self.service.library.settings["mapping"]]
        self.service.handle("set_settings", {"mapping": rows})
        self.service.handle("get_score", {"id": item_id})
        revised = self.service.library.get(item_id)
        solo = adapt_score(source, mapping_from_settings(self.service.library.settings), track=0)
        locked = tuple(n for n in decode_score(revised["score"]).notes if n.track == 0)
        self.assertEqual(locked, solo.score.notes)
        self.assertEqual(decode_score(revised["original"]), decode_score(original))
        self.assertEqual(revised["melody_track"], 0)
        self.factory.assert_not_called()

    def test_sparse_mode_and_tempo_survive_reload_and_mapping_change_without_source_file(self):
        from music_adapt import adapt_score
        source = Score("Sparse", (Note(0, .9, 64), Note(1, .9, 64),
                                  Note(0, .8, 57, 1), Note(.5, .3, 57, 1), Note(1, .8, 57, 1)), 2)
        config = {"version": 1, "backing_track": 1, "tempo_map": [[0, 0, 1000000]]}
        item = self.service.library.arrange(source, "nonexistent-source.mid", texture="melody_bass",
                                            melody_track=0, sparse_config=config)
        item_id = self.service.library.add(item)["id"]
        saved = ScoreLibrary(self.temp.name).get(item_id)
        self.assertEqual(saved["sparse_config"], config)
        config["tempo_map"][0][2] = 1
        self.assertEqual(item["sparse_config"]["tempo_map"][0][2], 1000000)
        rows = [{"pitch": row["pitch"] + 12, "key": row["key"]}
                for row in self.service.library.settings["mapping"]]
        self.service.handle("set_settings", {"mapping": rows})
        self.service.handle("get_score", {"id": item_id})
        revised = ScoreLibrary(self.temp.name).get(item_id)
        mapping = mapping_from_settings(self.service.library.settings)
        expected = adapt_score(source, mapping, texture="melody_bass", melody_track=0,
                               sparse_config=saved["sparse_config"])
        self.assertEqual(decode_score(revised["score"]), expected.score)
        self.assertEqual(revised["texture"], "melody_bass")
        self.assertEqual(revised["original"], saved["original"])
        self.assertEqual(revised["sparse_config"], saved["sparse_config"])
        self.assertEqual([n.start for n in expected.score.notes if n.track == 1], [0, 1])
        self.factory.assert_not_called()

    def test_readapting_legacy_neural_score_does_not_claim_new_recognition(self):
        record = self.service.library.get(self.a)
        record["transcription"] = {"engine": "instrument", "register": "auto"}
        original = json.dumps(record["original"], sort_keys=True)
        rows = [{"pitch": row["pitch"] + 12, "key": row["key"]}
                for row in self.service.library.settings["mapping"]]
        self.service.handle("set_settings", {"mapping": rows})
        with patch("music_service.transcribe_audio") as transcribe:
            detail = self.service.handle("get_score", {"id": self.a})
            transcribe.assert_not_called()
        self.assertEqual(detail["item"]["transcription"], {"engine": "instrument", "register": "auto"})
        restored = ScoreLibrary(self.temp.name).get(self.a)
        self.assertEqual(json.dumps(restored["original"], sort_keys=True), original)
        self.assertNotIn("revision", restored["transcription"])

    def test_corrupt_library_is_not_overwritten(self):
        path = Path(self.temp.name) / "library.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            ScoreLibrary(self.temp.name)
        self.assertEqual(path.read_text(encoding="utf-8"), "not json")

    def test_transcription_automatically_arranges_and_cancel_does_not_commit(self):
        source = Score("Audio", (Note(0, 0.2, 90), Note(0.3, 0.2, 94)), 0.5)
        path = Path(self.temp.name) / "fixture.wav"
        path.touch()
        with patch("music_service.transcribe_audio", return_value=source) as transcribe:
            self.service.handle("transcribe", {"path": str(path), "engine": "melody"})
            self.service.job_thread.join(2)
            self.assertEqual(self.service.job["state"], "done")
            self.assertEqual(transcribe.call_args.kwargs["engine"], "melody")
        item = self.service.library.get(self.service.library.current_id)
        self.assertEqual(len(decode_score(item["original"]).notes), 2)
        self.assertEqual(compile_score(decode_score(item["score"]), mapping_from_settings(self.service.library.settings)).skipped, 0)
        count = len(self.service.library.items)

        def cancelled(path, stop, **kwargs):
            stop.wait(2)
            return source

        with patch("music_service.transcribe_audio", side_effect=cancelled):
            self.service.handle("transcribe", {"path": str(path)})
            self.service.handle("cancel_job")
            self.service.job_thread.join(3)
        self.assertEqual(self.service.job["state"], "cancelled")
        self.assertEqual(len(self.service.library.items), count)

    def test_json_lines_process_roundtrip_and_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = [{"method": "get_state"}, {"method": "add_text", "params": {"title": "小测试", "text": "3 4 5"}},
                        {"method": "shutdown"}]
            result = subprocess.run([sys.executable, "-u", str(ROOT / "music_service.py"), "--no-hotkeys", "--data-dir", folder],
                                    input="".join(json.dumps(c) + "\n" for c in commands), capture_output=True,
                                    text=True, encoding="utf-8", timeout=15, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(responses), 3)
            self.assertTrue(all(r["ok"] for r in responses), responses)
            self.assertEqual(responses[1]["result"]["title"], "小测试")
            self.assertTrue(responses[-1]["result"]["closed"])


if __name__ == "__main__":
    unittest.main()

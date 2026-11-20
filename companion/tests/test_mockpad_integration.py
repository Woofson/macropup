"""
Integration tests: the real companion code (macropad.py) talking to the mock
pad (mockpad.py) over a virtual serial port. Linux/macOS only.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import macropad  # noqa: E402
from mockpad import MockPad  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(HERE), "macropad.json")


class FakeAudio:
    """Stands in for LinuxAudio/WindowsAudio and records what the daemon asks for."""

    def __init__(self):
        self.lock = threading.Lock()
        self.out = [("sink.a", "Realtek Speakers", True), ("sink.b", "USB Headset", False),
                    ("sink.v", "Virtual Cable", False)]
        self.inp = [("src.a", "Built-in Mic", True)]
        self.vol, self.muted, self.mic_muted = 30, False, False
        self.set_default_calls = []
        self.set_volume_calls = []

    def devices(self, kind):
        with self.lock:
            return list(self.out if kind == "OUT" else self.inp)

    def set_default(self, kind, dev_id):
        with self.lock:
            self.set_default_calls.append((kind, dev_id))
            lst = self.out if kind == "OUT" else self.inp
            lst[:] = [(i, n, i == dev_id) for i, n, _ in lst]

    def set_volume(self, pct):
        with self.lock:
            self.set_volume_calls.append(pct)
            self.vol = pct

    def state(self):
        with self.lock:
            return self.vol, self.muted, self.mic_muted


def wait_until(pred, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class PadLink(unittest.TestCase):
    """The Pad class on its own: probe, upload, hash handling."""

    def setUp(self):
        self.mock = MockPad()
        self.mock.start()
        self.pad = macropad.Pad(self.mock.path)

    def tearDown(self):
        self.pad.close()
        self.mock.stop()

    def test_connect_reports_default_hash(self):
        self.assertTrue(self.pad.connect())
        self.assertEqual(self.pad.hash, "default")

    def test_upload_stores_exact_lines(self):
        _, lines, digest, _ = macropad.load_config(EXAMPLE)
        self.assertTrue(self.pad.connect())
        self.pad.upload(lines)
        self.assertEqual(self.pad.hash, digest)
        self.assertEqual(self.mock.stored_config, lines)
        self.assertEqual(self.mock.hash, digest)
        self.assertEqual(self.mock.layers[0], "Main")
        self.assertEqual(self.mock.keys[(0, 0)][2], "Copy")

    def test_hash_survives_reconnect(self):
        _, lines, digest, _ = macropad.load_config(EXAMPLE)
        self.assertTrue(self.pad.connect())
        self.pad.upload(lines)
        self.pad.close()
        self.assertTrue(self.pad.connect())
        self.assertEqual(self.pad.hash, digest)

    def test_reset(self):
        _, lines, _, _ = macropad.load_config(EXAMPLE)
        self.assertTrue(self.pad.connect())
        self.pad.upload(lines)
        self.pad.send("CFG RESET")
        self.pad.wait_for("OK RESET")
        self.assertIsNone(self.mock.stored_config)
        self.assertEqual(self.mock.hash, "default")

    def test_host_vanishing_mid_upload_keeps_old_config(self):
        _, lines, digest, _ = macropad.load_config(EXAMPLE)
        self.assertTrue(self.pad.connect())
        self.pad.upload(lines)
        self.pad.send("CFG BEGIN")
        self.pad.wait_for("OK BEGIN")
        self.pad.send("C O rot 3")                         # ...and then the host dies
        self.pad.close()
        self.assertTrue(self.pad.connect())                # a new host says HELLO
        self.assertEqual(self.pad.hash, digest)
        self.assertFalse(self.mock.uploading)
        self.assertEqual(self.mock.stored_config, lines)

    def test_no_pad_on_port(self):
        self.mock.stop()
        self.assertFalse(macropad.Pad("/nonexistent/tty").connect())


class DaemonSession(unittest.TestCase):
    """The full `run` daemon against the mock pad, with fake audio."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="padtest-")
        self.cfg_path = os.path.join(self.tmp, "macropad.json")
        shutil.copy(EXAMPLE, self.cfg_path)
        self.link = os.path.join(self.tmp, "port")       # stable name, re-pointed on "replug"
        self.mock = MockPad()
        self.mock.start()
        os.symlink(self.mock.path, self.link)

        self.audio = FakeAudio()
        self._orig_make_audio = macropad.make_audio
        macropad.make_audio = lambda cfg: self.audio
        self.daemon = macropad.Daemon(self.cfg_path, self.link)
        self.actions_run = []
        self.daemon.run_action = lambda idx: self.actions_run.append(idx)
        self.thread = threading.Thread(target=self.daemon.forever, daemon=True)
        self.thread.start()
        self.assertTrue(self.mock.wait(lambda p: p.hash == self.daemon.digest and p.devs["OUT"], timeout=8),
                        "daemon did not upload config and push audio")

    def tearDown(self):
        self.daemon.stop()
        self.thread.join(timeout=5)
        macropad.make_audio = self._orig_make_audio
        self.mock.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_initial_sync(self):
        self.assertEqual(self.mock.uploads, 1)
        # "Virtual" is hidden and "Realtek" renamed by the example config
        self.assertEqual(self.mock.devs["OUT"], [("Speakers", True), ("Headset", False)])
        self.assertEqual(self.mock.devs["IN"], [("Built-in Mic", True)])
        self.assertEqual(self.mock.state, (30, False, False))
        self.assertTrue(self.mock.host_alive())

    def test_pot_sets_volume(self):
        self.mock.device_says("VOL 40")
        self.assertTrue(wait_until(lambda: 40 in self.audio.set_volume_calls))

    def test_encoder_selects_output(self):
        self.mock.device_says("SEL OUT 1")
        self.assertTrue(wait_until(lambda: ("OUT", "sink.b") in self.audio.set_default_calls))
        self.assertTrue(self.mock.wait(lambda p: p.devs["OUT"] == [("Speakers", False), ("Headset", True)]))

    def test_run_action(self):
        self.mock.device_says("ACT 2")
        self.assertTrue(wait_until(lambda: self.actions_run == [2]))

    def test_volume_change_on_pc_is_pushed(self):
        with self.audio.lock:
            self.audio.vol, self.audio.muted = 55, True
        self.assertTrue(self.mock.wait(lambda p: p.state == (55, True, False), timeout=4))

    def test_config_save_triggers_upload(self):
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["layers"][0]["keys"][0]["label"] = "Kopier"
        time.sleep(0.05)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.assertTrue(self.mock.wait(lambda p: p.uploads == 2, timeout=4))
        self.assertEqual(self.mock.keys[(0, 0)][2], "Kopier")

    def test_broken_config_keeps_device_config(self):
        old_hash = self.mock.hash
        time.sleep(0.05)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write('{ "layers": [ { "keys": [ "ctrl+nope" ] } ] }')
        time.sleep(2.0)
        self.assertEqual(self.mock.uploads, 1)
        self.assertEqual(self.mock.hash, old_hash)
        self.assertTrue(self.thread.is_alive())

    def test_garbage_from_device_does_not_crash(self):
        for junk in ["VOL abc", "VOL", "SEL OUT x", "SEL", "SEL OUT 99", "ACT", "ACT -1", "ACT zz",
                     "HELLO", "\x00\xff\xfe", "OK CFG", "ERR whatever", "x" * 3000]:
            self.mock.device_says(junk)
        self.mock.device_says_raw(b"\xff\xfe\x00 partial line without newline")
        self.mock.device_says("\nVOL 77")
        self.assertTrue(wait_until(lambda: 77 in self.audio.set_volume_calls),
                        "daemon stopped handling input after garbage")
        self.assertTrue(self.thread.is_alive())

    def test_stop(self):
        self.daemon.stop()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_unplug_and_replug(self):
        digest = self.daemon.digest
        stored = self.mock.stored_config
        self.mock.stop()                                   # unplug
        time.sleep(0.5)
        self.mock = MockPad()                              # replug (flash keeps the config)
        self.mock.start()
        self.mock.stored_config = stored
        self.mock._load_config()
        os.remove(self.link)
        os.symlink(self.mock.path, self.link)
        self.assertTrue(self.mock.wait(lambda p: p.host_alive() and p.devs["OUT"], timeout=10),
                        "daemon did not reconnect")
        self.assertEqual(self.mock.hash, digest)
        self.assertEqual(self.mock.uploads, 0, "unchanged config must not be re-uploaded")


if __name__ == "__main__":
    unittest.main()

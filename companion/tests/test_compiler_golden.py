"""Golden-file tests for the Python reference compiler (protocol v1 config format)."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import macropad  # noqa: E402

GOLDEN = os.path.join(HERE, "golden")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class GoldenOk(unittest.TestCase):
    def test_all_ok_configs(self):
        names = [n for n in sorted(os.listdir(os.path.join(GOLDEN, "ok")))
                 if n.endswith(".json") and not n.endswith(".actions.json")]
        self.assertGreater(len(names), 0)
        for name in names:
            with self.subTest(config=name):
                src = os.path.join(GOLDEN, "ok", name)
                base = src[:-5]
                _, lines, digest, actions = macropad.load_config(src)
                self.assertEqual("\n".join(lines) + "\n", _read(base + ".expected.txt"))
                self.assertEqual(actions, json.loads(_read(base + ".actions.json")))
                self.assertEqual(lines[-1], f"O hash {digest}")


class GoldenErr(unittest.TestCase):
    def test_all_error_configs(self):
        names = [n for n in sorted(os.listdir(os.path.join(GOLDEN, "err"))) if n.endswith(".json")]
        self.assertGreater(len(names), 0)
        for name in names:
            with self.subTest(config=name):
                src = os.path.join(GOLDEN, "err", name)
                with self.assertRaises(macropad.ConfigError) as cm:
                    macropad.load_config(src)
                msg = str(cm.exception).replace(src, "<config>") + "\n"
                self.assertEqual(msg, _read(src[:-5] + ".error.txt"))


class CompilerProperties(unittest.TestCase):
    def test_hash_is_sha1_prefix_of_lines(self):
        import hashlib
        cfg = {"layers": [{"keys": ["a"]}]}
        lines, digest, _ = macropad.Compiler(cfg).compile()
        body = "\n".join(lines[:-1]).encode()
        self.assertEqual(digest, hashlib.sha1(body).hexdigest()[:8])

    def test_every_line_fits_firmware_buffer(self):
        # Worst case: a full 512-byte macro and a 26-char label must fit LINE_BUF (1200) with "C " prefix.
        cfg = {"layers": [{"keys": [{"label": "x" * 40, "action": {"macro": [{"delay": 1}] * 170 + ["a"]}}]}]}
        lines, _, _ = macropad.Compiler(cfg).compile()
        self.assertLess(max(len("C " + line) for line in lines), 1199)

    def test_compile_is_deterministic(self):
        path = os.path.join(GOLDEN, "ok", "example.json")
        a = macropad.load_config(path)
        b = macropad.load_config(path)
        self.assertEqual(a[1], b[1])


if __name__ == "__main__":
    unittest.main()

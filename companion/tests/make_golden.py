#!/usr/bin/env python3
"""
Regenerate the golden files from the Python reference compiler (macropad.py).

    python tests/make_golden.py          # rewrite expected outputs
    python tests/make_golden.py --check  # exit 1 if any expected output would change

Layout:
  tests/golden/ok/<name>.json            config that must compile
  tests/golden/ok/<name>.expected.txt    compiled lines (last line is "O hash ...")
  tests/golden/ok/<name>.actions.json    run actions [{"linux": ..., "windows": ...}, ...]
  tests/golden/err/<name>.json           config that must be rejected
  tests/golden/err/<name>.error.txt      the exact error message (file path replaced by <config>)

Every other compiler implementation (e.g. the Windows tray app) must reproduce
these files byte for byte. Only regenerate them when a change to the compiled
format is intended, and never for configs that use only v1 features: their
hash must stay the same forever.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import macropad  # noqa: E402

GOLDEN = os.path.join(HERE, "golden")


def render_ok(path):
    _, lines, _digest, actions = macropad.load_config(path)
    return "\n".join(lines) + "\n", json.dumps(actions, indent=2, ensure_ascii=False) + "\n"


def render_err(path):
    try:
        macropad.load_config(path)
    except macropad.ConfigError as e:
        return str(e).replace(path, "<config>") + "\n"
    return None


def expected_files():
    """Yield (target_path, content) for every golden output."""
    for name in sorted(os.listdir(os.path.join(GOLDEN, "ok"))):
        if not name.endswith(".json") or name.endswith(".actions.json"):
            continue
        src = os.path.join(GOLDEN, "ok", name)
        base = src[:-5]
        compiled, actions = render_ok(src)
        yield base + ".expected.txt", compiled
        yield base + ".actions.json", actions
    for name in sorted(os.listdir(os.path.join(GOLDEN, "err"))):
        if not name.endswith(".json"):
            continue
        src = os.path.join(GOLDEN, "err", name)
        msg = render_err(src)
        if msg is None:
            raise SystemExit(f"{src} compiled but should have failed")
        yield src[:-5] + ".error.txt", msg


def main():
    check = "--check" in sys.argv
    changed = []
    for target, content in expected_files():
        old = None
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                old = f.read()
        if old != content:
            changed.append(os.path.relpath(target, HERE))
            if not check:
                with open(target, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
    if check:
        if changed:
            print("golden files out of date:\n  " + "\n  ".join(changed))
            sys.exit(1)
        print("golden files up to date")
    else:
        print(f"{len(changed)} golden file(s) written" + (":\n  " + "\n  ".join(changed) if changed else ""))


if __name__ == "__main__":
    main()

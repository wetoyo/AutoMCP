"""Offline tests for JS archive extraction and the discovery checker.

Run: python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.js_extract import extract  # noqa: E402
from validation.discovery_check import check  # noqa: E402

APP_JS = """'use strict';
class Tabs {
  constructor() { this.list = []; }

  addTab(url, options = {}) {
    const tab = { url };
    this.list.push(tab);
    return tab;
  }
}
const tabs = new Tabs();
function onNewTabCommand(event) {
  return tabs.addTab(event.url);
}
document.addEventListener("cmd-new-tab", onNewTabCommand);
"""


def make_asar(path: Path, files: dict[str, bytes]):
    """Write a minimal Electron .asar (Chromium Pickle header + concatenated data)."""
    header, blobs, offset = {"files": {}}, [], 0
    for rel, data in files.items():
        node = header
        *dirs, name = rel.split("/")
        for d in dirs:
            node = node["files"].setdefault(d, {"files": {}})
        node["files"][name] = {"size": len(data), "offset": str(offset)}
        blobs.append(data)
        offset += len(data)
    js = json.dumps(header).encode()
    padded = js + b"\0" * ((4 - len(js) % 4) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(js)) + padded
    path.write_bytes(struct.pack("<II", 4, len(pickle)) + pickle + b"".join(blobs))


def make_front_cd_zip(path: Path, files: dict[str, bytes]):
    """Write a zip whose central directory sits at offset 4 (Mozilla optimized-jar layout)."""
    tmp = path.with_suffix(".tmp.zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in files.items():
            z.writestr(rel, data)
    d = tmp.read_bytes()
    tmp.unlink()
    eocd = d.rfind(b"PK\x05\x06")
    sig, disk, cd_disk, n_disk, n, cd_size, cd_off, clen = struct.unpack_from("<4s4H2IH", d, eocd)
    cd, local = d[cd_off:cd_off + cd_size], d[:cd_off]
    shift = 4 + cd_size  # local records move behind [4-byte header][central directory]
    entries, p = bytearray(), 0
    for _ in range(n):
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", cd, p + 28)
        size = 46 + name_len + extra_len + comment_len
        e = bytearray(cd[p:p + size])
        struct.pack_into("<I", e, 42, struct.unpack_from("<I", e, 42)[0] + shift)
        entries += e
        p += size
    out = struct.pack("<I", len(entries)) + bytes(entries) + local
    out += struct.pack("<4s4H2IH", sig, disk, cd_disk, n_disk, n, cd_size, 4, clen)
    path.write_bytes(out)


class Extraction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_asar_round_trip(self):
        files = {"package.json": b'{"main":"main.js"}', "main.js": b"console.log(1)", "src/app.js": APP_JS.encode()}
        make_asar(self.tmp / "app.asar", files)
        rep = extract([self.tmp / "app.asar"], self.tmp / "out")["archives"][0]
        self.assertEqual((rep["kind"], rep["files"], rep["js_files"]), ("asar", 3, 2))
        self.assertEqual((self.tmp / "out" / "app" / "src" / "app.js").read_text(), APP_JS)

    def test_front_central_directory_zip(self):
        make_front_cd_zip(self.tmp / "omni.ja", {"chrome/a.js": b"function a(){}", "b.txt": b"x" * 500})
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(self.tmp / "omni.ja")  # the layout really is non-standard
        rep = extract([self.tmp / "omni.ja"], self.tmp / "out")["archives"][0]
        self.assertEqual((rep["files"], rep["js_files"]), (2, 1))
        self.assertEqual((self.tmp / "out" / "omni" / "b.txt").read_bytes(), b"x" * 500)

    def test_traversal_rejected(self):
        with zipfile.ZipFile(self.tmp / "evil.zip", "w") as z:
            z.writestr("../escape.js", "x")
        with self.assertRaises(ValueError):
            extract([self.tmp / "evil.zip"], self.tmp / "out")
        self.assertFalse((self.tmp / "escape.js").exists())


class DiscoveryCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "src").mkdir()
        (self.root / "src" / "app.js").write_text(APP_JS, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def report(self, **kw):
        r = {"operation": "open a tab", "execution_context": "renderer", "inputs": [], "observable_effects": [],
             "confidence": "high", "unknowns": [],
             "function": {"name": "Tabs.addTab", "file": "src/app.js", "line": 5, "signature": "(url, options)"},
             "call_chain": [{"symbol": "tabs.addTab", "file": "src/app.js", "line": 13}],
             "entry_points": [{"symbol": "cmd-new-tab", "file": "src/app.js", "line": 15}]}
        r.update(kw)
        return r

    def test_valid_report(self):
        self.assertEqual(check(self.root, self.report()), [])

    def test_wrong_definition_line(self):
        r = self.report(function={"name": "addTab", "file": "src/app.js", "line": 13, "signature": "x"})
        self.assertTrue(any("not defined" in p for p in check(self.root, r)))

    def test_bad_citations(self):
        r = self.report(call_chain=[{"symbol": "nope", "file": "src/app.js", "line": 13}],
                        entry_points=[{"symbol": "x", "file": "../outside.js", "line": 1}])
        problems = check(self.root, r)
        self.assertTrue(any("'nope' not found" in p for p in problems))
        self.assertTrue(any("'..'" in p for p in problems))

    def test_missing_file_and_fields(self):
        self.assertTrue(any("does not exist" in p for p in check(self.root, self.report(
            function={"name": "a", "file": "src/missing.js", "line": 1, "signature": ""}))))
        self.assertTrue(check(self.root, {"operation": "x"}))


if __name__ == "__main__":
    unittest.main()

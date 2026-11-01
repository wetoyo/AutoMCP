"""Extract JavaScript application archives for static analysis (nothing is executed).

Supported containers:
  zip family  .zip .ja (omni.ja) .xpi .jar .crx-less zips   -> zipfile (tolerates prefixed/optimized jars)
  Electron    .asar                                          -> header JSON + concatenated file data;
                                                                 files in <name>.asar.unpacked are copied
Writes <out>/<archive stem>/... and <out>/extract_manifest.json (archive sha256, file counts, JS file count).

Usage: python -m analysis.js_extract --out targets/<name>/extracted ARCHIVE [ARCHIVE ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zipfile
import zlib
from pathlib import Path, PurePosixPath

JS_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".sys.mjs", ".jsm"}


def _safe_dest(root: Path, rel: str) -> Path:
    """Reject absolute paths and traversal in archive member names."""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if not parts or rel.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
        raise ValueError(f"unsafe archive member path: {rel!r}")
    return root.joinpath(*parts)


def extract_zip_by_central_directory(archive: Path, dest: Path) -> int:
    """Zips whose central directory is not adjacent to the end record (e.g. Mozilla optimized jars,
    which place it at the front). Entries are located via the EOCD's absolute directory offset."""
    data = archive.read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise zipfile.BadZipFile("no end-of-central-directory record")
    _, _, _, _, count, _, cd_offset, _ = struct.unpack_from("<4s4H2IH", data, eocd)
    p, n = cd_offset, 0
    for _ in range(count):
        if data[p:p + 4] != b"PK\x01\x02":
            raise zipfile.BadZipFile(f"bad central directory entry at {p}")
        (method, csize, usize, name_len, extra_len, comment_len, local_off) = (
            struct.unpack_from("<H", data, p + 10)[0], *struct.unpack_from("<II", data, p + 20),
            *struct.unpack_from("<HHH", data, p + 28), struct.unpack_from("<I", data, p + 42)[0])
        name = data[p + 46:p + 46 + name_len].decode("utf-8", errors="replace")
        p += 46 + name_len + extra_len + comment_len
        if name.endswith("/"):
            continue
        if data[local_off:local_off + 4] != b"PK\x03\x04":
            raise zipfile.BadZipFile(f"bad local header for {name!r}")
        l_name, l_extra = struct.unpack_from("<HH", data, local_off + 26)
        start = local_off + 30 + l_name + l_extra
        raw = data[start:start + csize]
        if method == 0:
            content = raw
        elif method == 8:
            content = zlib.decompress(raw, -15)
        else:
            raise zipfile.BadZipFile(f"unsupported compression method {method} for {name!r}")
        if len(content) != usize:
            raise zipfile.BadZipFile(f"size mismatch for {name!r}")
        target = _safe_dest(dest, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        n += 1
    return n


def extract_zip(archive: Path, dest: Path) -> int:
    try:
        zipfile.ZipFile(archive).close()
    except zipfile.BadZipFile:
        return extract_zip_by_central_directory(archive, dest)
    n = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            target = _safe_dest(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    return n


def extract_asar(archive: Path, dest: Path) -> int:
    data = archive.read_bytes()
    # Chromium Pickle framing: [4: payload size=4][4: header pickle size][4: pickle payload size][4: json len][json]
    header_size = struct.unpack_from("<I", data, 4)[0]
    json_len = struct.unpack_from("<I", data, 12)[0]
    header = json.loads(data[16:16 + json_len].decode("utf-8"))
    base = 8 + header_size
    unpacked_dir = archive.with_name(archive.name + ".unpacked")
    n = 0

    def walk(node: dict, rel: str):
        nonlocal n
        for name, entry in node.get("files", {}).items():
            child = f"{rel}/{name}" if rel else name
            if "files" in entry:
                walk(entry, child)
                continue
            if "link" in entry:
                continue  # symlinks are not materialised
            target = _safe_dest(dest, child)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.get("unpacked"):
                src = _safe_dest(unpacked_dir, child)
                if src.exists():
                    shutil.copyfile(src, target)
                    n += 1
                continue
            off, size = base + int(entry["offset"]), int(entry["size"])
            target.write_bytes(data[off:off + size])
            n += 1

    walk(header, "")
    return n


def extract(archives: list[Path], out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    report = {"archives": []}
    for archive in archives:
        dest = out / archive.stem.replace(".", "_")
        if dest.exists():
            shutil.rmtree(dest)
        kind = "asar" if archive.suffix.lower() == ".asar" else "zip"
        files = extract_asar(archive, dest) if kind == "asar" else extract_zip(archive, dest)
        js = sum(1 for p in dest.rglob("*") if p.is_file() and
                 (p.suffix.lower() in JS_SUFFIXES or p.name.lower().endswith(".sys.mjs")))
        report["archives"].append({"archive": str(archive), "kind": kind, "extracted_to": str(dest),
                                   "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                                   "files": files, "js_files": js})
    (out / "extract_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("archives", nargs="+")
    a = ap.parse_args(argv)
    print(json.dumps(extract([Path(p) for p in a.archives], Path(a.out)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

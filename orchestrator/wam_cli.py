"""`wam`: the only shell command the discovery agent may run.

Subcommands (all target the run's selected application):
  identity                     sha256, PE TimeDateStamp, image base, file version of the target module
  bytes <rva> <count>          raw file bytes at a module-relative RVA (hex)
  probe <script.js> [--seconds N] [--observe JSON]
                               launch a disposable instance, load WAM lib + script (CONFIG = {}),
                               print every send() payload; ends after N s or on send({done: true}).
                               --observe evaluates harness observers before and after the script.
  validate [--calls N]         run the full validation harness on ./adapter

Electron targets (TARGETS kind "electron") replace `bytes` with `inspect`, and `probe` evaluates JS over
the DevTools protocol instead of Frida:
  identity                     exe + app.asar sha256, Electron version, fuses, available contexts
  inspect --anchor EXPR [--path JSON] [--context renderer|main] [--url-pattern RX]
                               launch a disposable instance, resolve anchor + path steps exactly as the bridge
                               does, and describe the value: properties, internal slots, and for functions the
                               closure-scope variables and source_sha256
  probe <script.js> [--handles FILE] [--seconds N] [--observe JSON] [--context ...] [--url-pattern RX]
                               run the script as the body of `async (h) => {...}` with WAM loaded (CONFIG = {});
                               `h` holds the handles from FILE (a JSON object of handle specs, or a manifest).
                               Prints the returned value; --observe evaluates observers before and after.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import frida  # noqa: E402

from runtime import electron_bridge as eb  # noqa: E402
from runtime.bridge import WAM_LIB  # noqa: E402
from validation import harness, observers  # noqa: E402


def module_path(target: str) -> Path:
    spec = harness.TARGETS[target]
    return spec["app_dir"] / spec["exe"]


def pe_info(data: bytes) -> dict:
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    image_base = struct.unpack_from("<Q", data, pe + 24 + 24)[0]
    sections = []
    for i in range(nsec):
        name, vsize, va, rsize, raw = struct.unpack_from("<8sIIII", data, pe + 24 + opt_size + 40 * i)
        sections.append((name.rstrip(b"\0").decode(), vsize, va, rsize, raw))
    return {"timestamp": struct.unpack_from("<I", data, pe + 8)[0], "image_base": image_base, "sections": sections}


def cmd_identity(target: str, _a) -> int:
    path = module_path(target)
    data = path.read_bytes()
    info = pe_info(data)
    out = {"module": path.name, "sha256": hashlib.sha256(data).hexdigest(),
           "pe_timestamp": f"0x{info['timestamp']:08x}", "image_base": f"0x{info['image_base']:x}",
           "sections": [{"name": n, "rva": f"0x{va:x}", "size": f"0x{vs:x}"} for n, vs, va, _, _ in info["sections"]]}
    try:
        import win32api
        v = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = v["FileVersionMS"], v["FileVersionLS"]
        out["file_version"] = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception as e:  # version resource is optional
        out["file_version_error"] = str(e)
    print(json.dumps(out, indent=2))
    return 0


def cmd_bytes(target: str, a) -> int:
    data = module_path(target).read_bytes()
    rva, count = int(a.rva, 0), int(a.count, 0)
    for name, vsize, va, rsize, raw in pe_info(data)["sections"]:
        if va <= rva < va + max(vsize, rsize):
            off = raw + rva - va
            print(json.dumps({"rva": f"0x{rva:x}", "section": name, "hex": data[off:off + count].hex()}))
            return 0
    print(json.dumps({"error": f"rva 0x{rva:x} not in any section"}))
    return 1


def cmd_probe(target: str, a) -> int:
    user_js = Path(a.script).read_text(encoding="utf-8")
    obs = json.loads(a.observe) if a.observe else []
    inst = harness.Instance(target)
    result = {"messages": [], "observe_before": None, "observe_after": None}
    try:
        pid = inst.start()
        result["pid"] = pid
        time.sleep(a.startup)
        session = frida.attach(pid)
        detached = []
        session.on("detached", lambda reason, crash: detached.append(f"{reason} {crash.summary if crash else ''}".strip()))
        if obs:
            result["observe_before"] = harness.snapshot(pid, obs)
        script = session.create_script(f"const CONFIG = {{}};\n{WAM_LIB.read_text(encoding='utf-8')}\n{user_js}")
        done = []

        def on_message(msg, data):
            result["messages"].append(msg)
            if msg.get("type") == "send" and isinstance(msg.get("payload"), dict) and msg["payload"].get("done"):
                done.append(True)
        script.on("message", on_message)
        script.load()
        deadline = time.monotonic() + a.seconds
        while time.monotonic() < deadline and not done and not detached:
            time.sleep(0.1)
        time.sleep(0.3)
        result["process_alive"] = inst.proc.poll() is None
        if obs and result["process_alive"]:
            result["observe_after"] = harness.snapshot(pid, obs)
        result["detached"] = detached
        if not detached:
            session.detach()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        inst.cleanup()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_identity_electron(target: str, _a) -> int:
    print(json.dumps(eb.identity(module_path(target)), indent=2))
    return 0


def _electron_instance(target: str, a):
    inst = harness.ElectronTargetInstance(target, inspect=a.context == "main")
    inst.start()
    if not inst.wait_endpoint():
        inst.cleanup()
        raise RuntimeError("remote debugging endpoint did not come up")
    time.sleep(a.startup)
    return inst


def _context(a) -> dict:
    return {"kind": a.context, **({"url_pattern": a.url_pattern} if a.url_pattern else {})}


def cmd_inspect(target: str, a) -> int:
    inst, out = None, {}
    try:
        inst = _electron_instance(target, a)
        cdp = eb.connect_context(inst.ports, _context(a))
        r = eb.Resolver(cdp)
        try:
            cur = r.evaluate(a.anchor)
            for st in json.loads(a.path) if a.path else []:
                cur = r.step(cur, st)
                out.setdefault("trail", []).append({"step": st, "got": cur.get("description", cur.get("type"))})
            out["value"] = r.describe(cur, limit=a.limit)
        except eb.HandleError as e:
            out["error"] = {"code": e.code, "message": str(e)}
        finally:
            r.release()
            cdp.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if inst:
            inst.cleanup()
    print(json.dumps(out, indent=2, default=str))
    return 0


PROBE_CALL = "function (names, ...v) { const h = {}; names.forEach((n, i) => { h[n] = v[i]; }); return this(h); }"


def cmd_probe_electron(target: str, a) -> int:
    body = Path(a.script).read_text(encoding="utf-8")
    handles = {}
    if a.handles:
        spec = json.loads(Path(a.handles).read_text(encoding="utf-8"))
        handles = spec.get("handles", spec) if isinstance(spec, dict) else {}
    obs = json.loads(a.observe) if a.observe else []
    result, inst = {}, None
    try:
        inst = _electron_instance(target, a)
        result["pid"] = inst.proc.pid
        if obs:
            result["observe_before"] = harness.snapshot(inst.proc.pid, obs, inst.ports)
        cdp = eb.connect_context(inst.ports, _context(a), timeout=a.seconds + 5)
        r = eb.Resolver(cdp)
        try:
            values = [r.resolve(n, h) for n, h in handles.items()]
            fn = r.evaluate(f"(() => {{ const CONFIG = {{}};\n{eb.WAM_ELECTRON.read_text(encoding='utf-8')}\n"
                            f"return async (h) => {{\n{body}\n}}; }})()", side_effect_free=False)
            res = cdp.call("Runtime.callFunctionOn", objectId=fn["objectId"], objectGroup=eb.GROUP,
                           functionDeclaration=PROBE_CALL, arguments=[{"value": list(handles)}, *map(eb._arg, values)],
                           awaitPromise=True, returnByValue=True)
            err = eb.remote_error(res)
            result["error" if err else "value"] = err or res["result"].get("value")
        except eb.HandleError as e:
            result["error"] = {"code": e.code, "message": str(e)}
        finally:
            r.release()
            cdp.close()
        time.sleep(0.3)
        result["process_alive"] = inst.proc.poll() is None
        if obs and result["process_alive"]:
            result["observe_after"] = harness.snapshot(inst.proc.pid, obs, inst.ports)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if inst:
            inst.cleanup()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_validate(target: str, a) -> int:
    return harness.main(["--adapter-dir", str(Path(a.workspace) / "adapter"), "--calls", str(a.calls)])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wam")
    ap.add_argument("--target", required=True)
    ap.add_argument("--workspace", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("identity")
    b = sub.add_parser("bytes"); b.add_argument("rva"); b.add_argument("count")
    p = sub.add_parser("probe"); p.add_argument("script")
    p.add_argument("--seconds", type=float, default=5.0); p.add_argument("--startup", type=float, default=3.0)
    p.add_argument("--observe"); p.add_argument("--handles")
    i = sub.add_parser("inspect"); i.add_argument("--anchor", required=True); i.add_argument("--path")
    i.add_argument("--limit", type=int, default=200); i.add_argument("--startup", type=float, default=3.0)
    for q in (p, i):
        q.add_argument("--context", choices=eb.CONTEXTS, default="renderer"); q.add_argument("--url-pattern")
    v = sub.add_parser("validate"); v.add_argument("--calls", type=int, default=2)
    a = ap.parse_args(argv)
    if a.target not in harness.TARGETS:
        print(f"unknown target {a.target}")
        return 2
    if harness.kind_of(a.target) == "electron":
        cmds = {"identity": cmd_identity_electron, "inspect": cmd_inspect, "probe": cmd_probe_electron,
                "validate": cmd_validate}
    else:
        cmds = {"identity": cmd_identity, "bytes": cmd_bytes, "probe": cmd_probe, "validate": cmd_validate}
    if a.cmd not in cmds:
        print(f"`{a.cmd}` is not available for {harness.kind_of(a.target)} targets; use one of {sorted(cmds)}")
        return 2
    return cmds[a.cmd](a.target, a)


if __name__ == "__main__":
    sys.exit(main())

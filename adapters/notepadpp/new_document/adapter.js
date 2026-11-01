// Frida adapter: Notepad++ "new document" via in-process call to Notepad_plus::fileNew().
//
// CONFIG is injected by runtime/bridge.py from manifest.json; nothing here hard-codes
// an absolute address. Every assumption is checked before the call:
//   1. module identity       - notepad++.exe loaded, PE TimeDateStamp matches manifest
//   2. function identity     - prologue bytes at module_base + rva match manifest
//   3. `this` identity       - Notepad_plus_Window* from GWLP_USERDATA; core at +core_offset
//                              must hold a back-pointer to the window object (_pPublicInterface)
//   4. thread affinity       - the call runs on the window's UI thread, inside DispatchMessageW
//
// The call is void __thiscall(Notepad_plus*), i.e. Win64 ABI with `this` in RCX.

'use strict';

const GWLP_USERDATA = -21;
const WM_NULL = 0x0000;
const NPPMSG = 0x0400 + 1000;
const NPPM_GETNBOPENFILES = NPPMSG + 7;
const NPPM_GETCURRENTBUFFERID = NPPMSG + 60;
const ALL_OPEN_FILES = 0;

const user32 = Process.getModuleByName('user32.dll');
const fn = (name, ret, args) => new NativeFunction(user32.getExportByName(name), ret, args);
const FindWindowExW = fn('FindWindowExW', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer']);
const GetWindowThreadProcessId = fn('GetWindowThreadProcessId', 'uint32', ['pointer', 'pointer']);
const GetWindowLongPtrW = fn('GetWindowLongPtrW', 'pointer', ['pointer', 'int']);
const SendMessageW = fn('SendMessageW', 'pointer', ['pointer', 'uint', 'pointer', 'pointer']);
const PostMessageW = fn('PostMessageW', 'int', ['pointer', 'uint', 'pointer', 'pointer']);
const GetCurrentThreadId = new NativeFunction(
  Process.getModuleByName('kernel32.dll').getExportByName('GetCurrentThreadId'), 'uint32', []);

class AdapterError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes)).map(b => b.toString(16).padStart(2, '0')).join('');
}

function resolveModule() {
  const mod = Process.findModuleByName(CONFIG.module);
  if (mod === null) throw new AdapterError('module_missing', `${CONFIG.module} not loaded`);
  const peOff = mod.base.add(0x3c).readU32();
  const timestamp = mod.base.add(peOff + 8).readU32();
  if (timestamp !== CONFIG.pe_timestamp) {
    throw new AdapterError('version_mismatch',
      `PE TimeDateStamp 0x${timestamp.toString(16)} != expected 0x${CONFIG.pe_timestamp.toString(16)}`);
  }
  return mod;
}

function resolveFunction(mod) {
  const addr = mod.base.add(CONFIG.rva);
  const got = hex(addr.readByteArray(CONFIG.prologue_hex.length / 2));
  if (got !== CONFIG.prologue_hex) {
    throw new AdapterError('signature_mismatch', `prologue ${got} != expected ${CONFIG.prologue_hex}`);
  }
  return addr;
}

function resolveMainWindow() {
  const cls = Memory.allocUtf16String(CONFIG.window_class);
  const pidBuf = Memory.alloc(4);
  let h = ptr(0);
  while (!(h = FindWindowExW(ptr(0), h, cls, ptr(0))).isNull()) {
    const tid = GetWindowThreadProcessId(h, pidBuf);
    if (pidBuf.readU32() === Process.id) return { hwnd: h, uiThreadId: tid };
  }
  throw new AdapterError('window_missing', `no ${CONFIG.window_class} window in pid ${Process.id}`);
}

function resolveCore(hwnd) {
  const win = GetWindowLongPtrW(hwnd, GWLP_USERDATA);
  if (win.isNull()) throw new AdapterError('state_not_ready', 'GWLP_USERDATA is null (window not initialised)');
  if (!win.add(CONFIG.hself_offset).readPointer().equals(hwnd)) {
    throw new AdapterError('layout_mismatch', 'Window::_hSelf does not match HWND');
  }
  const core = win.add(CONFIG.core_offset);
  if (!core.readPointer().equals(win)) {
    throw new AdapterError('layout_mismatch', 'Notepad_plus::_pPublicInterface back-pointer mismatch');
  }
  return core;
}

function observe(hwnd) {
  return {
    open_files: SendMessageW(hwnd, NPPM_GETNBOPENFILES, ptr(0), ptr(ALL_OPEN_FILES)).toInt32(),
    current_buffer: SendMessageW(hwnd, NPPM_GETCURRENTBUFFERID, ptr(0), ptr(0)).toString(),
  };
}

// Run `work` on the UI thread: piggy-back on the next DispatchMessageW from that thread,
// and post WM_NULL so an idle message loop wakes up.
function onUiThread(hwnd, uiThreadId, work) {
  return new Promise((resolve, reject) => {
    let done = false;
    const listener = Interceptor.attach(user32.getExportByName('DispatchMessageW'), {
      onEnter() {
        if (done || GetCurrentThreadId() !== uiThreadId) return;
        done = true;
        try { resolve(work()); } catch (e) { reject(e); }
        setTimeout(() => listener.detach(), 0);
      },
    });
    PostMessageW(hwnd, WM_NULL, ptr(0), ptr(0));
    setTimeout(() => {
      if (done) return;
      done = true;
      listener.detach();
      reject(new AdapterError('ui_thread_timeout', `UI thread did not pump messages within ${CONFIG.timeout_ms} ms`));
    }, CONFIG.timeout_ms);
  });
}

function preflight() {
  const mod = resolveModule();
  const target = resolveFunction(mod);
  const { hwnd, uiThreadId } = resolveMainWindow();
  const core = resolveCore(hwnd);
  return { mod, target, hwnd, uiThreadId, core };
}

rpc.exports = {
  // Read-only: report whether every resolution step succeeds, without calling anything.
  check() {
    try {
      const r = preflight();
      return { ok: true, module_base: r.mod.base.toString(), target: r.target.toString(),
               core: r.core.toString(), ui_thread_id: r.uiThreadId, state: observe(r.hwnd) };
    } catch (e) {
      return { ok: false, error: e.code || 'exception', message: e.message };
    }
  },

  async invoke(args) {
    if (args !== null && typeof args === 'object' && Object.keys(args).length !== 0) {
      return { ok: false, error: 'invalid_arguments', message: 'new_document takes no arguments' };
    }
    let r;
    try { r = preflight(); } catch (e) {
      return { ok: false, error: e.code || 'exception', message: e.message };
    }
    const fileNew = new NativeFunction(r.target, 'void', ['pointer']);
    try {
      return await onUiThread(r.hwnd, r.uiThreadId, () => {
        const before = observe(r.hwnd);
        fileNew(r.core);
        const after = observe(r.hwnd);
        const ok = after.open_files === before.open_files + 1 && after.current_buffer !== before.current_buffer;
        return ok ? { ok: true, before, after }
                  : { ok: false, error: 'postcondition_failed', message: 'open-file count/current buffer did not change as expected', before, after };
      });
    } catch (e) {
      return { ok: false, error: e.code || 'exception', message: e.message };
    }
  },
};

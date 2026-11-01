// Generic, application-agnostic helpers injected ahead of every adapter as `WAM`.
// Nothing here knows about any particular application.
'use strict';

const WAM = (() => {
  const user32 = Process.getModuleByName('user32.dll');
  const kernel32 = Process.getModuleByName('kernel32.dll');
  const u = (name, ret, args) => new NativeFunction(user32.getExportByName(name), ret, args);
  const FindWindowExW = u('FindWindowExW', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer']);
  const GetWindowThreadProcessId = u('GetWindowThreadProcessId', 'uint32', ['pointer', 'pointer']);
  const GetClassNameW = u('GetClassNameW', 'int', ['pointer', 'pointer', 'int']);
  const IsWindowVisible = u('IsWindowVisible', 'int', ['pointer']);
  const GetWindowLongPtrW = u('GetWindowLongPtrW', 'pointer', ['pointer', 'int']);
  const SendMessageW = u('SendMessageW', 'pointer', ['pointer', 'uint', 'pointer', 'pointer']);
  const PostMessageW = u('PostMessageW', 'int', ['pointer', 'uint', 'pointer', 'pointer']);
  const GetCurrentThreadId = new NativeFunction(kernel32.getExportByName('GetCurrentThreadId'), 'uint32', []);

  class AdapterError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }

  function hex(bytes) {
    return Array.from(new Uint8Array(bytes)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function className(hwnd) {
    const buf = Memory.alloc(512);
    const n = GetClassNameW(hwnd, buf, 256);
    return n > 0 ? buf.readUtf16String(n) : '';
  }

  // All top-level windows owned by this process.
  function topLevelWindows() {
    const pidBuf = Memory.alloc(4);
    const out = [];
    let h = ptr(0);
    while (!(h = FindWindowExW(ptr(0), h, ptr(0), ptr(0))).isNull()) {
      const tid = GetWindowThreadProcessId(h, pidBuf);
      if (pidBuf.readU32() === Process.id) {
        out.push({ hwnd: h, threadId: tid, className: className(h), visible: IsWindowVisible(h) !== 0 });
      }
    }
    return out;
  }

  function findWindow(cls) {
    const w = topLevelWindows().find(w => w.className === cls);
    if (!w) throw new AdapterError('window_missing', `no top-level ${cls} window in pid ${Process.id}`);
    return w;
  }

  // Module identity: name + PE header TimeDateStamp.
  function module(name, expectedTimestamp) {
    const mod = Process.findModuleByName(name);
    if (mod === null) throw new AdapterError('module_missing', `${name} not loaded`);
    const ts = mod.base.add(mod.base.add(0x3c).readU32() + 8).readU32();
    if (expectedTimestamp !== undefined && ts !== expectedTimestamp) {
      throw new AdapterError('version_mismatch', `${name} TimeDateStamp 0x${ts.toString(16)} != 0x${expectedTimestamp.toString(16)}`);
    }
    return mod;
  }

  // Verify bytes at an address before trusting it as a function entry.
  function expectBytes(addr, expectedHex, what) {
    const got = hex(addr.readByteArray(expectedHex.length / 2));
    if (got !== expectedHex.toLowerCase()) {
      throw new AdapterError('signature_mismatch', `${what || addr}: bytes ${got} != expected ${expectedHex}`);
    }
    return addr;
  }

  // Run `work` on the thread that owns `hwnd` (GUI thread affinity): it executes inside that
  // thread's next DispatchMessageW; WM_NULL is posted only to wake an idle message loop.
  function onUiThread(hwnd, threadId, work, timeoutMs = 5000) {
    return new Promise((resolve, reject) => {
      let done = false;
      const listener = Interceptor.attach(user32.getExportByName('DispatchMessageW'), {
        onEnter() {
          if (done || GetCurrentThreadId() !== threadId) return;
          done = true;
          try { resolve(work()); } catch (e) { reject(e); }
          setTimeout(() => listener.detach(), 0);
        },
      });
      PostMessageW(hwnd, 0 /* WM_NULL */, ptr(0), ptr(0));
      setTimeout(() => {
        if (done) return;
        done = true;
        listener.detach();
        reject(new AdapterError('ui_thread_timeout', `thread ${threadId} did not pump messages within ${timeoutMs} ms`));
      }, timeoutMs);
    });
  }

  function errorResult(e) {
    return { ok: false, error: e.code || 'exception', message: e.message };
  }

  return { AdapterError, hex, className, topLevelWindows, findWindow, module, expectBytes, onUiThread,
           errorResult, GetWindowLongPtrW, SendMessageW, GetCurrentThreadId };
})();

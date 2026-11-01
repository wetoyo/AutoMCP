// Notepad++ 8.9.8.1 x64: call Notepad_plus::fileNew() in-process on the GUI thread.
'use strict';

const NPPM_GETNBOPENFILES = 0x7ef; // NPPMSG(WM_USER+1000) + 7, lParam ALL_OPEN_FILES = 0

function resolve() {
  const mod = WAM.module(CONFIG.module, CONFIG.pe_timestamp);
  const fileNew = WAM.expectBytes(mod.base.add(CONFIG.file_new_rva), CONFIG.file_new_entry_hex, 'Notepad_plus::fileNew');
  WAM.expectBytes(mod.base.add(CONFIG.run_proc_rva), CONFIG.run_proc_entry_hex, 'Notepad_plus_Window::runProc');

  const w = WAM.findWindow(CONFIG.window_class);
  const win = WAM.GetWindowLongPtrW(w.hwnd, -21 /* GWLP_USERDATA */);
  if (win.isNull()) throw new WAM.AdapterError('state_invalid', 'GWLP_USERDATA is null');
  // Notepad_plus_Window::_hSelf must be the main window handle.
  if (!win.add(CONFIG.window_hself_offset).readPointer().equals(w.hwnd)) {
    throw new WAM.AdapterError('state_invalid', 'Notepad_plus_Window::_hSelf != main hwnd');
  }
  const core = win.add(CONFIG.core_offset);
  // Notepad_plus::_pPublicInterface (first member) points back to the owning window object.
  if (!core.add(CONFIG.core_public_iface_offset).readPointer().equals(win)) {
    throw new WAM.AdapterError('state_invalid', 'Notepad_plus::_pPublicInterface != window object');
  }
  // _pDocTab / _pNonDocTab point at DocTabView members embedded in the same Notepad_plus object.
  for (const off of [CONFIG.core_pdoctab_offset, CONFIG.core_pnondoctab_offset]) {
    const p = core.add(off).readPointer();
    if (p.isNull() || p.compare(core) <= 0 ||p.compare(core.add(CONFIG.core_size_bound)) >= 0) {
      throw new WAM.AdapterError('state_invalid', `doc tab pointer at +0x${off.toString(16)} not inside Notepad_plus`);
    }
  }
  const activeView = core.add(CONFIG.core_active_view_offset).readU32();
  if (activeView > 1) throw new WAM.AdapterError('state_invalid', `_activeView ${activeView} not 0/1`);

  return { mod, fileNew, w, win, core, activeView };
}

function openCount(hwnd) {
  return WAM.SendMessageW(hwnd, NPPM_GETNBOPENFILES, ptr(0), ptr(0)).toInt32();
}

rpc.exports = {
  check() {
    try {
      const r = resolve();
      return { ok: true, function: r.fileNew.toString(), this: r.core.toString(), ui_thread: r.w.threadId,
               open_documents: openCount(r.w.hwnd) };
    } catch (e) {
      return WAM.errorResult(e);
    }
  },

  async invoke(args) {
    try {
      if (args === null || args === undefined) args = {};
      if (typeof args !== 'object' || Array.isArray(args)) {
        return { ok: false, error: 'invalid_arguments', message: 'arguments must be an object' };
      }
      const unknown = Object.keys(args);
      if (unknown.length > 0) {
        return { ok: false, error: 'invalid_arguments', message: `unknown argument(s): ${unknown.join(', ')}` };
      }
      const r = resolve();
      const fileNew = new NativeFunction(r.fileNew, 'void', ['pointer'], 'win64');
      const result = await WAM.onUiThread(r.w.hwnd, r.w.threadId, () => {
        const before = openCount(r.w.hwnd);
        fileNew(r.core);
        return { before, after: openCount(r.w.hwnd) };
      }, CONFIG.timeout_ms || 5000);
      if (result.after !== result.before + 1) {
        return { ok: false, error: 'no_effect', message: `open documents ${result.before} -> ${result.after}` };
      }
      return { ok: true, open_documents_before: result.before, open_documents_after: result.after };
    } catch (e) {
      return WAM.errorResult(e);
    }
  },
};

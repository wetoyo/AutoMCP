// SumatraPDF 3.6.1 x64: CreateAndShowMainWindow(SessionData* data = nullptr), called in-process
// on the GUI thread exactly like the CmdNewWindow (242) case of the frame's command handler.
'use strict';

const FRAME_CLASS = CONFIG.frame_class;
const CANVAS_CLASS = CONFIG.canvas_class;
const MAX_WINDOWS = 256;

function E(code, msg) { return new WAM.AdapterError(code, msg); }

function readGlobals(base) {
  const prefs = base.add(CONFIG.gGlobalPrefs_rva).readPointer();
  if (prefs.isNull()) throw E('state_invalid', 'gGlobalPrefs is null');
  const windowState = prefs.add(CONFIG.prefs_windowState_off).readS32();
  if (windowState < 0 || windowState > 4) throw E('state_invalid', `gGlobalPrefs->windowState ${windowState} out of range`);

  const vec = base.add(CONFIG.gWindows_rva);
  const len = vec.add(CONFIG.vec_len_off).readU64().toNumber();
  const cap = vec.add(CONFIG.vec_cap_off).readU64().toNumber();
  const els = vec.add(CONFIG.vec_els_off).readPointer();
  if (len < 1 || len > MAX_WINDOWS || len > cap || els.isNull()) {
    throw E('state_invalid', `gWindows not plausible: len=${len} cap=${cap} els=${els}`);
  }
  const frames = WAM.topLevelWindows().filter(w => w.className === FRAME_CLASS);
  const wins = [];
  for (let i = 0; i < len; i++) {
    const w = els.add(i * 8).readPointer();
    if (w.isNull()) throw E('state_invalid', `gWindows[${i}] is null`);
    const hf = w.add(CONFIG.mw_hwndFrame_off).readPointer();
    const hc = w.add(CONFIG.mw_hwndCanvas_off).readPointer();
    const f = frames.find(x => x.hwnd.equals(hf));
    if (!f) throw E('state_invalid', `gWindows[${i}]->hwndFrame ${hf} is not a ${FRAME_CLASS} window of this process`);
    if (WAM.className(hc) !== CANVAS_CLASS) throw E('state_invalid', `gWindows[${i}]->hwndCanvas ${hc} is not ${CANVAS_CLASS}`);
    wins.push({ win: w, hwndFrame: hf, threadId: f.threadId });
  }
  return { prefs, windowState, len, wins };
}

function resolve() {
  const mod = WAM.module(CONFIG.module, CONFIG.pe_timestamp);
  const fnAddr = WAM.expectBytes(mod.base.add(CONFIG.fn_rva), CONFIG.fn_entry_bytes, 'CreateAndShowMainWindow');
  const g = readGlobals(mod.base);
  const ui = g.wins[0];
  if (g.wins.some(w => w.threadId !== ui.threadId)) throw E('state_invalid', 'main windows live on different threads');
  return { mod, fnAddr, g, ui };
}

function validateArgs(args) {
  if (args === undefined || args === null) return;
  if (typeof args !== 'object' || Array.isArray(args)) throw E('invalid_arguments', 'arguments must be an object');
  const extra = Object.keys(args);
  if (extra.length) throw E('invalid_arguments', `unknown argument(s): ${extra.join(', ')}`);
}

rpc.exports = {
  check() {
    try {
      const r = resolve();
      return { ok: true, function: r.fnAddr.toString(), windows: r.g.len, ui_thread: r.ui.threadId };
    } catch (e) { return WAM.errorResult(e); }
  },

  async invoke(args) {
    try {
      validateArgs(args);
      const r = resolve();
      const fn = new NativeFunction(r.fnAddr, 'pointer', ['pointer'], 'win64');
      const result = await WAM.onUiThread(r.ui.hwndFrame, r.ui.threadId, () => {
        // Re-validate on the GUI thread immediately before the call.
        const before = readGlobals(r.mod.base);
        const win = fn(ptr(0));
        if (win.isNull()) throw E('call_failed', 'CreateAndShowMainWindow returned null');
        const after = readGlobals(r.mod.base);
        if (after.len !== before.len + 1) throw E('postcondition_failed', `gWindows grew ${before.len} -> ${after.len}`);
        const added = after.wins[after.len - 1];
        if (!added.win.equals(win)) throw E('postcondition_failed', 'returned MainWindow is not the new gWindows entry');
        return { hwndFrame: added.hwndFrame.toString(), windowsBefore: before.len, windowsAfter: after.len };
      }, CONFIG.timeout_ms || 5000);
      return Object.assign({ ok: true }, result);
    } catch (e) { return WAM.errorResult(e); }
  },
};

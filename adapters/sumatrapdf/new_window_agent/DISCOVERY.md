# DISCOVERY: SumatraPDF 3.6.1 x64 "New Window"

Binary: `SumatraPDF-3.6.1-64.exe`, sha256 `719f689b…c3d0`, PE timestamp `0x69d3aabe`, image base `0x140000000`
(`./wam identity`). There are no symbols, so every name below comes from matching against the source.

## Source-level target

- `src/SumatraPDF.cpp:5448`: `case CmdNewWindow: CreateAndShowMainWindow(nullptr); break;` (`CmdNewWindow = 242`, `Commands.h:25`).
- `CreateAndShowMainWindow(SessionData* data)` (`SumatraPDF.cpp:1603`) calls the static `CreateMainWindow()` (`:1498`),
  which does `CreateWindowExW(FRAME_CLASS_NAME = L"SUMATRA_PDF_FRAME", L"SumatraPDF", …)`, creates the
  `SUMATRA_PDF_CANVAS` child, and appends to `gWindows`.

## Evidence chain (REA / Ghidra 12.1.4)

1. `search_strings "SUMATRA_PDF_FRAME"` returns one string at `0x140900cc8`.
2. `xrefs 0x140900cc8` returns 4 refs. `resolve_containing_procedure 0x1401a359b` returns **FUN_1401a34b4** (1284 bytes).
3. `procedure_pseudo_code 0x1401a34b4` matches `CreateMainWindow` line for line. It has no parameters, reads
   `gGlobalPrefs->windowPos` (`DAT_1410eb6e8+0x284`), and shifts x by `(int)gWindows.size*0xf` (`DAT_1410dae58`).
   It calls `CreateWindowExW(0,"SUMATRA_PDF_FRAME","SumatraPDF",0x2cf0000=WS_OVERLAPPEDWINDOW|WS_CLIPCHILDREN,…)`.
   It contains the assert string `"nullptr != FindMainWindowByHwnd(hwndFrame)"`, `new MainWindow` (alloc 0x4e0),
   the `SUMATRA_PDF_CANVAS` window stored at `win+0x18`, the `"win->menu"` assert (`win+0x1a0`), and the
   `gWindows.Append(win)` (Vec at `0x1410dae50`: len `+8`, cap `+0x10`, els `+0x20`). It returns `win`.
4. `procedure_callers 0x1401a34b4` returns a single caller, **0x1401a39b8**. Its `procedure_assembly` matches
   `CreateAndShowMainWindow` exactly:
   - It saves RCX (`data`) in RSI and reads `windowState = gGlobalPrefs+0x280`.
   - It calls CreateMainWindow and returns null on failure, then runs `ReportIf(windowState != …)`.
   - `if (data)`: it reads `data+0xc` (windowState) and `data+0x10` (windowPos), then calls ShiftRectToWorkArea and MoveWindow.
   - `ShowWindow(hwndFrame=[win+0x10], (state-2)<=1 ? SW_MAXIMIZE(3) : SW_SHOW(5))`, then `UpdateWindow`.
   - `if gWindows.len==1`: it runs HwndToForeground (IsIconic, ShowWindow 9, SetForegroundWindow).
   - It calls SetSidebarVisibility(win,false,prefs+0x51), ToolbarUpdateStateForWindow(win,true), and EnterFullScreen if state==3.
   - It returns win in RAX.
   - Signature: `MainWindow* __fastcall(SessionData*)` (win64, RCX only).
5. `xrefs 0x1401a39b8` returns 10 call sites. The site at `0x1401ac403` is in FUN_1401ab804, the WM_COMMAND
   dispatcher. Its bytes are `33c9 e8ae75ffff e9…` = `xor ecx,ecx; call 0x1401a39b8; jmp break`. The only
   incoming ref to it (`inspect_native_instruction`) is the `jz` at `0x1401ac225`. In the id chain at
   `0x1401ac20a` (`mov ecx,ebx; sub ecx,0xf0; jz; sub ecx,1; jz; sub ecx,1; jz →0x1ac403`), that branch is
   the case where `cmd == 0xf2 == 242 == CmdNewWindow`. This confirms that the menu command calls this function
   with `nullptr` on the frame's GUI thread.
6. The entry bytes at RVA `0x1a39b8` (`./wam bytes`) are `48895c2408 4889742410 57 4883ec50 488b05…`, which
   match the disassembly above. The adapter checks the first 18 bytes, before the RIP-relative displacement.

## Runtime verification (`./wam probe scratch/probe_layout.js`)

- `gGlobalPrefs` was non-null, with `windowState` = 1 (normal).
- `gWindows` at base+0x10dae50 had len=1 and cap=0xf. Its els pointer (at +0x20) points to the inline buffer at +0x28.
  The element's `+0x10` was the HWND of class `SUMATRA_PDF_FRAME`, and its `+0x18` was the HWND of class `SUMATRA_PDF_CANVAS`.
- All windows were on a single GUI thread.

## Adapter design

- Resolution: module base + RVA, guarded by `WAM.module` (timestamp) and `WAM.expectBytes`.
- State checks: `gGlobalPrefs` must be non-null with a sane windowState. `gWindows` len/cap must be plausible, and every
  MainWindow's frame/canvas HWNDs must have the expected classes and belong to this process. The checks run in
  `check()` and are repeated on the UI thread immediately before the call.
- Thread: `WAM.onUiThread(frameHwnd, frameThreadId, …)` runs the call inside that thread's DispatchMessageW,
  which is the same context as the WM_COMMAND path.
- Post-check in-process: gWindows len +1, and its last element == the returned pointer.
- Harness postconditions: visible top-level `SUMATRA_PDF_FRAME` count +1, and the process's visible top-level window
  count changes. The first attempt expected the second observer to be +1, which failed validation. It is actually +2,
  because one of the new frame's tooltips_class32 popups becomes visible, so the expectation is now `changed: true`.

## Failed hypotheses / notes

- I first assumed the Vec's `els` pointer would be the heap buffer. It is actually the small inline buffer (vec+0x28)
  while len is small. The adapter reads through `els`, so the code is correct either way.
- The class string has three other xrefs (FUN_1401b0284, FUN_1401b0dcc, FUN_1401b463c). I did not need them:
  FUN_1401a34b4 is the only one whose code matches `CreateMainWindow`.

## Remaining uncertainties

- If `gGlobalPrefs->windowState == 3` (fullscreen), the new window enters fullscreen. This is the same as the app's own behaviour.
- The "any visible top-level window" observer is coarse (`changed`). The frame-class observer is the precise one.
- I did not open the other three xrefs to the frame class string to confirm what they are.

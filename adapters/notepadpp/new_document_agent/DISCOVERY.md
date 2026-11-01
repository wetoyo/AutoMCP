# Discovery: Notepad++ 8.9.8.1 x64 — "create a new document"

Binary: `notepad++.exe`, sha256 `c52c9ed2…a9af2665709464b3cf`, PE TimeDateStamp `0x6ab526f3`, image base `0x140000000`, no symbols.
Analysis: REA / Ghidra 12.1.4.

## Source-level target

`PowerEditor/src/NppIO.cpp:2422`:

```cpp
void Notepad_plus::fileNew() {
    BufferID newBufID = MainFileManager.newEmptyDocument();
    if (newBufID != BUFFER_INVALID) {
        loadBufferIntoView(newBufID, currentView(), true);
        switchToFile(newBufID);
    }
}
```

This is what `IDM_FILE_NEW` runs (`NppCommands.cpp:162`). It takes no arguments apart from `this`.

## Evidence chain

1. **String anchor.** `search_strings("tab-untitled-string")` returned `0x140488178`. `xrefs` returned 4 references, in
   `FUN_140050880`, `FUN_140050b90`, `FUN_140050f60` and one at `0x1400ed447`. In source this string appears in
   `nextUntitledNewNumber`, `newEmptyDocument`, `bufferFromDocument` (Buffer.cpp, in that order) and
   `Notepad_plus::fileClose…` (Notepad_plus.cpp).
2. **`FileManager::newEmptyDocument` = `FUN_140050b90`.** The pseudocode matches the source line by line. It calls
   `FUN_140050880` (nextUntitledNewNumber) and then `wsprintfW(L"%d")`. It sends message `0x947` (= `SCI_CREATEDOCUMENT`,
   2375) through the scratch Scintilla at `this+0x10`. It allocates `0x208` bytes (`new Buffer`), pushes to `_buffers`,
   increments `_nbBufs`, and returns the buffer.
3. **`Notepad_plus::fileNew` = `FUN_14013c7a0` (RVA `0x13c7a0`).** `procedure_callers(0x140050b90)` returned
   `0x140050e50`, `0x1400ed380` and `0x14013c7a0`. The assembly of `0x14013c7a0` is:
   `rbx=rcx` (this). It calls `FUN_14004a940` (MainFileManager getter) and then `newEmptyDocument`. If the result is non-null, it calls
   `FUN_1400ed170(this, buf, [this+0x32fc], 1)`, which is `loadBufferIntoView(id, currentView(), true)`; the literal `true` is
   `R9B=1` and `_activeView` is `+0x32fc`. The rest is an inlined `switchToFile`: it searches `[this+0x800]` (`_pDocTab`), then
   `[this+0x808]` (`_pNonDocTab`), then calls `switchEditViewTo` and `activateBuffer`. This matches the source exactly. Its only
   resolved caller is `FUN_1401175e0`, which has many callers including the message processor, so it fits `Notepad_plus::command`.
   - Signature: `void (Notepad_plus* this)`, win64 (rcx = this). No return value is used.
   - Entry bytes (`./wam bytes 0x13c7a0 32`): `48895c2408574883ec20488bd9e88ee1f0ff488bc8e8d643f1ff488bf84885c0`.
4. **Getting `this`.** `search_strings("Windows session is about to be terminated…")` returned `0x1404a7750`. Its xref
   `0x14010c0d1` is inside `FUN_140109a60` (28 KB). That function is `Notepad_plus::process` (NppBigSwitch.cpp). Its only caller is
   `FUN_140109950`, which is `Notepad_plus_Window::runProc`:
   - In the default path it does `ADD RCX,0x20; CALL process`, so `_notepad_plus_plus_core` is at **window+0x20**.
   - In the `WM_CREATE` path (`CMP R8D,1`) it does `LEA RCX,[RBX+0x20]; MOV [RCX],RBX`. This is
     `_notepad_plus_plus_core._pPublicInterface = this`, so **core+0 == window**.
   - It calls `AddClipboardFormatListener([RBX+0x18])`, so `_hSelf` is at **window+0x18**.
   - `Notepad_plus_Proc` stores the window object in `GWLP_USERDATA` (NppBigSwitch.cpp:202).
5. **Runtime check** (`./wam probe scratch/probe_layout.js`, read-only): `GWLP_USERDATA` gave `win`. `win+0x18` equalled the hwnd.
   `core[0]` equalled `win`. `[core+0x800]`/`[core+0x808]` pointed at core+0x4a0/core+0x650, which are DocTabView members embedded in
   the object. `[core+0x32fc]` was 0. The entry bytes matched at runtime.

## Adapter

- Resolves `base+0x13c7a0`. It is guarded by the PE timestamp and by the entry bytes of both `fileNew` and `runProc`
  (`0x109950`), because the `+0x20` core offset comes from `runProc`.
- Structural checks before the call: `_hSelf == hwnd`, `_pPublicInterface == window`, both doc-tab pointers lie inside
  `[core, core+0x3358)`, and `_activeView ∈ {0,1}`. The bound comes from `window+0x3378` being a field of the window
  after the core.
- Makes the call on the GUI thread of the `Notepad++` window via `WAM.onUiThread`. After the call, it compares
  NPPM_GETNBOPENFILES before and after inside the same UI-thread slice and fails with `no_effect` if the count did not go up by 1.

## Postconditions (checked by the harness)

- `window_message` NPPM_GETNBOPENFILES (`0x7ef`, lParam 0) should change by +1.
- `child_control_items` with SysTabControl32 / TCM_GETITEMCOUNT (`0x1304`) should change by +1.
- `window_title` should change (`new 1 - Notepad++` → `new 2 - Notepad++`).

`./wam validate` passed: a fresh instance, 2 calls, a restart, and 2 more calls. All postconditions held and bad arguments were rejected.

## Failed hypotheses / notes

- `FUN_140050f60` also references the untitled string. It is `bufferFromDocument`, which creates a buffer with no tab, so it was
  not the target.
- Following callers of `SetWindowLongPtrW` to reach the window procedure produced too many results (90+). Tracing through
  `Notepad_plus::process` via a unique string was used instead.

## Remaining uncertainties

- The other two callers of `newEmptyDocument` (`0x140050e50`, `0x1400ed380`) were not decompiled. Per the source, they are the
  "close last tab → replace with new" path and `newPlaceholderDocument`-adjacent code. They are not needed for this tool.
- `fileNew` is reached in source from three places (`command`, plus two inside `command`'s other cases). Ghidra shows one direct
  caller, which suggests the duplicates were merged or inlined. This does not affect the call.
- The `core_size_bound` (0x3358) is inferred from the window field offset `0x3378`, not from an exact `sizeof(Notepad_plus)`.

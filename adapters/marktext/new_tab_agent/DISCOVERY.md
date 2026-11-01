# DISCOVERY: MarkText 0.22.0 — new empty untitled tab

## Target identity (`./wam identity`)
- marktext.exe sha256 `c013dd3c…98f5`, app.asar sha256 `72ebff95…fd7e`, Electron 42.1.0.
- Fuse `EnableNodeCliInspectArguments` is disabled, so only the `renderer` channel is reachable (`main: false`).
  The tool therefore has to run in a renderer page, and the function has to live in renderer code.

## Locating the function (static, in the bundle)
All references are to `app/out/renderer/assets/index-DlO1BZCi.js` (the main renderer bundle, Vite/Rolldown output, not minified).

- I grepped for `NEW_UNTITLED_TAB`. Every UI and IPC path that makes a blank tab ends up calling the same store action:
  - `190395`: `mt::bootstrap-editor` IPC handler → `this.NEW_UNTITLED_TAB({ selected: true })` (the startup blank tab)
  - `190415`, `190418`: `mt::open-new-tab` / `mt::new-untitled-tab` IPC handlers (the File → New Tab menu)
  - `190425`: the internal event-bus `mt::new-untitled-tab`
  - `191259`: the "New File" button on the "no tabs open" screen → `editorStore.NEW_UNTITLED_TAB({})`
  - `192797`: the "+" button in the tab bar → `editorStore.NEW_UNTITLED_TAB({})`
- Definition, `190677-190694`: `NEW_UNTITLED_TAB({ markdown: markdownString, selected })`. It is an action of
  `defineStore("editor", {...})` (`189843`). Its body: `selected` defaults to true, it calls `this.SHOW_TAB_VIEW(false)`,
  builds `getBlankFileState(this.tabs, …, markdownString ?? null)`, and then either calls `this.UPDATE_CURRENT_FILE(fileState)`
  and emits `file-loaded` (selected), or pushes the new state onto `this.tabs` (not selected).
- `getBlankFileState` (`186271`): when markdown is null it falls back to `defaultFileStateWithoutId.markdown`, which is `""`
  (`186204-186208`). It sets `pathname: ""` and `filename: Untitled-(max+1)`. This is what makes the document **empty and untitled**.
- Owner: the action relies on `this` being the editor store (`this.tabs`, `this.SHOW_TAB_VIEW`, `this.UPDATE_CURRENT_FILE`,
  `this.updateTabIdToIndex`). Pinia is created at `13815` and installed on the Vue app at `207054` (`app.use(pinia)`).
  The app is mounted on `#app` at `207060`.

## Reaching it at runtime (`./wam inspect`)
1. `document.querySelector("#app").__vue_app__` → `.config.globalProperties.$pinia`. This is a plain Pinia object
   with `_s` = `Map(9)`.
2. `_s.get("editor")` (a `get` step) returns the reactive store, a `Proxy(Object)`. That proxy is the `store` handle (the owner).
3. `[[Target]]` → `.NEW_UNTITLED_TAB` is Pinia's `wrappedAction` (sha `6cf910be…7585`). Its `[[Scopes]]` show
   `Closure (action)` → `fn` = `NEW_UNTITLED_TAB({ markdown: markdownString, selected }) {…}`, and `Closure (createSetupStore)`
   → `$id: 'editor'`.
4. `{"scope":"fn"}` gives the raw action, `name 'NEW_UNTITLED_TAB'`, `length 1`, with
   **source_sha256 `0b0d19cae04b58eb25ba506f5461b2a1117aff18b0dd148cae4f76dd0475e311`**. Its source text matches bundle line 190677 exactly.

The adapter calls `h.fn.call(h.store, { selected: true })`. This is the same `this` the wrapper would use
(`fn.apply(store, args)`). It skips only Pinia's action-subscription bookkeeping, and the editor store has no subscribers there (`actionSubscriptions: Set(0)`).

## Probe (`./wam probe scratch/probe1.js`)
- Page URL: `file:///…/app.asar/out/renderer/index.html?…&type=editor#/editor`, which gives the context `url_pattern`.
- Before the call: 1 tab (`mt-0:Untitled-1`), title `Untitled-1 - MarkText`, 1 `<li>` in `.editor-tabs ul.tabs-container`.
- After the call: 2 tabs (`mt-1:Untitled-2` is current), title `Untitled-2 - MarkText`, 2 `<li>`s.

## Postconditions (harness-side)
- `page_eval`: `document.querySelectorAll('.editor-tabs ul.tabs-container > li').length`, delta +1. This reads the rendered DOM,
  not the store.
- `window_title`: changes. This is the native Win32 title, read from outside the process.

`./wam validate`: passed (fresh instance with 2 calls, then a restart with 2 more calls; bad arguments rejected; no static problems).

## Failed hypotheses / notes
- I first thought the call might need to go through the Pinia wrapper (`store.NEW_UNTITLED_TAB`). Calling the raw `fn` with
  the store as `this` is equivalent and lets me pin the actual shipped action body.
- The `main` process (the menu's `mt::new-untitled-tab` sender) is not reachable because of the fuse. It is not needed anyway, since the action lives in the renderer.

## Remaining uncertainties
- The tool accepts no arguments. It always creates a blank, selected tab. The action's optional `markdown` and `selected`
  parameters are deliberately not exposed: the request is for an *empty* tab, and `selected: false` would leave the window title unchanged.
- With several editor windows open, the bridge binds to the first page that matches `type=editor`.
- The "no tabs" view (`SHOW_TAB_VIEW`) is handled inside the action itself. The adapter's readiness check needs the tab container
  `ul.tabs-container` in the DOM. That element is present on the editor route even when the tab bar is hidden.

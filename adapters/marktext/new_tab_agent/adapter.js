// MarkText 0.22.0: create a new, empty, selected untitled tab by calling the editor Pinia store's
// raw NEW_UNTITLED_TAB action (out/renderer/assets/index-DlO1BZCi.js:190677) with `this` = the store.

const TAB_SELECTOR = CONFIG.tab_selector;

function tabDomCount() {
  return document.querySelectorAll(TAB_SELECTOR).length;
}

function validateHandles(h) {
  if (!h || typeof h.fn !== 'function') throw new WAM.AdapterError('handle_missing', 'h.fn (raw NEW_UNTITLED_TAB) not resolved');
  if (h.fn.name !== 'NEW_UNTITLED_TAB' || h.fn.length !== 1) {
    throw new WAM.AdapterError('signature_mismatch', `unexpected function ${h.fn.name}/${h.fn.length}`);
  }
  const store = WAM.expectType(h.store, 'object', 'editor store');
  if (store.$id !== 'editor') throw new WAM.AdapterError('state_invalid', `store.$id is ${store.$id}, expected 'editor'`);
  for (const m of ['SHOW_TAB_VIEW', 'UPDATE_CURRENT_FILE', 'updateTabIdToIndex']) {
    if (typeof store[m] !== 'function') throw new WAM.AdapterError('state_invalid', `editor store lacks ${m}()`);
  }
  if (!Array.isArray(store.tabs)) throw new WAM.AdapterError('state_invalid', 'editor store tabs is not an array');
  if (!/#\/editor\b/.test(location.hash)) throw new WAM.AdapterError('state_not_ready', `page is not on the editor route (${location.hash})`);
  if (!document.querySelector(CONFIG.tabs_container_selector)) {
    throw new WAM.AdapterError('state_not_ready', 'editor tab bar not rendered yet');
  }
  return store;
}

rpc.exports = {
  check(h) {
    try {
      const store = validateHandles(h);
      return {
        ok: true,
        tab_count: store.tabs.length,
        current_tab: store.currentFile ? store.currentFile.id : null,
        dom_tab_count: tabDomCount(),
      };
    } catch (e) {
      return WAM.errorResult(e);
    }
  },

  async invoke(h, args) {
    try {
      WAM.validateArgs(args, CONFIG.inputSchema);
      const store = validateHandles(h);
      const beforeIds = new Set(store.tabs.map((t) => t.id));
      const beforeCount = store.tabs.length;
      const beforeDom = tabDomCount();

      // Signature: NEW_UNTITLED_TAB({ markdown, selected }); markdown omitted -> blank document.
      h.fn.call(store, { selected: true });

      if (store.tabs.length !== beforeCount + 1) {
        throw new WAM.AdapterError('postcondition_failed', `tab count ${beforeCount} -> ${store.tabs.length}`);
      }
      const created = store.tabs.find((t) => !beforeIds.has(t.id));
      if (!created) throw new WAM.AdapterError('postcondition_failed', 'no new tab id in editor store');
      if (!store.currentFile || store.currentFile.id !== created.id) {
        throw new WAM.AdapterError('postcondition_failed', 'new tab did not become the current file');
      }
      if (created.markdown !== '' || created.pathname) {
        throw new WAM.AdapterError('postcondition_failed', 'new tab is not an empty untitled document');
      }
      await WAM.waitFor(() => tabDomCount() === beforeDom + 1, CONFIG.timeout_ms || 2000);

      return {
        ok: true,
        tab_id: created.id,
        filename: created.filename,
        tab_count: store.tabs.length,
        dom_tab_count: tabDomCount(),
        title: document.title,
      };
    } catch (e) {
      return WAM.errorResult(e);
    }
  },
};

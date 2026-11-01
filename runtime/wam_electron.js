// Generic helpers available to Electron adapters as `WAM`. Evaluated in the adapter's context
// (a renderer page's main world, or the Node main process). Nothing here knows any application.
const WAM = (() => {
  class AdapterError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }

  function errorResult(e) {
    return { ok: false, error: (e && e.code) || 'exception', message: String((e && e.message) || e) };
  }

  // Resolve when predicate() is truthy (returns its value); reject with code after timeoutMs.
  function waitFor(predicate, timeoutMs = 2000, code = 'postcondition_failed', intervalMs = 25) {
    const deadline = Date.now() + timeoutMs;
    return new Promise((resolve, reject) => {
      const tick = () => {
        let v;
        try { v = predicate(); } catch (e) { return reject(e); }
        if (v) return resolve(v);
        if (Date.now() > deadline) return reject(new AdapterError(code, `condition not met within ${timeoutMs} ms`));
        setTimeout(tick, intervalMs);
      };
      tick();
    });
  }

  // Reject unknown keys and wrong primitive types against a flat JSON-Schema-like {properties, required}.
  function validateArgs(args, schema) {
    if (args === undefined || args === null) args = {};
    if (typeof args !== 'object' || Array.isArray(args)) throw new AdapterError('invalid_arguments', 'arguments must be an object');
    const props = (schema && schema.properties) || {};
    for (const k of Object.keys(args)) {
      if (!(k in props)) throw new AdapterError('invalid_arguments', `unknown argument: ${k}`);
      const t = props[k].type;
      const got = Array.isArray(args[k]) ? 'array' : (Number.isInteger(args[k]) ? 'integer' : typeof args[k]);
      if (t && t !== got && !(t === 'number' && got === 'integer')) {
        throw new AdapterError('invalid_arguments', `${k} must be ${t}, got ${got}`);
      }
    }
    for (const k of (schema && schema.required) || []) {
      if (!(k in args)) throw new AdapterError('invalid_arguments', `missing argument: ${k}`);
    }
    return args;
  }

  // Throw unless value has the expected typeof (or is a non-null object for 'object').
  function expectType(value, type, what) {
    const ok = type === 'object' ? (value !== null && typeof value === 'object') : typeof value === type;
    if (!ok) throw new AdapterError('state_invalid', `${what}: expected ${type}, got ${value === null ? 'null' : typeof value}`);
    return value;
  }

  return { AdapterError, errorResult, waitFor, validateArgs, expectType };
})();

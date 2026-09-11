/* ═══════════════════════════════════════════════════════════════════════
   WATHB runtime — a virtual filesystem for single-file ports.

   Installed before any app code runs. Every asset-loading API is routed
   through an in-memory filesystem, so the app's own paths keep working with
   nothing on disk and nothing on the network.

   Why it is not simpler
   ────────────────────
   · Paths resolve LEXICALLY to a full VFS key, not by substring or basename
     matching. Two files named `data.json` in different folders are different
     files; fuzzy matching silently serves the wrong one. Lexically rather
     than via `new URL(ref, base)` because the base is frequently unusable:
     in a blob: document — which is how Red Portal runs every game —
     `new URL('.', document.baseURI)` THROWS, since blob URLs are opaque
     rather than hierarchical. Anything that assumes a resolvable base dies
     before a single hook is installed and takes the whole app with it.
   · Property setters on element prototypes are patched, not a
     MutationObserver: the browser starts fetching the instant `img.src =`
     is assigned, and an observer runs a microtask too late to stop it.
   · References already present in the markup CANNOT be caught here at all —
     the parser sets those internally. The builder rewrites them ahead of
     time. Both layers are required.
   · A request for something absent is answered with a synthetic success of
     the right shape, never a 404. That is what keeps the console clean and
     stops the app noticing anything is missing.
   · All bytes live in ONE ArrayBuffer decoded once. Per-file base64 costs
     33% inflation and an atob call per asset.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var CFG = window.__WATHB__ || {};
  var INDEX = CFG.index || {};        // key -> [offset, length, mime]
  var QUIET = CFG.quiet !== false;    // synthesize rather than 404
  var LOG = CFG.log === true;
  var BYTES;

  function log() {
    if (LOG) console.debug.apply(console, ['[wathb]'].concat([].slice.call(arguments)));
  }

  /* ── payload: one pass over the concatenated base64 ───────────────── */
  (function decode() {
    // A nested document (an iframe built with srcdoc) inherits the parent's
    // origin, so it can borrow the already-decoded bytes instead of carrying
    // a second copy of a payload that may be hundreds of megabytes.
    if (CFG.bytes) { BYTES = CFG.bytes; return; }
    var chunks = CFG.payload || [];
    if (typeof chunks === 'string') chunks = [chunks];
    var bins = [], total = 0, i, k;
    for (i = 0; i < chunks.length; i++) {
      var b = atob(chunks[i]);
      bins.push(b);
      total += b.length;
    }
    BYTES = new Uint8Array(total);
    var o = 0;
    for (i = 0; i < bins.length; i++) {
      var s = bins[i];
      for (k = 0; k < s.length; k++) BYTES[o++] = s.charCodeAt(k);
    }
    CFG.payload = null;              // let the base64 strings be collected
  })();
  log('payload', BYTES.length, 'bytes /', Object.keys(INDEX).length, 'files');

  /* ── key resolution ───────────────────────────────────────────────────
     A blob: document has NO base to resolve against. `new URL('.', blobUrl)`
     throws outright, and so does `new URL('some/relative/path', blobUrl)` —
     blob URLs are opaque, not hierarchical. Red Portal runs every game in a
     blob tab, so this is the normal case here, not an edge case: any
     dependence on document.baseURI has to be optional. Paths are resolved
     lexically instead, which is what the VFS keys are anyway. */
  var BASE = null;
  try {
    var b = new URL('.', document.baseURI);
    if (b.protocol === 'http:' || b.protocol === 'https:' || b.protocol === 'file:') BASE = b;
  } catch (e) { BASE = null; }

  function lexical(p) {
    p = String(p).split('#')[0].split('?')[0].replace(/\\/g, '/');
    try { p = decodeURIComponent(p); } catch (e) { /* leave as written */ }
    var parts = p.split('/'), out = [];
    for (var i = 0; i < parts.length; i++) {
      var seg = parts[i];
      if (seg === '' || seg === '.') continue;
      if (seg === '..') { out.pop(); continue; }
      out.push(seg);
    }
    return out.join('/');
  }

  function toKey(url) {
    if (url == null) return null;
    if (typeof url !== 'string') {
      if (typeof URL !== 'undefined' && url instanceof URL) url = url.href;
      else if (url && typeof url.url === 'string') url = url.url;
      else url = String(url);
    }
    if (/^(data|blob|javascript|about|mailto|ws|wss):/i.test(url)) return null;

    var path;
    if (/^(https?:)?\/\//i.test(url)) {
      // absolute: an app hardcoding its own CDN. Resolve against its own
      // origin and keep the path tail.
      var abs;
      try { abs = new URL(url.indexOf('//') === 0 ? 'https:' + url : url); }
      catch (e) { return null; }
      path = lexical(abs.pathname);
      if (BASE && abs.origin === BASE.origin) {
        var bp = lexical(BASE.pathname);
        if (bp && path.indexOf(bp + '/') === 0) path = path.slice(bp.length + 1);
        else if (bp && path === bp) path = '';
      }
    } else if (url.charAt(0) === '/') {
      path = lexical(url);
      if (BASE) {
        var bp2 = lexical(BASE.pathname);
        if (bp2 && path.indexOf(bp2 + '/') === 0) path = path.slice(bp2.length + 1);
      }
    } else {
      path = lexical(url);
    }

    if (INDEX[path]) return path;
    // Rippers that percent-encode the filename when saving: the page asks
    // for "Dadish 2 Pk.js", the file is named "Dadish%202%20Pk.js".
    if (CFG.alias && CFG.alias[path]) return CFG.alias[path];

    // Mirrored trees: the app asks for the path its original <base href>
    // made resolve, while the file sits under an escaped mirror directory.
    // Accept the unique key ENDING with the reference; refuse when it is
    // ambiguous, because serving the wrong file silently is exactly the
    // failure this resolver exists to prevent.
    var bn = path.split('/').pop();
    var cands = (CFG.byName && CFG.byName[bn]) || null;
    if (cands && cands.length) {
      if (cands.length === 1 && (path.indexOf('/') < 0 || cands[0].endsWith('/' + path))) {
        return cands[0];
      }
      var tail = '/' + path, hits = [];
      for (var ci = 0; ci < cands.length; ci++) {
        if (cands[ci].endsWith(tail)) hits.push(cands[ci]);
      }
      if (hits.length === 1) return hits[0];
      if (cands.length === 1) return cands[0];
    }
    return null;
  }

  var bytesOf = function (k) { var e = INDEX[k]; return BYTES.subarray(e[0], e[0] + e[1]); };
  var mimeOf = function (k) { return INDEX[k][2] || 'application/octet-stream'; };

  /* ── synthetic answers for things genuinely not here ──────────────── */
  var PNG_1X1 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
  var WAV_EMPTY = 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';

  function b64u8(b) {
    var s = atob(b), u = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    return u;
  }

  function synth(url) {
    var clean = String(url).split('?')[0].split('#')[0];
    var ext = (clean.match(/\.([a-z0-9]+)$/i) || [, ''])[1].toLowerCase();
    switch (ext) {
      case 'json': return { body: '{}', mime: 'application/json' };
      case 'js': case 'mjs': return { body: '', mime: 'text/javascript' };
      case 'css': return { body: '', mime: 'text/css' };
      case 'html': case 'htm': return { body: '', mime: 'text/html' };
      case 'xml': return { body: '<?xml version="1.0"?><root/>', mime: 'text/xml' };
      case 'txt': case 'csv': case 'md': return { body: '', mime: 'text/plain' };
      case 'png': case 'jpg': case 'jpeg': case 'gif': case 'webp': case 'bmp': case 'ico':
        return { body: b64u8(PNG_1X1), mime: 'image/png' };
      case 'svg': return { body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>', mime: 'image/svg+xml' };
      case 'mp3': case 'wav': case 'ogg': case 'm4a': case 'aac':
        return { body: b64u8(WAV_EMPTY), mime: 'audio/wav' };
      case 'woff': case 'woff2': case 'ttf': case 'otf': case 'eot':
        return { body: new Uint8Array(0), mime: 'font/woff2' };
      default: return { body: new Uint8Array(0), mime: 'application/octet-stream' };
    }
  }

  /* A URL the PAGE made itself — blob: or data: — is real and must reach the
     native implementation. Synthesizing over it breaks every app that
     assembles an asset and then fetches it back: Brotato merges its wasm
     from .partN files into a Blob, re-fetches it, and got an empty buffer.
     `WebAssembly.instantiate(): BufferSource argument is empty` was this. */
  function isPageOwned(url) {
    return /^(blob|data):/i.test(String(url));
  }

  function responseFor(url) {
    if (isPageOwned(url)) return null;      // pass through to the real fetch
    var key = toKey(url);
    if (key) {
      log('serve', url, '->', key);
      return new Response(bytesOf(key), {
        status: 200, statusText: 'OK',
        headers: { 'Content-Type': mimeOf(key), 'Content-Length': String(INDEX[key][1]) },
      });
    }
    if (!QUIET) return null;
    var s = synth(url);
    log('synth', url, s.mime);
    return new Response(s.body, { status: 200, statusText: 'OK', headers: { 'Content-Type': s.mime } });
  }

  /* ── blob URLs for elements that need a real src ──────────────────── */
  var BLOBS = Object.create(null), SYNTH = Object.create(null);
  function blobFor(key) {
    return BLOBS[key] || (BLOBS[key] = URL.createObjectURL(
      new Blob([bytesOf(key)], { type: mimeOf(key) })));
  }
  function rewrite(url) {
    if (url == null) return url;
    var str = String(url);
    if (/^(data|blob|javascript|about):/i.test(str)) return url;
    var key = toKey(str);
    if (key) return blobFor(key);
    if (!QUIET) return url;
    var s = synth(str);
    return SYNTH[s.mime] || (SYNTH[s.mime] = URL.createObjectURL(new Blob([s.body], { type: s.mime })));
  }

  /* ── fetch ────────────────────────────────────────────────────────── */
  var origFetch = window.fetch && window.fetch.bind(window);
  window.fetch = function (resource, init) {
    try {
      var url = typeof resource === 'string' ? resource
        : (typeof Request !== 'undefined' && resource instanceof Request) ? resource.url
          : String(resource);
      var r = responseFor(url);
      if (r) {
        try {
          Object.defineProperty(r, 'url', { value: new URL(url, document.baseURI).href });
        } catch (e) { /* some engines seal Response */ }
        return Promise.resolve(r);
      }
    } catch (e) { log('fetch hook error', e); }
    return origFetch ? origFetch(resource, init) : Promise.reject(new Error('fetch unavailable'));
  };

  /* ── XMLHttpRequest ───────────────────────────────────────────────── */
  var OrigXHR = window.XMLHttpRequest;
  /* Define as an OWN data property rather than assigning.
     Some runtimes (GameMaker's HTML5 export among them) install
     compatibility accessors on XMLHttpRequest.prototype — and because we
     replaced the global, that is OUR prototype. A plain `this.response = x`
     then hits a getter-only property and throws in strict mode, killing the
     constructor. An own data property shadows the accessor instead. */
  function own(obj, name, value) {
    try {
      Object.defineProperty(obj, name, {
        value: value, writable: true, enumerable: true, configurable: true,
      });
    } catch (e) {
      try { obj[name] = value; } catch (e2) { /* nothing more to try */ }
    }
  }

  function XHR() {
    this._l = Object.create(null);
    own(this, 'readyState', 0); own(this, 'status', 0); own(this, 'statusText', '');
    own(this, 'response', null); own(this, 'responseText', '');
    own(this, 'responseType', ''); own(this, 'responseURL', '');
    own(this, 'responseXML', null);
    own(this, 'timeout', 0); own(this, 'withCredentials', false);
    own(this, 'onreadystatechange', null); own(this, 'onload', null);
    own(this, 'onerror', null); own(this, 'onloadstart', null);
    own(this, 'onloadend', null); own(this, 'onprogress', null);
    own(this, 'onabort', null); own(this, 'ontimeout', null);
    own(this, 'upload', {
      addEventListener: function () { }, removeEventListener: function () { },
    });
  }
  XHR.prototype.addEventListener = function (t, fn) { (this._l[t] = this._l[t] || []).push(fn); };
  XHR.prototype.removeEventListener = function (t, fn) {
    var l = this._l[t]; if (!l) return;
    var i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
  };
  XHR.prototype.abort = function () { this._aborted = true; };
  XHR.prototype.setRequestHeader = function () { };
  XHR.prototype.overrideMimeType = function (m) { this._forceMime = m; };
  XHR.prototype.getAllResponseHeaders = function () {
    return 'content-type: ' + (this._mime || 'application/octet-stream') + '\r\n'
      + 'content-length: ' + (this._len || 0) + '\r\n';
  };
  XHR.prototype.getResponseHeader = function (h) {
    h = String(h).toLowerCase();
    if (h === 'content-type') return this._forceMime || this._mime || null;
    if (h === 'content-length') return this._len != null ? String(this._len) : null;
    return null;
  };
  XHR.prototype.open = function (method, url, async) {
    this._method = method; this._url = url;
    this._async = async !== false;
    own(this, 'readyState', 1); this._fire('readystatechange');
  };
  XHR.prototype._fire = function (type, extra) {
    var ev = { type: type, target: this, currentTarget: this };
    if (extra) for (var k in extra) ev[k] = extra[k];
    var h = this['on' + type];
    if (typeof h === 'function') { try { h.call(this, ev); } catch (e) { console.error(e); } }
    var l = this._l[type];
    if (l) for (var i = 0; i < l.length; i++) { try { l[i].call(this, ev); } catch (e) { console.error(e); } }
  };
  XHR.prototype._deliver = function (data, mime) {
    if (this._aborted) return;
    this._mime = mime; this._len = data.byteLength;
    own(this, 'status', 200); own(this, 'statusText', 'OK');
    var href;
    try { href = new URL(this._url, document.baseURI).href; }
    catch (e) { href = String(this._url); }   // blob: documents have no base
    own(this, 'responseURL', href);
    var ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
    var rt = this.responseType || '';
    if (rt === 'arraybuffer') own(this, 'response', ab);
    else if (rt === 'blob') own(this, 'response', new Blob([ab], { type: mime }));
    else if (rt === 'json') {
      try { own(this, 'response', JSON.parse(new TextDecoder().decode(data))); }
      catch (e) { own(this, 'response', null); }
    } else {
      var t = new TextDecoder().decode(data);
      own(this, 'responseText', t); own(this, 'response', t);
      if (rt === 'document' || /xml|html/.test(mime)) {
        try {
          own(this, 'responseXML', new DOMParser().parseFromString(
            t, /html/.test(mime) ? 'text/html' : 'text/xml'));
        } catch (e) { /* not parseable */ }
      }
    }
    own(this, 'readyState', 2); this._fire('readystatechange');
    own(this, 'readyState', 3); this._fire('readystatechange');
    this._fire('progress', { lengthComputable: true, loaded: this._len, total: this._len });
    own(this, 'readyState', 4); this._fire('readystatechange');
    this._fire('load'); this._fire('loadend');
  };
  XHR.prototype.send = function () {
    var self = this, key = toKey(this._url), data, mime;
    // blob:/data: belong to the page; hand them to the real XHR
    if (!key && isPageOwned(this._url)) {
      var passthru = new OrigXHR();
      passthru.open(this._method, this._url, this._async !== false);
      if (this.responseType) { try { passthru.responseType = this.responseType; } catch (e) { } }
      passthru.onload = function () {
        own(self, 'status', passthru.status);
        own(self, 'statusText', passthru.statusText);
        own(self, 'response', passthru.response);
        try { own(self, 'responseText', passthru.responseText); } catch (e) { }
        own(self, 'readyState', 4);
        self._fire('readystatechange'); self._fire('load'); self._fire('loadend');
      };
      passthru.onerror = function () { self._fire('error'); self._fire('loadend'); };
      passthru.send();
      return;
    }
    if (key) { data = bytesOf(key); mime = mimeOf(key); }
    else if (QUIET) {
      var s = synth(this._url);
      // The caller told us it wants JSON; an empty body would give it
      // "Unexpected end of JSON input" instead of a harmless empty object.
      // The extension often does not say -- plenty of endpoints have none.
      if (this.responseType === 'json' && typeof s.body !== 'string') {
        s = { body: '{}', mime: 'application/json' };
      }
      data = typeof s.body === 'string' ? new TextEncoder().encode(s.body) : s.body;
      mime = s.mime;
    } else {
      var real = new OrigXHR();
      real.open(this._method, this._url, this._async);
      real.onload = function () { self._fire('load'); };
      real.send();
      return;
    }
    // synchronous XHR must be delivered before send() returns
    if (this._async === false) this._deliver(data, mime);
    else setTimeout(function () { self._deliver(data, mime); }, 0);
  };
  XHR.UNSENT = 0; XHR.OPENED = 1; XHR.HEADERS_RECEIVED = 2; XHR.LOADING = 3; XHR.DONE = 4;
  window.XMLHttpRequest = XHR;

  /* ── element src/href, set via property or setAttribute ───────────── */
  function patchProp(proto, prop) {
    if (!proto) return;
    var d = Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.set) return;
    Object.defineProperty(proto, prop, {
      configurable: true, enumerable: d.enumerable,
      get: function () { return d.get.call(this); },
      set: function (v) { d.set.call(this, rewrite(v)); },
    });
  }

  /* An inlined script reports src === '', and bundlers depend on it.
     Webpack's "publicPath: auto" reads document.currentScript.src to work out
     where its chunks live and THROWS "Automatic publicPath is not supported
     in this browser" when it is empty. Report a synthetic URL instead: the
     chunk requests that follow come back through toKey(), which strips the
     origin and resolves them out of the payload by path. */
  var SYNTH_ORIGIN = 'https://wathb.invalid/';
  if (window.HTMLScriptElement) {
    var sd = Object.getOwnPropertyDescriptor(window.HTMLScriptElement.prototype, 'src');
    if (sd && sd.get) {
      Object.defineProperty(window.HTMLScriptElement.prototype, 'src', {
        configurable: true, enumerable: sd.enumerable,
        get: function () {
          var v = sd.get.call(this);
          if (v) return v;
          return this.hasAttribute('src') ? v : SYNTH_ORIGIN + 'wathb-inline.js';
        },
        set: function (v) { sd.set.call(this, rewrite(v)); },
      });
    }
  }
  // HTMLScriptElement.src is handled above, with a getter as well as a setter
  [[window.HTMLImageElement, 'src'],
  [window.HTMLMediaElement, 'src'], [window.HTMLSourceElement, 'src'],
  [window.HTMLLinkElement, 'href'], [window.HTMLIFrameElement, 'src'],
  [window.HTMLEmbedElement, 'src'], [window.HTMLObjectElement, 'data'],
  [window.HTMLTrackElement, 'src'], [window.HTMLVideoElement, 'poster']]
    .forEach(function (p) { if (p[0]) patchProp(p[0].prototype, p[1]); });

  var origSet = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    var n = String(name).toLowerCase();
    if (n === 'src' || n === 'href' || n === 'poster' || n === 'data') {
      try { value = rewrite(value); } catch (e) { /* keep original */ }
    } else if (n === 'style') {
      try { value = rewriteCssValue(value); } catch (e) { /* keep original */ }
    } else if (n === 'srcset') {
      try {
        value = String(value).split(',').map(function (part) {
          var bits = part.trim().split(/\s+/);
          if (bits[0]) bits[0] = rewrite(bits[0]);
          return bits.join(' ');
        }).join(', ');
      } catch (e) { /* keep original */ }
    }
    return origSet.call(this, name, value);
  };

  /* ── CSS values assigned from script ──────────────────────────────
     `el.style.backgroundImage = 'url(' + someUrl + ')'` is neither an
     attribute nor a fetch, so nothing above sees it — and bundlers emit
     exactly this shape constantly. Rewrite url() inside the value. */
  var URL_IN_VALUE = /url\(\s*(['"]?)([^'")]+)\1\s*\)/gi;
  function rewriteCssValue(v) {
    if (typeof v !== 'string' || v.indexOf('url(') < 0) return v;
    return v.replace(URL_IN_VALUE, function (whole, q, ref) {
      if (/^(data|blob):/i.test(ref)) return whole;
      return 'url("' + rewrite(ref) + '")';
    });
  }

  var CSS_PROPS = ['background', 'backgroundImage', 'borderImage',
    'borderImageSource', 'listStyleImage', 'maskImage', 'webkitMaskImage',
    'content', 'cursor', 'shapeOutside', 'src', 'filter', 'clipPath',
    'offsetPath', 'webkitBoxReflect'];
  if (window.CSSStyleDeclaration) {
    var CSD = window.CSSStyleDeclaration.prototype;
    var kebab = function (p) {
      return p.replace(/^webkit/, '-webkit-')
        .replace(/[A-Z]/g, function (c) { return '-' + c.toLowerCase(); });
    };
    // Blink installs CSS property accessors LAZILY as own properties of each
    // style object, so getOwnPropertyDescriptor on the prototype reports
    // nothing and a descriptor-guarded patch silently does nothing. Define
    // them outright instead, backed by the real setProperty. This runs before
    // any app code, so no instance has shadowed them yet.
    CSS_PROPS.forEach(function (prop) {
      var css = kebab(prop);
      try {
        Object.defineProperty(CSD, prop, {
          configurable: true, enumerable: false,
          get: function () { return this.getPropertyValue(css); },
          set: function (v) { this.setProperty(css, rewriteCssValue(String(v))); },
        });
      } catch (e) { /* property is not configurable here */ }
    });
    var origSetProp = CSD.setProperty;
    CSD.setProperty = function (name, value, priority) {
      return origSetProp.call(this, name, rewriteCssValue(value), priority);
    };
    var cssTextDesc = Object.getOwnPropertyDescriptor(CSD, 'cssText');
    if (cssTextDesc && cssTextDesc.set) {
      Object.defineProperty(CSD, 'cssText', {
        configurable: true, enumerable: cssTextDesc.enumerable,
        get: function () { return cssTextDesc.get.call(this); },
        set: function (v) { cssTextDesc.set.call(this, rewriteCssValue(v)); },
      });
    }
  }
  if (window.CSSStyleSheet) {
    var origInsert = window.CSSStyleSheet.prototype.insertRule;
    if (origInsert) {
      window.CSSStyleSheet.prototype.insertRule = function (rule, index) {
        return origInsert.call(this, rewriteCssValue(rule), index);
      };
    }
  }

  /* CSS text poured into a dynamically created <style> element.
     Styling libraries (JSS / styled-components / emotion) build a rule as a
     string and assign it as the element's text — no attribute, no fetch, no
     setProperty, so every hook above misses it, and the image request fires
     on the next style recalc. */
  var isStyle = function (n) { return n && n.tagName === 'STYLE'; };

  function patchTextSink(proto, prop, guard) {
    if (!proto) return;
    var d = Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.set) return;
    Object.defineProperty(proto, prop, {
      configurable: true, enumerable: d.enumerable,
      get: function () { return d.get ? d.get.call(this) : undefined; },
      set: function (v) {
        try { if (guard(this)) v = rewriteCssValue(String(v)); } catch (e) { }
        return d.set.call(this, v);
      },
    });
  }
  patchTextSink(window.Node && window.Node.prototype, 'textContent', isStyle);
  patchTextSink(window.Element && window.Element.prototype, 'innerHTML', isStyle);
  patchTextSink(window.HTMLElement && window.HTMLElement.prototype, 'innerText', isStyle);
  // a text node whose parent is a <style>
  patchTextSink(window.CharacterData && window.CharacterData.prototype, 'data',
    function (n) { return isStyle(n.parentNode); });
  patchTextSink(window.Node && window.Node.prototype, 'nodeValue',
    function (n) { return isStyle(n.parentNode); });

  ['appendChild', 'insertBefore', 'replaceChild'].forEach(function (fn) {
    var orig = window.Node && window.Node.prototype[fn];
    if (!orig) return;
    window.Node.prototype[fn] = function (node) {
      try {
        if (isStyle(this) && node && node.nodeType === 3 && node.data
            && node.data.indexOf('url(') >= 0) {
          node.data = rewriteCssValue(node.data);
        }
      } catch (e) { /* fall through and insert as-is */ }
      return orig.apply(this, arguments);
    };
  });

  // `new Audio(url)` sets src inside the constructor, before our setter
  if (window.Audio) {
    var OrigAudio = window.Audio;
    window.Audio = function (src) {
      return arguments.length ? new OrigAudio(rewrite(src)) : new OrigAudio();
    };
    window.Audio.prototype = OrigAudio.prototype;
  }

  /* ── static refs the builder deferred (too large to inline twice) ─── */
  function hydrate() {
    // An embedded iframe document: attach it as a blob URL. It inherits this
    // origin, so its own runtime can borrow __WATHB_SHARE__ from the parent
    // rather than carrying a second copy of the payload.
    var docs = document.querySelectorAll('[data-wathb-doc]');
    for (var d = 0; d < docs.length; d++) {
      var dkey = docs[d].getAttribute('data-wathb-doc');
      if (INDEX[dkey]) {
        origSet.call(docs[d], 'src', blobFor(dkey));
        docs[d].removeAttribute('data-wathb-doc');
      }
    }
    ['src', 'poster', 'data', 'href'].forEach(function (attr) {
      var nodes = document.querySelectorAll('[data-wathb-' + attr + ']');
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i], key = el.getAttribute('data-wathb-' + attr);
        if (INDEX[key]) {
          origSet.call(el, attr, blobFor(key));
          el.removeAttribute('data-wathb-' + attr);
        }
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hydrate);
  } else { hydrate(); }
  window.__wathbHydrate = hydrate;

  /* ── WebAssembly streaming ────────────────────────────────────────── */
  if (window.WebAssembly) {
    ['instantiateStreaming', 'compileStreaming'].forEach(function (name) {
      var orig = WebAssembly[name];
      WebAssembly[name] = function (src, imports) {
        return Promise.resolve(src).then(function (resp) {
          if (resp && typeof resp.arrayBuffer === 'function') {
            return resp.arrayBuffer().then(function (buf) {
              return name === 'compileStreaming'
                ? WebAssembly.compile(buf)
                : WebAssembly.instantiate(buf, imports);
            });
          }
          if (orig) return orig(src, imports);
          throw new Error('WebAssembly.' + name + ' unavailable');
        });
      };
    });
  }

  /* ── workers get the same filesystem ──────────────────────────────── */
  function u8b64(u8) {
    var CH = 0x8000, out = '';
    for (var i = 0; i < u8.length; i += CH) out += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    return btoa(out);
  }
  function workerBoot(cfg, b64) {
    var s = atob(b64), B = new Uint8Array(s.length), i;
    for (i = 0; i < s.length; i++) B[i] = s.charCodeAt(i);
    var IDX = cfg.index;
    // Resolved lexically, for the same reason as the main thread: a worker
    // spawned from a blob document has no hierarchical base to resolve
    // against, and new URL(relative, blobUrl) throws.
    function key(u) {
      if (u == null) return null;
      u = String(typeof u === 'object' && u.url ? u.url : u);
      if (/^(data|blob|javascript|about):/i.test(u)) return null;
      var p = u.split('#')[0].split('?')[0].replace(/\\/g, '/');
      if (/^(https?:)?\/\//i.test(p)) p = p.replace(/^(https?:)?\/\/[^/]*/i, '');
      try { p = decodeURIComponent(p); } catch (e) { /* leave as written */ }
      var parts = p.split('/'), out = [];
      for (var j = 0; j < parts.length; j++) {
        if (parts[j] === '' || parts[j] === '.') continue;
        if (parts[j] === '..') { out.pop(); continue; }
        out.push(parts[j]);
      }
      p = out.join('/');
      if (IDX[p]) return p;
      var bn = p.split('/').pop();
      if (bn && cfg.byName && cfg.byName[bn] && cfg.byName[bn].length === 1) {
        return cfg.byName[bn][0];
      }
      return null;
    }
    var slice = function (k) { return B.subarray(IDX[k][0], IDX[k][0] + IDX[k][1]); };
    self.fetch = function (u) {
      var k = key(typeof u === 'string' ? u : (u && u.url));
      return Promise.resolve(k
        ? new Response(slice(k), { status: 200, headers: { 'Content-Type': IDX[k][2] } })
        : new Response('', { status: 200 }));
    };
    var oi = self.importScripts;
    if (oi) {
      self.importScripts = function () {
        var urls = [];
        for (var j = 0; j < arguments.length; j++) {
          var k = key(arguments[j]);
          urls.push(k
            ? URL.createObjectURL(new Blob([slice(k)], { type: 'text/javascript' }))
            : arguments[j]);
        }
        return oi.apply(self, urls);
      };
    }
  }
  var OrigWorker = window.Worker;
  if (OrigWorker) {
    window.Worker = function (url, opts) {
      var key = toKey(url);
      if (key) {
        var boot = '(' + workerBoot.toString() + ')('
          + JSON.stringify({ index: INDEX, byName: CFG.byName || {} }) + ','
          + JSON.stringify(u8b64(BYTES)) + ');\n';
        var src = new TextDecoder().decode(bytesOf(key));
        return new OrigWorker(URL.createObjectURL(
          new Blob([boot + src], { type: 'text/javascript' })), opts);
      }
      return new OrigWorker(rewrite(url), opts);
    };
    window.Worker.prototype = OrigWorker.prototype;
  }

  /* ── the app's own `new URL(x, base)` calls ───────────────────────────
     App code does this constantly, and in a blob document it throws
     "Failed to construct 'URL': Invalid base URL" — Unity's JobScheduler
     hits it in CreateWorker before the engine ever starts. Falling back to a
     synthetic base keeps that code running; nothing is fetched from the
     fallback origin because every load path is already intercepted.
     Installed AFTER our own BASE probe, which must be allowed to fail. */
  if (window.URL) {
    var NativeURL = window.URL;
    var FALLBACK_BASE = 'https://wathb.invalid/';
    var PatchedURL = function (url, base) {
      try {
        return arguments.length < 2 ? new NativeURL(url) : new NativeURL(url, base);
      } catch (e) {
        try { return new NativeURL(url, FALLBACK_BASE); } catch (e2) { throw e; }
      }
    };
    PatchedURL.prototype = NativeURL.prototype;
    PatchedURL.createObjectURL = function (o) { return NativeURL.createObjectURL(o); };
    PatchedURL.revokeObjectURL = function (u) { return NativeURL.revokeObjectURL(u); };
    if (NativeURL.canParse) {
      PatchedURL.canParse = function () { return true; };
    }
    try { window.URL = PatchedURL; } catch (e) { /* frozen */ }
  }

  // `new Request('models/car.glb')` fails the same way and for the same
  // reason -- it resolves against the document base, which a blob has none
  // of. Polytrack builds every asset request this way.
  if (window.Request) {
    var NativeRequest = window.Request;
    var PatchedRequest = function (input, init) {
      try {
        return new NativeRequest(input, init);
      } catch (e) {
        if (typeof input === 'string') {
          try {
            return new NativeRequest(
              'https://wathb.invalid/' + String(input).replace(/^\.?\//, ''), init);
          } catch (e2) { /* fall through to the original error */ }
        }
        throw e;
      }
    };
    PatchedRequest.prototype = NativeRequest.prototype;
    try { window.Request = PatchedRequest; } catch (e) { /* frozen */ }
  }

  /* ── stand-ins for the third-party SDKs the builder removed ───────────
     Ad and analytics scripts are dropped at build time (they exist only to
     reach the network, and embedding them would put the requests straight
     back). Their globals are still referenced by the app's own bundled code,
     though, and `google is not defined` kills the frame it is thrown in.
     A permissive no-op absorbs any call, property access or construction. */
  if (typeof Proxy === 'function') {
    var STUB = new Proxy(function () { }, {
      get: function (t, prop) {
        if (prop === Symbol.toPrimitive) return function () { return ''; };
        if (prop === Symbol.iterator) return undefined;
        if (prop === 'toString' || prop === 'valueOf') return function () { return ''; };
        if (prop === 'length') return 0;
        // stay awaitable: sdk.init().then(cb) is a very common shape, and a
        // missing `then` would throw while a never-settling one would hang
        if (prop === 'then') {
          return function (res) {
            if (typeof res === 'function') setTimeout(function () { res(undefined); }, 0);
            return STUB;
          };
        }
        // Fire whatever the app registers. Ad SDKs are a GATE, not a
        // decoration: a preroll game calls requestAds() and starts only from
        // the resulting callback, so a stub that merely absorbs the listener
        // leaves it on "Loading..." for ever. Moto X3M hung exactly here.
        if (prop === 'addEventListener' || prop === 'addListener'
            || prop === 'on' || prop === 'once') {
          return function (_type, cb) {
            var fn = typeof _type === 'function' ? _type : cb;
            if (typeof fn === 'function') setTimeout(function () {
              try { fn(STUB); } catch (e) { /* the app's own handler */ }
            }, 0);
            return STUB;
          };
        }
        return STUB;
      },
      set: function () { return true; },
      has: function () { return true; },
      apply: function () { return STUB; },
      construct: function () { return STUB; },
    });
    ['google', 'googletag', 'gtag', 'ga', '_gaq', 'fbq', 'PokiSDK', 'CrazyGames',
      'Crazygames', 'GameAnalytics', 'GameMonetize', 'adsbygoogle', 'aiptag',
      'ramp', 'cookieconsent'].forEach(function (name) {
        if (typeof window[name] === 'undefined') {
          try { window[name] = STUB; } catch (e) { /* non-writable */ }
        }
      });
    // these two are conventionally real values, not objects
    if (!Array.isArray(window.dataLayer)) { try { window.dataLayer = []; } catch (e) { } }
    window.__wathbStub = STUB;
  }

  /* ── things that only exist to reach the network ──────────────────── */
  if (navigator.sendBeacon) navigator.sendBeacon = function () { return true; };
  /* Service workers cannot exist here at all. In a blob: document the origin
     is opaque ("null"), so every call throws SecurityError -- not just
     register(): Godot's loader calls getRegistrations() and dies on it.
     Replace the whole interface with something inert that resolves. */
  if (navigator.serviceWorker) {
    var fakeReg = {
      scope: (BASE && BASE.href) || './',
      active: null, installing: null, waiting: null,
      addEventListener: function () { }, removeEventListener: function () { },
      update: function () { return Promise.resolve(); },
      unregister: function () { return Promise.resolve(true); },
    };
    var swStub = {
      controller: null,
      ready: new Promise(function () { }),   // never settles, like a real page with no SW
      register: function () { return Promise.resolve(fakeReg); },
      getRegistration: function () { return Promise.resolve(undefined); },
      getRegistrations: function () { return Promise.resolve([]); },
      startMessages: function () { },
      addEventListener: function () { }, removeEventListener: function () { },
      dispatchEvent: function () { return true; },
    };
    try {
      Object.defineProperty(navigator, 'serviceWorker', {
        configurable: true, get: function () { return swStub; },
      });
    } catch (e) {
      try { navigator.serviceWorker.register = swStub.register; } catch (e2) { }
    }
  }

  window.WATHB = {
    has: function (p) { return !!toKey(p); },
    keys: function () { return Object.keys(INDEX); },
    read: function (p) { var k = toKey(p); return k ? bytesOf(k) : null; },
    url: function (p) { var k = toKey(p); return k ? blobFor(k) : null; },
    size: BYTES.length,
  };
  // what a nested srcdoc document picks up instead of its own payload
  window.__WATHB_SHARE__ = {
    index: INDEX, byName: CFG.byName || {}, alias: CFG.alias || {}, bytes: BYTES,
  };
  log('installed');
})();

# WATHB — Web App To HTML Builder

Turns a web app or game folder into **one self-contained `.html` file** that
still resolves every path the app asks for, makes **zero network requests**,
and produces **no new console errors**.

```bash
python build.py "path/to/game"            # -> game.html
python build.py "path/to/game" -o out.html --entry sub/index.html
```

Verify a port against the original before trusting it:

```bash
node verify.mjs --url http://127.0.0.1:8802/game.html
node verify.mjs --jobs jobs.json --out report.json
```

Build a whole Red Portal-shaped tree:

```bash
python batch.py "C:\...\red-portal-DKR-LCL-main" -o built/
node verify.mjs --jobs built/jobs.json --out built/verify-report.json
```

## How it works

Two layers. **Neither is sufficient alone**, and that is the central fact
about this problem.

### Build time — `build.py`

References that already exist in the markup can never be caught at runtime:
the HTML parser sets those attributes internally and begins the request
before any script can execute. Patching `setAttribute` or the `src` property
setter catches nothing. So the builder rewrites them:

| Input | Becomes |
| --- | --- |
| `<script src>` | inlined `<script>`, order preserved |
| `<link rel=stylesheet>` | inlined `<style>`, `@import` flattened recursively |
| `url()` in CSS | data URI, resolved **relative to the stylesheet**, not the document |
| `<img>`, `<audio>`, `<video>`, `srcset` | data URI under 256 KB, `data-wathb-src` above it |
| `<base href>` | removed — it would re-point every relative URL |
| external `<script>` / `<link>` | dropped, so they cannot request or error |

Everything else is concatenated into one payload, base64'd, and carried in
the file alongside an index of `path -> [offset, length, mime]`.

### Run time — `rpvfs.js`

Installed in `<head>` before any app code. Catches everything dynamic:

- `fetch` and `XMLHttpRequest` — the XHR shim honours `responseType`
  (`arraybuffer`/`blob`/`json`/`document`), `addEventListener`, progress
  events, `getResponseHeader`, and synchronous requests
- `img.src =`, `new Audio(...)`, `setAttribute`, and the `src`/`href`/`poster`
  setters on every relevant element prototype
- `WebAssembly.instantiateStreaming` / `compileStreaming`
- `new Worker(...)` and `importScripts` — the worker is handed the same
  filesystem, since it has its own scope
- `navigator.sendBeacon` and `serviceWorker.register`, neutralised: a service
  worker cannot be registered from a single file and the rejection surfaces
  as an uncaught error

## Design decisions worth keeping

**Paths resolve through `new URL(ref, base)`** — the algorithm the browser
itself uses — never by substring or basename matching. Two files called
`data.json` in different folders are different files; fuzzy matching silently
serves the wrong one. A unique-basename fallback exists but only fires when
exactly one candidate matches.

**A missing file returns a synthetic success, not a 404.** `{}` for JSON, a
1×1 PNG for images, a valid silent WAV for audio, empty text for JS/CSS. This
is what keeps the console clean and stops the app noticing anything is
absent. `--loud` disables it while debugging.

**All bytes live in one `ArrayBuffer`, decoded once.** Per-file base64 costs
33% inflation and an `atob` call per asset.

**`<link rel="icon" href="data:,">` is injected**, because the browser
requests `/favicon.ico` on its own and that 404 would otherwise appear in
every single port.

## Limits, measured not assumed

- **V8 caps one JS string at 512 MiB**, so base64 cannot carry more than
  **384 MiB** of assets. Larger apps cannot be a single file — the builder
  refuses rather than emitting something broken.
- Apps whose assets live on a remote CDN rather than in the folder cannot be
  made self-contained; the builder reports them under `missingRefs`.
- Flash games need a Ruffle runtime bundled; not handled yet.
- `defer`/`async` scripts become ordinary inline scripts. Order is preserved,
  which is what loaders actually depend on, but the note is recorded.

## Verifying

`verify.mjs` drives Edge or Chrome over CDP with no dependencies (Node 22+
for native `WebSocket`). It loads the port **and the original**, then reports
only problems the original did not already have — most games log warnings of
their own, and flagging those makes the report useless.

A port passes when it has no new errors, no new exceptions, no failed
requests, and **no sub-requests at all**.

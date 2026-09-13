# WATHB — Web App To HTML Builder

Turns a web game/app folder into one self-contained `.html` that resolves every
path it asks for, makes zero network requests, and works inside a blob tab.

Built for **Red Portal** (`github.com/VVVULTURE/red-portal-DKR-LCL`). 70 of its
games are currently served as single files produced by this tool.

## Read these first

| Where | What |
| --- | --- |
| `README.md` (here) | how the builder and runtime work, and why each piece exists |
| Red Portal's `docs/SINGLE-FILE-PORTS.md` | every defect class found, the measured limits, and what "verified" really means |
| Red Portal's `docs/SESSION-HISTORY.md` | the site itself: architecture, bug ledger, runbook |

Those two live in the Red Portal repo, not this one. Keep them updated when
this pipeline changes — that rule is written into them.

## The five things that matter

1. **A blob: document has no resolvable base.** `new URL('.', document.baseURI)`
   THROWS there, and Red Portal runs every game in a blob tab. Anything
   assuming a base dies before installing a single hook and takes the app with
   it. Paths resolve lexically for this reason.
2. **Verify in a blob tab, never over `http://`.** Different URL context.
   Testing over http passed games that were broken in production, twice.
   `verify-all.py` uses `blob-harness.html`, which reproduces the real launch.
3. **Two layers, neither sufficient alone.** References already in the markup
   cannot be caught at runtime — the parser sets them before any script runs —
   so `build.py` rewrites those and `rpvfs.js` catches everything dynamic.
4. **A missing file returns a synthetic success, not a 404.** That is what
   keeps the console clean. `--loud` disables it while debugging.
5. **"Passed" means "no worse than the original", not "works."** A game can sit
   on a menu forever with a spotless console.

## Hard limit

V8 caps a single JS string at 512 MiB, so base64 carries at most **384 MiB** of
assets. Eight Red Portal games exceed it and can never be single files. The
builder refuses rather than emitting something broken.

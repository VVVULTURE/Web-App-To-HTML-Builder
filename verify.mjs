/* WATHB verifier — headless acceptance test for a port.

   A port is good when, compared with the original, it introduces no new
   console errors, no new exceptions, and makes NO network requests at all.
   The comparison matters: most games already log warnings of their own, and
   flagging those as port defects makes the report useless.

   One browser is launched for the whole job list; a fresh tab per check.

   usage:
     node verify.mjs --url <url> [--wait ms]
     node verify.mjs --jobs jobs.json      // [{name, portUrl, baseUrl?, wait?}]
*/
import { spawn } from 'node:child_process';
import { mkdtempSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const FULL = argv.includes('--full');

const EXES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];
const EXE = EXES.find((p) => existsSync(p));
if (!EXE) { console.error('no chromium browser found'); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PORT = 9000 + Math.floor(Math.random() * 900);
const profile = mkdtempSync(join(tmpdir(), 'wathb-'));
const proc = spawn(EXE, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check', '--mute-audio',
  '--disable-extensions', '--disable-component-extensions-with-background-pages',
  '--disable-features=Translate,AutofillServerCommunication',
  '--use-gl=swiftshader', '--enable-unsafe-swiftshader',
  '--autoplay-policy=no-user-gesture-required',
  '--js-flags=--max-old-space-size=8192',
  '--window-size=1280,800',
], { stdio: 'ignore' });

let wsu;
for (let i = 0; i < 120 && !wsu; i++) {
  try { wsu = (await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()).webSocketDebuggerUrl; }
  catch { await sleep(250); }
}
if (!wsu) { console.error('devtools never came up'); proc.kill(); process.exit(2); }

const ws = new WebSocket(wsu);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
const sessions = new Map();          // sessionId -> event array
ws.onmessage = (m) => {
  const j = JSON.parse(m.data);
  if (j.id && pending.has(j.id)) { pending.get(j.id)(j); pending.delete(j.id); return; }
  if (j.method && j.sessionId && sessions.has(j.sessionId)) sessions.get(j.sessionId).push(j);
};
// Every CDP call is bounded. A renderer that dies under a 500 MB page never
// answers, and an unbounded await there hangs the whole pool: node exits with
// "Detected unsettled top-level await" and the report is never written.
const CDP_TIMEOUT = Number(arg('--cdp-timeout', 120000));
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const n = ++id;
  const timer = setTimeout(() => {
    if (pending.has(n)) { pending.delete(n); res({ timedOut: true, result: {} }); }
  }, CDP_TIMEOUT);
  pending.set(n, (msg) => { clearTimeout(timer); res(msg); });
  try {
    ws.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
  } catch (e) {
    clearTimeout(timer); pending.delete(n); res({ error: String(e), result: {} });
  }
});

async function check(url, waitMs, ignore) {
  const created = await send('Target.createTarget', { url: 'about:blank' });
  const targetId = created.result && created.result.targetId;
  if (!targetId) throw new Error('could not create target (browser may have died)');
  const attached = await send('Target.attachToTarget', { targetId, flatten: true });
  const sessionId = attached.result && attached.result.sessionId;
  if (!sessionId) { await send('Target.closeTarget', { targetId }); throw new Error('could not attach'); }
  const ev = []; sessions.set(sessionId, ev);
  await send('Runtime.enable', {}, sessionId);
  await send('Log.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  await send('Page.enable', {}, sessionId);
  await send('Page.navigate', { url }, sessionId);
  await sleep(waitMs);

  const errors = [], exceptions = [], netFail = [], reqs = [];
  for (const e of ev) {
    const p = e.params;
    if (e.method === 'Runtime.consoleAPICalled' && p.type === 'error') {
      const t = (p.args || []).map((a) => a.value ?? a.description ?? '?').join(' ');
      if (!t.includes('chrome-extension://')) errors.push(t.slice(0, 300));
    }
    if (e.method === 'Runtime.exceptionThrown') {
      const t = (p.exceptionDetails?.exception?.description
        || p.exceptionDetails?.text || '');
      // extensions injected into the page are not the port's doing
      if (!t.includes('chrome-extension://')) {
        // --full keeps the stack: the first line names the error but almost
        // never says which code provoked it
        exceptions.push(FULL ? t.slice(0, 2000) : t.split('\n')[0].slice(0, 300));
      }
    }
    if (e.method === 'Log.entryAdded' && p.entry.level === 'error') {
      if (!p.entry.text.includes('chrome-extension://')) errors.push(p.entry.text.slice(0, 300));
    }
    if (e.method === 'Network.requestWillBeSent') reqs.push(p.request.url);
    if (e.method === 'Network.loadingFailed' && !p.canceled) netFail.push(p.errorText);
    if (e.method === 'Network.responseReceived' && p.response.status >= 400) {
      netFail.push(`HTTP ${p.response.status} ${p.response.url}`);
    }
  }
  // Visible text matters as much as the error list: a port can load with a
  // completely clean console and still sit forever on "Downloading...",
  // which is precisely what a defect looks like to a player.
  const probe = await send('Runtime.evaluate', {
    expression: `JSON.stringify({title:document.title,
      body:document.body?document.body.innerHTML.length:0,
      text:(document.body?document.body.innerText||'':'').replace(/\\s+/g,' ').trim().slice(0,300),
      canvas:(function(){var c=document.querySelector('canvas');
        return c?{w:c.width,h:c.height}:null})()})`,
    returnByValue: true,
  }, sessionId);

  sessions.delete(sessionId);
  await send('Target.closeTarget', { targetId });

  // Compare decoded: a game whose name contains an apostrophe arrives as
  // Baldi%27s and silently fails an exact match, which reports the harness's
  // own fetch as though the port had reached the network.
  const dec = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
  const ignores = (ignore || []).map(dec);
  const selfUrl = dec(url);
  const sub = [...new Set(reqs)].filter((u) => {
    if (u.startsWith('data:') || u.startsWith('blob:')) return false;
    const d = dec(u);
    if (d === selfUrl || d.split('?')[0] === selfUrl.split('?')[0]) return false;
    // the blob harness fetches the file under test; that is the harness, not
    // the port, and it does not exist once Red Portal serves the html itself
    return !ignores.some((ig) => d === ig || d.split('?')[0] === ig);
  });
  let info = {};
  try { info = JSON.parse(probe.result?.result?.value || '{}'); } catch { }
  return {
    errors: [...new Set(errors)], exceptions: [...new Set(exceptions)],
    netFail: [...new Set(netFail)], subRequests: sub,
    totalRequests: reqs.length, ...info,
  };
}

const norm = (s) => s.replace(/https?:\/\/[^\s)]+/g, '<url>')
  .replace(/blob:[^\s)]+/g, '<blob>').replace(/\d+/g, 'N').trim();

const jobsFile = arg('--jobs');
const results = [];
if (jobsFile) {
  let jobs = JSON.parse(readFileSync(jobsFile, 'utf8'));
  // A single browser cannot survive the whole set: these pages run to half a
  // gigabyte each and it dies of memory exhaustion somewhere around job 70,
  // after which every remaining job fails for a reason that has nothing to do
  // with the game. Run a slice per process and let the driver recycle it.
  const slice = arg('--slice');
  if (slice) {
    const [a0, b0] = slice.split(':').map(Number);
    jobs = jobs.slice(a0, b0);
  }
  const CONC = Number(arg('--concurrency', 4));

  // Baselines describe the ORIGINAL game, which does not change between
  // runs. Caching them turns every re-verify into half the work — and
  // re-verifying is the common case while fixing a class of failures.
  const cachePath = arg('--baseline-cache', 'baseline-cache.json');
  let baseCache = {};
  try { baseCache = JSON.parse(readFileSync(cachePath, 'utf8')); } catch { }

  let n = 0;
  const runJob = async (j) => {
    const port = await check(j.portUrl, j.wait || 9000, j.ignore);
    let base = null;
    if (j.baseUrl) {
      if (baseCache[j.baseUrl]) base = baseCache[j.baseUrl];
      else {
        base = await check(j.baseUrl, j.wait || 9000, j.ignore);
        baseCache[j.baseUrl] = base;
      }
    }

    // only count problems the original did not already have
    const known = new Set([...(base?.errors || []), ...(base?.exceptions || [])].map(norm));
    const newErrors = port.errors.filter((e) => !known.has(norm(e)));
    const newExc = port.exceptions.filter((e) => !known.has(norm(e)));

    // Stuck-loader detection. A clean console is not proof the app runs:
    // Balatro sat on "Downloading..." with zero errors and zero requests
    // because an inlined script's onload never fired. If the port still shows
    // loader text that the original has moved past, it is broken.
    const LOADER = /\b(downloading|loading|preparing|please wait|initialising|initializing)\b/i;
    const stuck = !!(port.text && LOADER.test(port.text)
      && !(base && base.text && LOADER.test(base.text)));

    const pass = !newErrors.length && !newExc.length
      && !port.netFail.length && !port.subRequests.length && !stuck;

    results.push({ name: j.name, pass, stuck, newErrors, newExceptions: newExc,
      netFail: port.netFail, subRequests: port.subRequests,
      port: { title: port.title, body: port.body, canvas: port.canvas, text: port.text },
      base: base && { title: base.title, body: base.body, canvas: base.canvas,
        text: base.text, requests: base.totalRequests } });
    n++;
    console.log(`[${String(n).padStart(3)}/${jobs.length}] ${pass ? 'PASS' : 'FAIL'}  ${j.name}`
      + (pass ? '' : `  (${newErrors.length + newExc.length} new err, `
        + `${port.netFail.length} netfail, ${port.subRequests.length} requests`
        + `${stuck ? ', STUCK ON LOADER' : ''})`));
  };

  // Simple pool. Each check gets its own target and session, so they are
  // already independent; the only shared resource is browser memory, which
  // is why this is a handful of workers and not one per job.
  const out = arg('--out', 'verify-report.json');
  // Flush after every job. A run that dies at 67 of 95 should still leave a
  // usable report behind -- losing the whole thing to one crashed renderer
  // costs another full pass.
  const flush = () => {
    try {
      const sorted = results.slice().sort((a, b) => a.name.localeCompare(b.name));
      writeFileSync(out, JSON.stringify(sorted, null, 2));
      writeFileSync(cachePath, JSON.stringify(baseCache));
    } catch { /* keep going; the console log is still the record */ }
  };

  const JOB_TIMEOUT = Number(arg('--job-timeout', 180000));
  const withTimeout = (p, ms, label) => Promise.race([
    p,
    new Promise((_r, rej) => setTimeout(() => rej(new Error('timed out: ' + label)), ms)),
  ]);

  const queue = jobs.slice();
  await Promise.all(Array.from({ length: Math.min(CONC, queue.length) }, async () => {
    while (queue.length) {
      const j = queue.shift();
      try { await withTimeout(runJob(j), JOB_TIMEOUT, j.name); }
      catch (e) {
        n++;
        results.push({ name: j.name, pass: false, harnessError: String(e).slice(0, 200) });
        console.log(`[${String(n).padStart(3)}/${jobs.length}] FAIL  ${j.name}  (harness: ${String(e).slice(0, 80)})`);
      }
      flush();
    }
  }));

  flush();
  const passed = results.filter((r) => r.pass).length;
  console.log(`\n${passed}/${results.length} passed  ->  ${out}`);
} else {
  const r = await check(arg('--url'), Number(arg('--wait', 9000)), (arg('--ignore','')||'').split(',').filter(Boolean));
  console.log(JSON.stringify(r, null, 2));
}

ws.close(); proc.kill();
process.exit(0);

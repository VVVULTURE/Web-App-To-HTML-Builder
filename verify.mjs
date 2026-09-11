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
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const n = ++id; pending.set(n, res);
  ws.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
});

async function check(url, waitMs) {
  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
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
      if (!t.includes('chrome-extension://')) exceptions.push(t.split('\n')[0].slice(0, 300));
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
  const probe = await send('Runtime.evaluate', {
    expression: `JSON.stringify({title:document.title,
      body:document.body?document.body.innerHTML.length:0,
      canvas:(function(){var c=document.querySelector('canvas');
        return c?{w:c.width,h:c.height}:null})()})`,
    returnByValue: true,
  }, sessionId);

  sessions.delete(sessionId);
  await send('Target.closeTarget', { targetId });

  const self = new URL(url);
  const sub = [...new Set(reqs)].filter((u) => {
    if (u.startsWith('data:') || u.startsWith('blob:')) return false;
    return u !== url;
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
  const jobs = JSON.parse(readFileSync(jobsFile, 'utf8'));
  let n = 0;
  for (const j of jobs) {
    n++;
    const port = await check(j.portUrl, j.wait || 9000);
    let base = null;
    if (j.baseUrl) base = await check(j.baseUrl, j.wait || 9000);

    // only count problems the original did not already have
    const known = new Set([...(base?.errors || []), ...(base?.exceptions || [])].map(norm));
    const newErrors = port.errors.filter((e) => !known.has(norm(e)));
    const newExc = port.exceptions.filter((e) => !known.has(norm(e)));
    const pass = !newErrors.length && !newExc.length
      && !port.netFail.length && !port.subRequests.length;

    results.push({ name: j.name, pass, newErrors, newExceptions: newExc,
      netFail: port.netFail, subRequests: port.subRequests,
      port: { title: port.title, body: port.body, canvas: port.canvas },
      base: base && { title: base.title, body: base.body, canvas: base.canvas,
        requests: base.totalRequests } });
    console.log(`[${String(n).padStart(3)}/${jobs.length}] ${pass ? 'PASS' : 'FAIL'}  ${j.name}`
      + (pass ? '' : `  (${newErrors.length + newExc.length} new err, `
        + `${port.netFail.length} netfail, ${port.subRequests.length} requests)`));
  }
  const out = arg('--out', 'verify-report.json');
  writeFileSync(out, JSON.stringify(results, null, 2));
  const passed = results.filter((r) => r.pass).length;
  console.log(`\n${passed}/${results.length} passed  ->  ${out}`);
} else {
  const r = await check(arg('--url'), Number(arg('--wait', 9000)));
  console.log(JSON.stringify(r, null, 2));
}

ws.close(); proc.kill();
process.exit(0);

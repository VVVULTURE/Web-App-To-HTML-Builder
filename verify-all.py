# -*- coding: utf-8 -*-
"""Run the verifier over a whole job list in slices, one browser per slice.

A single Chromium cannot survive the full set: the pages run to half a
gigabyte each, and it dies of memory exhaustion around job 70. Everything
after that point then fails with "could not create target", which looks like
a game defect and is not one. A fresh process per slice keeps memory bounded
and makes the run reproducible.

    python verify-all.py --built <dir> [--size 10] [--concurrency 3]
"""
import argparse, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--built', default=r'C:\Stuff\WATHB-built')
    ap.add_argument('--jobs', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--size', type=int, default=10, help='jobs per browser')
    ap.add_argument('--concurrency', type=int, default=3)
    a = ap.parse_args()

    jobs_path = a.jobs or os.path.join(a.built, 'jobs.json')
    out_path = a.out or os.path.join(a.built, 'verify-report.json')
    jobs = json.load(open(jobs_path, encoding='utf-8'))
    print('%d jobs, %d per browser, concurrency %d\n' % (len(jobs), a.size, a.concurrency))

    merged, t0 = [], time.time()
    for start in range(0, len(jobs), a.size):
        end = min(start + a.size, len(jobs))
        part = os.path.join(a.built, 'verify-part-%03d.json' % start)
        cmd = [
            'node', os.path.join(HERE, 'verify.mjs'),
            '--jobs', jobs_path, '--out', part,
            '--slice', '%d:%d' % (start, end),
            '--concurrency', str(a.concurrency),
            '--baseline-cache', os.path.join(a.built, 'baseline-cache.json'),
        ]
        print('--- jobs %d-%d ---' % (start, end - 1))
        try:
            subprocess.run(cmd, cwd=a.built, timeout=60 * 30)
        except subprocess.TimeoutExpired:
            print('    slice timed out, moving on')
        if os.path.exists(part):
            try:
                merged.extend(json.load(open(part, encoding='utf-8')))
            except Exception as e:
                print('    unreadable part: %s' % e)
            os.remove(part)
        # give the OS a moment to reclaim the browser's memory
        time.sleep(2)

    merged.sort(key=lambda x: x['name'])
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, indent=2)

    ok = [x for x in merged if x.get('pass')]
    print('\n%d/%d passed in %.1f min -> %s'
          % (len(ok), len(merged), (time.time() - t0) / 60, out_path))
    bad = [x for x in merged if not x.get('pass')]
    if bad:
        print('\nfailures:')
        for x in bad:
            why = []
            if x.get('harnessError'):
                why.append('harness: ' + x['harnessError'][:60])
            if x.get('stuck'):
                why.append('STUCK ON LOADER')
            n = len(x.get('newErrors') or []) + len(x.get('newExceptions') or [])
            if n:
                why.append('%d new err' % n)
            if x.get('subRequests'):
                why.append('%d requests' % len(x['subRequests']))
            print('  %-36s %s' % (x['name'], '; '.join(why) or 'unknown'))


if __name__ == '__main__':
    main()

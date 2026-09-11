# -*- coding: utf-8 -*-
"""Build every compatible app under a Red Portal-shaped root.

Walks <root>/<tab>/<game>/ for the given tabs, builds each one, and writes a
jobs file for verify.mjs so every port can be checked against its original.

usage:
    python batch.py <root> -o <outDir> [--tabs Games Testing Apps]
                    [--max-mb N] [--only NAME ...] [--jobs jobs.json]
"""
import argparse, json, os, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build import Builder, find_entry, HARD_LIMIT          # noqa: E402


def dir_size(p):
    t = 0
    for dp, _d, ns in os.walk(p):
        for n in ns:
            try:
                t += os.path.getsize(os.path.join(dp, n))
            except OSError:
                pass
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--tabs', nargs='*', default=['Games', 'Testing', 'Apps'])
    ap.add_argument('--max-mb', type=float, default=HARD_LIMIT / 1048576)
    ap.add_argument('--only', nargs='*', default=None)
    ap.add_argument('--jobs', default=None)
    ap.add_argument('--base-url', default='http://127.0.0.1:8801')
    ap.add_argument('--port-url', default='http://127.0.0.1:8802')
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    max_bytes = int(a.max_mb * 1048576)

    targets = []
    for tab in a.tabs:
        tp = os.path.join(a.root, tab)
        if not os.path.isdir(tp):
            continue
        for name in sorted(os.listdir(tp)):
            gp = os.path.join(tp, name)
            if os.path.isdir(gp) and (not a.only or name in a.only):
                targets.append((tab, name, gp))

    print('%d candidate folders\n' % len(targets))
    built, skipped, failed, jobs = [], [], [], []

    for i, (tab, name, gp) in enumerate(targets, 1):
        label = '%s/%s' % (tab, name)
        print('[%3d/%d] %s' % (i, len(targets), label))
        size = dir_size(gp)

        # Size is checked FIRST: constructing a Builder reads every file into
        # memory, and the largest folders here are multiple GB.
        if size > max_bytes:
            msg = ('%.0f MiB of assets exceeds the %.0f MiB single-file ceiling'
                   % (size / 1048576, max_bytes / 1048576))
            print('        SKIP  %s' % msg)
            skipped.append({'game': label, 'reason': msg, 'bytes': size})
            continue

        try:
            b = Builder(gp, verbose=False)
        except SystemExit as e:
            print('        SKIP  %s' % e)
            skipped.append({'game': label, 'reason': str(e), 'bytes': size})
            continue
        except MemoryError:
            print('        SKIP  out of memory reading the folder')
            skipped.append({'game': label, 'reason': 'MemoryError', 'bytes': size})
            continue
        except Exception as e:
            print('        FAIL  %s' % e)
            failed.append({'game': label, 'error': repr(e)})
            continue

        safe = name.replace(' ', '_').replace('+', 'plus')
        outfile = os.path.join(a.out, '%s__%s.html' % (tab, safe))
        t0 = time.time()
        try:
            stats = b.run(outfile, max_bytes=max_bytes)
        except SystemExit as e:
            print('        SKIP  %s' % str(e).split('\n')[0])
            skipped.append({'game': label, 'reason': str(e).split('\n')[0], 'bytes': size})
            continue
        except Exception as e:
            print('        FAIL  %s' % e)
            failed.append({'game': label, 'error': traceback.format_exc(limit=2)})
            continue

        dt = time.time() - t0
        print('        ok    %.1f MiB -> %.1f MiB  (%.1fs)%s'
              % (size / 1048576, stats['outBytes'] / 1048576, dt,
                 '  [%d missing refs]' % len(stats['missingRefs'])
                 if stats['missingRefs'] else ''))
        stats['game'] = label
        stats['outFile'] = os.path.basename(outfile)
        stats['sourceBytes'] = size
        stats['buildSeconds'] = round(dt, 1)
        built.append(stats)

        entry_url = '/'.join([a.base_url, tab, name, b.entry])
        fileurl = '%s/%s' % (a.port_url, os.path.basename(outfile))
        # Verify through the blob harness, because that is how Red Portal
        # actually launches a game. Serving the file over http:// exercises a
        # different URL context entirely and hides every base-resolution bug.
        jobs.append({
            'name': label,
            'portUrl': '%s/blob-harness.html?f=%s' % (a.port_url, os.path.basename(outfile)),
            'baseUrl': entry_url.replace(' ', '%20'),
            'ignore': [fileurl],
            'wait': 12000 if size < 40 * 1048576 else 24000,
        })

    report = {'built': built, 'skipped': skipped, 'failed': failed}
    with open(os.path.join(a.out, 'build-report.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    jobs_path = a.jobs or os.path.join(a.out, 'jobs.json')
    with open(jobs_path, 'w', encoding='utf-8') as fh:
        json.dump(jobs, fh, indent=2)

    print('\n%d built, %d skipped, %d failed' % (len(built), len(skipped), len(failed)))
    if built:
        tot_in = sum(b['sourceBytes'] for b in built)
        tot_out = sum(b['outBytes'] for b in built)
        print('total %.1f MiB source -> %.1f MiB of html'
              % (tot_in / 1048576, tot_out / 1048576))
    print('report -> %s' % os.path.join(a.out, 'build-report.json'))
    print('jobs   -> %s' % jobs_path)


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""Group verification failures by root signature.

Failures cluster: one builder defect shows up as the same message across a
dozen games. Reading them game-by-game hides that; grouping makes the next
fix obvious and shows how many games it buys.

    python triage.py [--built <dir>]
"""
import argparse, collections, json, os, re


def signature(msg):
    """Collapse a message to its shape so instances group together."""
    s = msg.split('\n')[0]
    s = re.sub(r'blob:[^\s)]+', '<blob>', s)
    s = re.sub(r'https?://[^\s)]+', '<url>', s)
    s = re.sub(r"'[^']{1,60}'", "'X'", s)
    s = re.sub(r'\d+', 'N', s)
    return s.strip()[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--built', default=r'C:\Stuff\WATHB-built')
    a = ap.parse_args()
    r = json.load(open(os.path.join(a.built, 'verify-report.json'), encoding='utf-8'))

    ok = [x for x in r if x.get('pass')]
    bad = [x for x in r if not x.get('pass')]
    print('%d verified, %d passed, %d failed\n' % (len(r), len(ok), len(bad)))

    groups = collections.defaultdict(list)
    for x in bad:
        keys = set()
        for e in (x.get('newErrors') or []) + (x.get('newExceptions') or []):
            keys.add(signature(e))
        if x.get('stuck'):
            keys.add('<STUCK ON LOADER>')
        for u in (x.get('subRequests') or []):
            keys.add('<NETWORK REQUEST> ' + signature(u))
        if x.get('harnessError'):
            keys.add('<HARNESS> ' + signature(x['harnessError']))
        if not keys:
            keys.add('<no signature recorded>')
        for k in keys:
            groups[k].append(x['name'])

    print('failure groups, most games first:\n')
    for sig, games in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print('  [%2d] %s' % (len(games), sig))
        for g in sorted(games)[:6]:
            print('         %s' % g)
        if len(games) > 6:
            print('         ... and %d more' % (len(games) - 6))
        print()

    net = [x['name'] for x in bad if x.get('subRequests')]
    stuck = [x['name'] for x in bad if x.get('stuck')]
    print('games still reaching the network: %s' % (', '.join(net) or 'none'))
    print('games stuck on a loader        : %s' % (', '.join(stuck) or 'none'))


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""Swap Red Portal game folders for their single-file ports, reversibly.

For each game that PASSED verification:

    Games/Foo/**            ->  moved to <backup>/Games/Foo/**
    Games/Foo/index.html    <-  the built single .html

The original folder is MOVED, not copied: on the same volume that is instant
and costs no extra disk, and it keeps a complete copy for rollback. Every
move is recorded in a manifest so `--restore` can put it all back exactly.

Nothing is touched unless --apply is given, and by default only games that
passed verification are considered.

    python replace.py --built <dir> --root <redportal folder>        # dry run
    python replace.py --built <dir> --root <...> --apply
    python replace.py --built <dir> --root <...> --restore --apply
"""
import argparse, json, os, shutil, sys, time

MANIFEST = 'wathb-replacement.json'


def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.1f %s' % (n, u)
        n /= 1024.0
    return '%.1f TB' % n


def dir_size(p):
    t = 0
    for dp, _d, ns in os.walk(p):
        for n in ns:
            try:
                t += os.path.getsize(os.path.join(dp, n))
            except OSError:
                pass
    return t


def do_replace(a):
    report = json.load(open(os.path.join(a.built, 'build-report.json'), encoding='utf-8'))
    built = {b['game']: b for b in report['built']}

    passed = None
    vpath = os.path.join(a.built, 'verify-report.json')
    if os.path.exists(vpath):
        v = json.load(open(vpath, encoding='utf-8'))
        passed = {x['name'] for x in v if x.get('pass')}
        print('verification: %d of %d games passed' % (len(passed), len(v)))
    elif not a.include_failed:
        raise SystemExit('no verify-report.json in %s -- run the verifier first, '
                         'or pass --include-failed to replace regardless' % a.built)

    plan = []
    for game, b in sorted(built.items()):
        if passed is not None and not a.include_failed and game not in passed:
            continue
        folder = os.path.join(a.root, *game.split('/'))
        src = os.path.join(a.built, b['outFile'])
        if not os.path.isdir(folder):
            print('  skip (folder gone): %s' % game)
            continue
        if not os.path.exists(src):
            print('  skip (build missing): %s' % game)
            continue
        plan.append((game, folder, src, b['outBytes']))

    if not plan:
        raise SystemExit('nothing to do')

    old_total = sum(dir_size(f) for _g, f, _s, _n in plan)
    new_total = sum(n for _g, _f, _s, n in plan)
    print('\n%d games to replace' % len(plan))
    print('  originals moved to backup : %s' % human(old_total))
    print('  single files written      : %s' % human(new_total))
    print('  backup directory          : %s' % a.backup)
    for game, folder, _src, n in plan[:10]:
        print('    %-36s -> one file, %s' % (game, human(n)))
    if len(plan) > 10:
        print('    ... and %d more' % (len(plan) - 10))

    if not a.apply:
        print('\nDRY RUN -- nothing changed. Re-run with --apply to do it.')
        return

    os.makedirs(a.backup, exist_ok=True)
    moved = []
    for i, (game, folder, src, _n) in enumerate(plan, 1):
        dest = os.path.join(a.backup, *game.split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            raise SystemExit('backup already exists, refusing to overwrite: %s' % dest)
        shutil.move(folder, dest)
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(src, os.path.join(folder, 'index.html'))
        moved.append({'game': game, 'folder': folder, 'backup': dest,
                      'built': os.path.basename(src)})
        print('[%3d/%d] %s' % (i, len(plan), game))

    man = {'when': time.strftime('%Y-%m-%d %H:%M:%S'), 'root': a.root,
           'backup': a.backup, 'moved': moved}
    with open(os.path.join(a.backup, MANIFEST), 'w', encoding='utf-8') as fh:
        json.dump(man, fh, indent=2)
    print('\nreplaced %d games. manifest -> %s'
          % (len(moved), os.path.join(a.backup, MANIFEST)))
    print('restore with:  python replace.py --restore --apply --backup "%s"' % a.backup)


def do_restore(a):
    mpath = os.path.join(a.backup, MANIFEST)
    if not os.path.exists(mpath):
        raise SystemExit('no manifest at %s' % mpath)
    man = json.load(open(mpath, encoding='utf-8'))
    print('restoring %d games recorded %s' % (len(man['moved']), man['when']))
    if not a.apply:
        for m in man['moved'][:10]:
            print('  %s  <-  %s' % (m['game'], m['backup']))
        print('\nDRY RUN -- nothing changed. Re-run with --apply.')
        return
    for i, m in enumerate(man['moved'], 1):
        folder, backup = m['folder'], m['backup']
        if not os.path.isdir(backup):
            print('  MISSING backup, skipping: %s' % m['game'])
            continue
        if os.path.isdir(folder):
            shutil.rmtree(folder)          # this is the single-file version
        shutil.move(backup, folder)
        print('[%3d/%d] %s' % (i, len(man['moved']), m['game']))
    os.remove(mpath)
    print('\nrestored. the backup directory is now empty of tracked games.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--built', default=r'C:\Stuff\WATHB-built')
    ap.add_argument('--root', default=r'C:\Stuff\RedTesting\red-portal-DKR-LCL-main')
    ap.add_argument('--backup', default=r'C:\Stuff\WATHB-replaced-originals')
    ap.add_argument('--apply', action='store_true', help='actually do it')
    ap.add_argument('--restore', action='store_true', help='put the originals back')
    ap.add_argument('--include-failed', action='store_true',
                    help='replace games that did not pass verification too')
    a = ap.parse_args()
    (do_restore if a.restore else do_replace)(a)


if __name__ == '__main__':
    main()

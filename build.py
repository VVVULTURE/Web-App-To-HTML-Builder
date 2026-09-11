# -*- coding: utf-8 -*-
"""WATHB — Web App To HTML Builder.

Turns a web app / game folder into ONE self-contained .html file that still
resolves every path the app asks for, with no network requests and no errors.

Two layers, because neither alone is sufficient:

  BUILD TIME (this file)  rewrites references that already exist in the
      markup. The HTML parser sets those attributes internally and starts
      the request before any script can run, so no runtime hook can see
      them. Scripts are inlined, CSS is flattened with url() resolved
      relative to each stylesheet, small media become data URIs and large
      media become data-wathb-* attributes.

  RUN TIME (rpvfs.js)  catches everything created dynamically: fetch, XHR,
      `img.src =`, injected <script>, Workers, WebAssembly streaming.

usage:
    python build.py <folder> [-o out.html] [--entry rel/path.html]
                    [--log] [--loud] [--max-mb N] [--quiet-build]
"""
import argparse, base64, json, os, posixpath, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, 'rpvfs.js')

# V8 caps a single JS string at 512 MiB, so one base64 chunk cannot exceed
# that. We chunk well below it; the real ceiling on a port is memory.
B64_CHUNK = 48 * 1024 * 1024
HARD_LIMIT = 384 * 1024 * 1024          # 384 MiB of assets -> 512 MiB base64
INLINE_MAX = 256 * 1024                 # bigger static refs hydrate instead

MIME = {
    '.html': 'text/html', '.htm': 'text/html', '.js': 'text/javascript',
    '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.webp': 'image/webp', '.avif': 'image/avif', '.bmp': 'image/bmp',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
    '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject',
    '.wasm': 'application/wasm', '.data': 'application/octet-stream',
    '.unityweb': 'application/octet-stream', '.mem': 'application/octet-stream',
    '.symbols': 'application/octet-stream', '.bundle': 'application/octet-stream',
    '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
    '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.mp4': 'video/mp4',
    '.webm': 'video/webm', '.txt': 'text/plain', '.xml': 'text/xml',
    '.csv': 'text/csv', '.pck': 'application/octet-stream',
    '.atlas': 'text/plain', '.skel': 'application/octet-stream',
    '.swf': 'application/x-shockwave-flash',
}
EXTERNAL = ('http://', 'https://', '//', 'data:', 'blob:', 'javascript:',
            'mailto:', '#', 'about:', 'ws:', 'wss:')
REMOTE = ('http://', 'https://', '//')

# A 1x1 transparent PNG. Substituted for any remote image the file cannot
# carry: it occupies the same slot, renders nothing, and — the point —
# issues no request. CSS url() is the case that forces this, because
# stylesheets are not interceptable at runtime the way elements are.
BLANK_PNG = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf'
             'FcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')


def is_remote(ref):
    return bool(ref) and ref.strip().strip('\'"').lower().startswith(REMOTE)


def blank_for(ref):
    """A substitute of the right *kind* for a remote reference we cannot keep.

    A transparent PNG standing in for a font makes the browser log a decode
    failure, which is the sort of console noise this whole tool exists to
    avoid — so the substitute has to match the extension.
    """
    ext = (str(ref).split('?')[0].split('#')[0].rsplit('.', 1) + [''])[1].lower()
    if ext in ('woff', 'woff2', 'ttf', 'otf', 'eot'):
        return 'data:font/woff2;base64,'
    if ext in ('mp3', 'wav', 'ogg', 'm4a', 'aac'):
        return ('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhY'
                'AQACABAAZGF0YQAAAAA=')
    if ext in ('mp4', 'webm'):
        return 'data:video/mp4;base64,'
    if ext in ('css',):
        return 'data:text/css,'
    if ext in ('js', 'mjs'):
        return 'data:text/javascript,'
    return BLANK_PNG


def mime_of(p):
    return MIME.get(os.path.splitext(p)[1].lower(), 'application/octet-stream')


def find_entry(vfs):
    """Shallowest index.html wins — the same rule Red Portal's discovery uses.

    Ties break alphabetically, so the result is deterministic.
    """
    cands = [k for k in vfs if k.lower().endswith('index.html')]
    if not cands:
        cands = [k for k in vfs if k.lower().endswith(('.html', '.htm'))]
    if not cands:
        return None
    cands.sort(key=lambda k: (k.count('/'), k.lower()))
    return cands[0]


class Builder(object):
    def __init__(self, root, entry=None, log=False, quiet=True, verbose=True):
        self.root = os.path.abspath(root)
        self.log = log
        self.quiet = quiet
        self.verbose = verbose
        self.vfs = {}
        self.inlined = set()
        self.missing = []          # refs the app names that do not exist
        self.notes = []
        self.remote_blanked = 0    # remote refs replaced so they cannot fire

        for dirpath, _d, names in os.walk(self.root):
            for n in names:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, self.root).replace(os.sep, '/')
                try:
                    with open(full, 'rb') as fh:
                        self.vfs[rel] = fh.read()
                except (OSError, IOError) as e:
                    self.notes.append('unreadable: %s (%s)' % (rel, e))

        self.entry = entry or find_entry(self.vfs)
        if not self.entry or self.entry not in self.vfs:
            raise SystemExit('no entry .html found in %s' % self.root)
        self.entry_dir = posixpath.dirname(self.entry)

    # ── reference resolution ──────────────────────────────────────────
    def norm(self, base_dir, ref):
        if not ref:
            return None
        ref = ref.strip().strip('\'"')
        if not ref or ref.lower().startswith(EXTERNAL):
            return None
        bare = ref.split('#')[0].split('?')[0].replace('\\', '/')
        if not bare:
            return None
        key = (posixpath.normpath(bare.lstrip('/')) if bare.startswith('/')
               else posixpath.normpath(posixpath.join(base_dir, bare)))
        if key.startswith('./'):
            key = key[2:]
        if key in self.vfs:
            return key
        self.missing.append(ref)
        return None

    def data_uri(self, key):
        return 'data:%s;base64,%s' % (
            mime_of(key), base64.b64encode(self.vfs[key]).decode('ascii'))

    # ── CSS ───────────────────────────────────────────────────────────
    CSS_URL = re.compile(r'url\(\s*([^)]+?)\s*\)', re.I)
    CSS_IMPORT = re.compile(
        r'@import\s+(?:url\(\s*([^)]+?)\s*\)|([\'"][^\'"]+[\'"]))[^;]*;', re.I)

    def inline_css(self, key, seen=None):
        seen = (seen or set()) | {key}
        self.inlined.add(key)
        css = self.vfs[key].decode('utf-8', 'replace')
        d = posixpath.dirname(key)

        def do_import(m):
            k = self.norm(d, m.group(1) or m.group(2) or '')
            if k is None:
                return m.group(0)
            if k in seen:
                return '/* wathb: circular @import dropped: %s */' % k
            return self.inline_css(k, seen)

        def do_url(m):
            ref = m.group(1)
            k = self.norm(d, ref)
            if k is None:
                # A stylesheet is not interceptable at runtime, so a remote
                # url() would fire a real request no matter what the shim
                # does. Blank it here or it never becomes self-contained.
                if is_remote(ref):
                    self.remote_blanked += 1
                    return 'url("%s")' % blank_for(ref)
                return m.group(0)
            self.inlined.add(k)
            return 'url("%s")' % self.data_uri(k)

        return self.CSS_URL.sub(do_url, self.CSS_IMPORT.sub(do_import, css))

    # ── HTML ──────────────────────────────────────────────────────────
    def build_html(self):
        html = self.vfs[self.entry].decode('utf-8', 'replace')
        ed = self.entry_dir

        # A <base> inside the document would re-point every relative URL and
        # break the mapping the runtime relies on.
        if re.search(r'<base\b[^>]*href', html, re.I):
            html = re.sub(r'<base\b[^>]*>', '<!-- wathb: <base> removed -->',
                          html, flags=re.I)
            self.notes.append('removed a <base href> from the entry document')

        def sub_link(m):
            tag = m.group(0)
            hm = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not hm:
                return tag
            k = self.norm(ed, hm.group(1))
            if k is None:
                # a stylesheet on a CDN cannot be fetched; drop the tag so it
                # does not produce a request and a console error
                if re.search(r'rel\s*=\s*["\'][^"\']*stylesheet', tag, re.I) \
                        and hm.group(1).lower().startswith(('http', '//')):
                    self.notes.append('dropped external stylesheet: ' + hm.group(1))
                    return '<!-- wathb: external stylesheet dropped -->'
                return tag
            rm = re.search(r'rel\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            rel = (rm.group(1) if rm else '').lower()
            if 'stylesheet' in rel:
                return '<style>\n%s\n</style>' % self.inline_css(k)
            self.inlined.add(k)
            return tag.replace(hm.group(1), self.data_uri(k))

        html = re.sub(r'<link\b[^>]*>', sub_link, html, flags=re.I)

        def sub_script(m):
            tag, src = m.group(0), m.group(1)
            k = self.norm(ed, src)
            if k is None:
                if src.lower().startswith(('http', '//')):
                    self.notes.append('dropped external script: ' + src)
                    return '<!-- wathb: external script dropped -->'
                return tag
            self.inlined.add(k)
            body = self.vfs[k].decode('utf-8', 'replace')
            # a literal </script> in the JS would close the tag early
            body = re.sub(r'</(script)', r'<\\/\1', body, flags=re.I)
            attrs = ''
            if re.search(r'\btype\s*=\s*["\']module["\']', tag, re.I):
                attrs = ' type="module"'
            elif re.search(r'\b(defer|async)\b', tag, re.I):
                # inline scripts ignore defer/async; order is preserved, which
                # is what almost every loader actually depends on
                self.notes.append('inlined a defer/async script: ' + k)
            return '<script%s>\n/* wathb: %s */\n%s\n</script>' % (attrs, k, body)

        html = re.sub(
            r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>\s*</script>',
            sub_script, html, flags=re.I)

        def sub_media(m):
            tag, out = m.group(0), m.group(0)
            for attr in ('src', 'poster', 'data'):
                mm = re.search(r'\b' + attr + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
                if not mm:
                    continue
                k = self.norm(ed, mm.group(1))
                if not k:
                    if is_remote(mm.group(1)):
                        self.remote_blanked += 1
                        out = out.replace(mm.group(0),
                                          '%s="%s"' % (attr, blank_for(mm.group(1))))
                    continue
                if len(self.vfs[k]) <= INLINE_MAX:
                    self.inlined.add(k)
                    out = out.replace(mm.group(0),
                                      '%s="%s"' % (attr, self.data_uri(k)))
                else:
                    out = out.replace(mm.group(0),
                                      'data-wathb-%s="%s"' % (attr, k))
            # srcset: a comma-separated list of "url descriptor" pairs
            sm = re.search(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if sm:
                parts = []
                for piece in sm.group(1).split(','):
                    piece = piece.strip()
                    if not piece:
                        continue
                    bits = piece.split(None, 1)
                    k = self.norm(ed, bits[0])
                    if k and len(self.vfs[k]) <= INLINE_MAX:
                        self.inlined.add(k)
                        bits[0] = self.data_uri(k)
                    elif not k and is_remote(bits[0]):
                        self.remote_blanked += 1
                        bits[0] = blank_for(bits[0])
                    parts.append(' '.join(bits))
                out = out.replace(sm.group(0), 'srcset="%s"' % ', '.join(parts))
            return out

        html = re.sub(r'<(?:img|source|audio|video|embed|object|track|iframe)\b[^>]*>',
                      sub_media, html, flags=re.I)

        def sub_style_block(m):
            inner = m.group(1)
            if not inner.strip():
                return m.group(0)

            def do_url(mm):
                k = self.norm(ed, mm.group(1))
                if k is None:
                    if is_remote(mm.group(1)):
                        self.remote_blanked += 1
                        return 'url("%s")' % blank_for(mm.group(1))
                    return mm.group(0)
                self.inlined.add(k)
                return 'url("%s")' % self.data_uri(k)
            return m.group(0).replace(inner, self.CSS_URL.sub(do_url, inner))

        html = re.sub(r'<style[^>]*>(.*?)</style>', sub_style_block, html,
                      flags=re.I | re.S)
        return html

    # ── payload + assembly ────────────────────────────────────────────
    def run(self, out_path, max_bytes=HARD_LIMIT):
        html = self.build_html()

        index, blobs, offset, by_name = {}, [], 0, {}
        for rel in sorted(self.vfs):
            if rel == self.entry or rel in self.inlined:
                continue
            data = self.vfs[rel]
            index[rel] = [offset, len(data), mime_of(rel)]
            by_name.setdefault(rel.split('/')[-1], []).append(rel)
            blobs.append(data)
            offset += len(data)

        payload = b''.join(blobs)
        if len(payload) > max_bytes:
            raise SystemExit(
                'TOO LARGE: %.1f MiB of assets exceeds the %.0f MiB ceiling.\n'
                'A single JS string caps at 512 MiB in V8, so base64 cannot '
                'carry more than %.0f MiB. This app cannot be a single file.'
                % (len(payload) / 1048576, max_bytes / 1048576, max_bytes / 1048576))

        chunks = [base64.b64encode(payload[i:i + B64_CHUNK]).decode('ascii')
                  for i in range(0, len(payload), B64_CHUNK)] or ['']

        with open(SHIM, 'r', encoding='utf-8') as fh:
            shim = fh.read()

        cfg = {
            'index': index,
            'byName': {k: v for k, v in by_name.items() if len(v) == 1},
            'payload': chunks,
            'quiet': self.quiet,
            'log': self.log,
        }
        boot = ('<link rel="icon" href="data:,">\n'
                '<script>window.__WATHB__=' + json.dumps(cfg, separators=(',', ':'))
                + ';</script>\n<script>' + shim + '</script>\n')

        m = re.search(r'<head[^>]*>', html, re.I)
        if m:
            html = html[:m.end()] + '\n' + boot + html[m.end():]
        else:
            m2 = re.search(r'<html[^>]*>', html, re.I)
            html = (html[:m2.end()] + '<head>' + boot + '</head>' + html[m2.end():]
                    if m2 else boot + html)

        with open(out_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(html)

        stats = {
            'entry': self.entry,
            'filesTotal': len(self.vfs),
            'filesInlined': len(self.inlined),
            'filesInPayload': len(index),
            'payloadBytes': len(payload),
            'outBytes': os.path.getsize(out_path),
            'b64Chunks': len(chunks),
            'missingRefs': sorted(set(self.missing)),
            'remoteBlanked': self.remote_blanked,
            'notes': self.notes,
        }
        if self.verbose:
            print('  entry     : %s' % stats['entry'])
            print('  files     : %d total -> %d inlined, %d in payload'
                  % (stats['filesTotal'], stats['filesInlined'], stats['filesInPayload']))
            print('  payload   : %.1f MiB in %d base64 chunk(s)'
                  % (stats['payloadBytes'] / 1048576, stats['b64Chunks']))
            print('  output    : %s (%.1f MiB)' % (out_path, stats['outBytes'] / 1048576))
            if stats['missingRefs']:
                print('  MISSING   : %d referenced path(s) not in the folder'
                      % len(stats['missingRefs']))
                for r in stats['missingRefs'][:8]:
                    print('              %s' % r)
            for n in self.notes[:8]:
                print('  note      : %s' % n)
        return stats


def main():
    ap = argparse.ArgumentParser(description='Web App To HTML Builder')
    ap.add_argument('folder')
    ap.add_argument('-o', '--out')
    ap.add_argument('--entry', help='relative path to the entry .html')
    ap.add_argument('--log', action='store_true', help='runtime debug logging')
    ap.add_argument('--loud', action='store_true',
                    help='let missing files 404 instead of synthesizing')
    ap.add_argument('--max-mb', type=float, default=HARD_LIMIT / 1048576)
    a = ap.parse_args()

    out = a.out or (os.path.basename(os.path.abspath(a.folder)) + '.html')
    b = Builder(a.folder, entry=a.entry, log=a.log, quiet=not a.loud)
    b.run(out, max_bytes=int(a.max_mb * 1048576))


if __name__ == '__main__':
    main()

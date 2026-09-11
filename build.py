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


# Hosts that exist only to talk to the network: ads, analytics, telemetry,
# consent widgets. These are DROPPED, never fetched -- embedding them would
# put the very requests back that the port exists to remove. The runtime
# defines permissive stand-ins for the globals they would have created.
TELEMETRY = (
    'googletagmanager.com', 'google-analytics.com', 'googlesyndication.com',
    'doubleclick.net', 'imasdk.googleapis.com', 'adservice.google',
    'analytics.google.com', 'poki.com', 'poki-gdn', 'gamemonetize',
    'gameanalytics', 'connect.facebook.net', 'hotjar', 'mixpanel',
    'segment.com', 'segment.io', 'sentry.io', 'adinplay', 'playwire',
    'crazygames.com', 'gamedistribution', 'clarity.ms', 'cookieconsent',
    'cookielaw', 'onetrust',
)
REMOTE_CACHE = os.path.join(HERE, '.remote-cache')
REMOTE_MAX = 8 * 1024 * 1024        # a library this big is not a library


def is_telemetry(url):
    u = str(url).lower()
    return any(h in u for h in TELEMETRY)


def fetch_remote(url, timeout=20):
    """Download a remote dependency ONCE, at build time.

    Fetching here is not a contradiction of "no network": the requirement is
    that the finished file needs no network, and a build step is exactly
    where a bundler resolves its dependencies. Telemetry is never fetched.
    """
    import hashlib, urllib.request, urllib.error
    if url.startswith('//'):
        url = 'https:' + url
    os.makedirs(REMOTE_CACHE, exist_ok=True)
    cache = os.path.join(REMOTE_CACHE, hashlib.sha256(url.encode()).hexdigest()[:32])
    if os.path.exists(cache):
        with open(cache, 'rb') as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 WATHB'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(REMOTE_MAX + 1)
    except Exception:
        return None
    if not data or len(data) > REMOTE_MAX:
        return None
    with open(cache, 'wb') as fh:
        fh.write(data)
    return data


def decode_text(data):
    """Decode a file for inlining WITHOUT destroying it.

    `decode('utf-8', 'replace')` turns every invalid byte into U+FFFD, which
    is irreversible. Plenty of shipped game scripts are Latin-1, and mangling
    them silently breaks the library they define -- Basketball Stars' physics
    engine came out undefined this way, with no error pointing at the cause.

    latin-1 maps bytes 1:1 onto U+0000-U+00FF, so the resulting characters
    are exactly what a browser reading the file as Latin-1 would see, which
    is what the original page did. Writing them back out as UTF-8 round-trips
    them intact.
    """
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('latin-1')


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
    def __init__(self, root, entry=None, log=False, quiet=True, verbose=True,
                 fetch_remote=True):
        self.root = os.path.abspath(root)
        self.log = log
        self.quiet = quiet
        self.verbose = verbose
        self.vfs = {}
        self.inlined = set()
        self.missing = []          # refs the app names that do not exist
        self.notes = []
        self.remote_blanked = 0    # remote refs replaced so they cannot fire
        self.fetch_remote = fetch_remote
        self.vendored = []         # remote libraries embedded at build time

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

        # Some rippers percent-encode the filename when saving, so the page
        # asks for "Dadish 2 Pk.js" while the file on disk is literally named
        # "Dadish%202%20Pk.js". Index the decoded form of every key so both
        # spellings resolve to the same file.
        from urllib.parse import unquote
        self.decoded = {}
        for k in self.vfs:
            d = unquote(k)
            if d != k and d not in self.vfs:
                self.decoded.setdefault(d, k)

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
        if key in self.decoded:
            return self.decoded[key]

        # Site rippers mirror a remote tree into escaped directory names
        # ("https://host/x@main/" -> "https_/_/host/x_main/"), and the page
        # still refers to files the way the original <base href> made them
        # resolve. Rather than reverse-engineer each ripper's escaping, take
        # the unique file whose path ENDS with the reference. Only accept it
        # when exactly one candidate matches -- an ambiguous match would
        # silently serve the wrong file, which is the failure mode this
        # resolver exists to avoid.
        # Try both spellings as a suffix: the entry-dir-joined key, and the
        # reference exactly as written. The second matters when a <base href>
        # pointed somewhere else entirely, so the path the page uses has
        # nothing to do with the entry document's folder.
        for cand in ([key] if key == bare.lstrip('/') else [key, bare.lstrip('/')]):
            tail = '/' + cand
            hits = [k for k in self.vfs if k.endswith(tail)]
            if len(hits) == 1:
                self.notes.append('resolved by unique suffix: %s -> %s' % (ref, hits[0]))
                return hits[0]
            if not hits:
                hits = [v for d, v in self.decoded.items() if d.endswith(tail)]
                if len(hits) == 1:
                    self.notes.append('resolved by decoded suffix: %s -> %s' % (ref, hits[0]))
                    return hits[0]

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
        css = decode_text(self.vfs[key])
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
    def nested_boot(self):
        """Bootstrap for a document embedded with srcdoc.

        It inherits the parent's origin, so it borrows the already-decoded
        payload through window.parent instead of carrying its own copy.
        """
        with open(SHIM, 'r', encoding='utf-8') as fh:
            shim = fh.read()
        return ('<meta charset="utf-8">'
                '<script>(function(){var p=window.parent&&window.parent.__WATHB_SHARE__;'
                'window.__WATHB__=p?{index:p.index,byName:p.byName,alias:p.alias,'
                'bytes:p.bytes,quiet:true}:{index:{},payload:[]};})();</script>'
                '<script>' + shim + '</script>')

    def build_html(self, entry=None, depth=0):
        entry = entry or self.entry
        html = decode_text(self.vfs[entry])
        ed = posixpath.dirname(entry)

        # An <iframe> pointing at a local page is its OWN document: our hooks
        # do not reach inside it, and turning it into a data: URL (which is
        # what the generic media path did) leaves it with no runtime at all --
        # Eggy Car then pulled 79 assets straight off a CDN. Build it properly
        # and hand it the same filesystem. Nine of these games are wrappers
        # whose entry is nothing but an iframe.
        def sub_iframe(m):
            tag = m.group(0)
            mm = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not mm or depth >= 3:
                return tag
            k = self.norm(ed, mm.group(1))
            if not k or not k.lower().endswith(('.html', '.htm')):
                return tag
            self.inlined.add(k)
            inner = self.nested_boot() + self.build_html(entry=k, depth=depth + 1)
            self.notes.append('embedded iframe document: %s' % k)

            # Carried as a payload entry and attached at runtime as a blob
            # URL, NOT as srcdoc. Escaping a whole document into an attribute
            # is fragile -- and every transform below matches raw text with
            # no notion of attribute boundaries, so an inline srcdoc gets
            # re-processed and mangled. A blob URL needs no escaping, and a
            # blob iframe inherits this origin, so it can still borrow the
            # parent's decoded payload.
            nkey = '__wathb_nested_%d.html' % len(nested)
            self.vfs[nkey] = inner.encode('utf-8')
            nested.append(nkey)
            return re.sub(r'\bsrc\s*=\s*["\'][^"\']+["\']',
                          lambda _m: 'data-wathb-doc="%s"' % nkey,
                          tag, flags=re.I, count=1)

        nested = []
        html = re.sub(r'<iframe\b[^>]*>', sub_iframe, html, flags=re.I)

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
                if re.search(r'rel\s*=\s*["\'][^"\']*stylesheet', tag, re.I) \
                        and hm.group(1).lower().startswith(('http', '//')):
                    href = hm.group(1)
                    if not is_telemetry(href) and self.fetch_remote:
                        data = fetch_remote(href)
                        if data:
                            self.vendored.append(href)
                            return ('<style>/* wathb vendored: %s */\n%s\n</style>'
                                    % (href, decode_text(data)))
                    self.notes.append('dropped external stylesheet: ' + href)
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
                    if is_telemetry(src):
                        self.notes.append('dropped telemetry script: ' + src)
                        return '<!-- wathb: telemetry script dropped -->'
                    # A real library the app needs (jQuery and friends).
                    # Fetching it HERE is not a contradiction of "no network":
                    # the finished file needs none, and resolving dependencies
                    # is what a build step is for.
                    data = fetch_remote(src) if self.fetch_remote else None
                    if data:
                        self.vendored.append(src)
                        js = re.sub(r'</(script)', r'<\\/\1',
                                    decode_text(data), flags=re.I)
                        return ('<script>\n/* wathb vendored at build time: %s */\n%s\n</script>'
                                % (src, js))
                    self.notes.append('dropped external script (unfetchable): ' + src)
                    return '<!-- wathb: external script dropped -->'
                # A local path that does not exist. The parser sets this src
                # itself, so no runtime hook can reach it: over http it is a
                # guaranteed 404 and in a blob document it fails silently.
                # Dropping it is the only outcome with no error.
                self.notes.append('dropped missing local script: ' + src)
                return '<!-- wathb: missing local script dropped -->'
            self.inlined.add(k)
            body = decode_text(self.vfs[k])
            # a literal </script> in the JS would close the tag early
            body = re.sub(r'</(script)', r'<\\/\1', body, flags=re.I)
            attrs = ''
            if re.search(r'\btype\s*=\s*["\']module["\']', tag, re.I):
                attrs = ' type="module"'
            elif re.search(r'\b(defer|async)\b', tag, re.I):
                # inline scripts ignore defer/async; order is preserved, which
                # is what almost every loader actually depends on
                self.notes.append('inlined a defer/async script: ' + k)

            # An INLINE script never fires a load event, so an onload handler
            # on the original tag would simply never run -- and for loaders
            # that start the app from onload (love.js does exactly this) the
            # result is a page that sits at "Downloading..." forever with no
            # error at all. Invoke it explicitly, with `this` bound to the
            # script element as the attribute form guarantees.
            after = ''
            om = re.search(r'\bonload\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
            if om:
                handler = re.sub(r'</(script)', r'<\\/\1', om.group(2), flags=re.I)
                self.notes.append('replayed onload for inlined script: ' + k)
                after = (
                    '\n<script>/* wathb: the original tag had onload */\n'
                    '(function(){var s=document.currentScript'
                    '&&document.currentScript.previousElementSibling;'
                    'try{(function(event){' + handler + '}).call(s,new Event("load"));}'
                    'catch(e){console.error("wathb onload replay:",e);}})();\n'
                    '</script>')
            return ('<script%s>\n/* wathb: %s */\n%s\n</script>%s'
                    % (attrs, k, body, after))

        # The body is matched as `.*?`, not `\s*`: a <script> may carry BOTH a
        # src and inline content, and the HTML spec says the content is
        # ignored when src is present. Requiring an empty body left those tags
        # untouched -- and a parser-set relative src cannot resolve in a blob
        # document, so the file simply never loaded, with no request and no
        # error. Basketball Stars' physics engine vanished exactly this way.
        html = re.sub(
            r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>.*?</script>',
            sub_script, html, flags=re.I | re.S)

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

        # iframes are deliberately absent here -- they were handled above as
        # documents. Letting the generic path data-URI them is the bug.
        html = re.sub(r'<(?:img|source|audio|video|embed|object|track)\b[^>]*>',
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

        # restore embedded iframe documents, now that nothing else will touch
        for i, doc in enumerate(nested):
            html = html.replace('@@WATHB_NESTED_%d@@' % i, doc)
        return html

    # ── payload + assembly ────────────────────────────────────────────
    def run(self, out_path, max_bytes=HARD_LIMIT):
        html = self.build_html()

        from urllib.parse import unquote
        index, blobs, offset, by_name, alias = {}, [], 0, {}, {}
        for rel in sorted(self.vfs):
            if rel == self.entry or rel in self.inlined:
                continue
            data = self.vfs[rel]
            index[rel] = [offset, len(data), mime_of(rel)]
            by_name.setdefault(rel.split('/')[-1], []).append(rel)
            # the decoded spelling is what the page will actually ask for
            dec = unquote(rel)
            if dec != rel:
                alias.setdefault(dec, rel)
                by_name.setdefault(dec.split('/')[-1], []).append(rel)
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
            'byName': by_name,
            'alias': alias,
            'payload': chunks,
            'quiet': self.quiet,
            'log': self.log,
        }
        # The charset declaration MUST come first and must be within the
        # first 1024 bytes -- that is all the browser sniffs. Our payload is
        # megabytes of base64 injected at the top of <head>, which shoves the
        # document's own <meta charset> far past that window; the browser
        # then falls back to Latin-1 and every non-ASCII byte is mangled.
        # That is not cosmetic: it turned a UTF-8 range regex into
        # "Invalid regular expression: range out of order" and killed the game.
        boot = ('<meta charset="utf-8">\n'
                '<link rel="icon" href="data:,">\n'
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
            'vendored': sorted(set(self.vendored)),
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
    ap.add_argument('--no-fetch-remote', action='store_true',
                    help='do not download remote libraries at build time')
    a = ap.parse_args()

    out = a.out or (os.path.basename(os.path.abspath(a.folder)) + '.html')
    b = Builder(a.folder, entry=a.entry, log=a.log, quiet=not a.loud,
                fetch_remote=not a.no_fetch_remote)
    b.run(out, max_bytes=int(a.max_mb * 1048576))


if __name__ == '__main__':
    main()

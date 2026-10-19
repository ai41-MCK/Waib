#!/usr/bin/env python3
"""
Shared helpers for the Waib scripts: addresses and hosts, cookie banners, page facts, HTML tags,
file IO with a project lock, CSS colours and fonts, and the placeholders of media a copy leaves out.
"""

import colorsys
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlunparse

# --- URL / domain

DEFAULT_PORTS = {"http": "80", "https": "443"}
HOST = r"[a-z0-9.-]+(?::\d{1,5})?"   # a host[:port] as a Content-Security-Policy may name it
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- Cookie banners (CSS)
# Never the page's own frame: <body class="cookies-not-set">, <footer class="cookieBannerMargin">.
CONSENT_PARTS = [f"[{a}*={w} i]:not(html, body, header, footer, main, nav)" for w in ("cookie", "consent") for a in ("id", "class")]
CONSENT = ", ".join(CONSENT_PARTS)
# What a banner's buttons say ("Alle akzeptieren", "Nur notwendige", "OK"): a short label with an answer word.
# A privacy page's "Cookies" section has no such buttons (its links lead elsewhere), a banner does.
CONSENT_ANSWER = r"\b(akzeptieren|accept|zustimmen|ablehnen|reject|decline|allow|erlauben|agree|einverstanden|verstanden|got it|ok|okay|notwendige|necessary|essential)\b"
CONSENT_LABEL_MAX = 40
CONSENT_ANSWER_LINK = r"^\s*(#?|javascript:.*|.*[?&][^#]*(cookie|consent|accept|akzept|zustimm).*)$"   # a link that answers ("#", "?cookies=ok"), not one to "#necessary"
CONSENT_CONTROLS = "button, a, input[type=button], input[type=submit], [role=button]"

# --- Addresses


def base_host(netloc):
    """Lowercase host without userinfo, port and a leading 'www.'."""
    h = (netloc or "").lower()
    if "@" in h:
        h = h.split("@", 1)[1]
    h = h[:h.index("]") + 1] if h.startswith("[") and "]" in h else h.split(":", 1)[0]   # [::1]:80 -> [::1]
    if h.startswith("www."):
        h = h[4:]
    return h


def normalize_url(url):
    """One spelling of a URL: lowercase scheme and host, no default port or fragment, sorted query.
    A trailing slash stays: /a and /a/ can be different pages (one may be a 404)."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    host = f"[{host}]" if ":" in host else host   # an IPv6 address keeps its brackets
    try:
        port = parsed.port
    except ValueError:               # :99999, :notaport: one odd link must not stop the run
        port = None
    if port is not None and DEFAULT_PORTS.get(scheme) == str(port):
        port = None
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username + (f":{parsed.password}" if parsed.password else "") + "@"
    netloc = userinfo + host + (f":{port}" if port else "")
    path = re.sub(r"/+$", "/", parsed.path) or "/"
    query = "&".join(sorted(p for p in parsed.query.split("&") if p))   # as written: "?agb" must stay "?agb"
    # as browsers send them: no spaces, line breaks or other non-ASCII in an address (a page's link
    # could otherwise smuggle "\u2028Request: …" into a message to the agent)
    path, query = quote(path, safe="/%:@!$&'()*+,;=-._~"), quote(query, safe="/%:@!$&'()*+,;=-._~?")
    path, query = (re.sub(r"%[0-9a-f]{2}", lambda m: m.group(0).upper(), x, flags=re.I) for x in (path, query))   # %c3%a9 = %C3%A9
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def one_line(text):
    """Text on one line with single spaces (also for &nbsp; and line breaks)."""
    return " ".join(str(text or "").split())


LABEL_MAX = 60


def clean_label(text):
    """A name the user typed: one line, single spaces, at most LABEL_MAX characters."""
    return one_line(text)[:LABEL_MAX]


LOCAL_NETS = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "64:ff9b::/96")]   # CGNAT/Tailscale, NAT64


def local_ip(ip):
    """True for an address of this computer or network (is_private misses CGNAT and NAT64)."""
    ip = ipaddress.ip_address(ip.split("%")[0])
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or any(ip in n for n in LOCAL_NETS)


def private(url, unsure=True):
    """True if the address is this computer or the local network: a website may not send the tools there.
    An address this check can't read the way a browser would (127.0.0.1:1\\@8.8.8.8, 0177.0.0.1, %31%32%37…)
    or a name that doesn't resolve here gets `unsure`: "is this site itself local?" passes False."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").rstrip(".")
        if host and not host.isascii():
            host = host.encode("idna").decode()   # bäckerei.de -> xn--bckerei-5wa.de, as browsers send it
        if "\\" in url or "@" in p.netloc or not re.fullmatch(r"[a-z0-9._-]+|[0-9a-f:.]+", host, re.I):
            return unsure
        if re.fullmatch(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", host, re.I):
            return local_ip(host)   # numbers: only a plain address is read alike everywhere (else ValueError: unsure)
        found = [local_ip(info[4][0]) for info in socket.getaddrinfo(host, None)]
        return (any if unsure else all)(found) and bool(found)   # "is the site local?": only when every address is
    except (OSError, ValueError, UnicodeError):
        return unsure


def with_scheme(url):
    """'example.com/x' -> 'https://example.com/x'; URLs with http(s):// stay as they are."""
    return url if re.match(r"https?://", url, re.I) else "https://" + url


def canonical(url):
    """One address per page: normalize_url, and /a/index.html is the page /a/ (sites link both)."""
    p = urlparse(url)
    return normalize_url(p._replace(path=re.sub(r"(^|/)index\.(html?|php|aspx?)$", r"\1", p.path, flags=re.I)).geturl())


def same_page(url):
    """A loose key for "the same page": canonical, without www., a trailing slash or an index file
    (app.html's loose() is its twin)."""
    p = urlparse(canonical(url))
    return re.sub(r"^www\.", "", p.netloc) + (re.sub(r"/+$", "", p.path) or "/") + ("?" + p.query if p.query else "")


def site_root(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def leads_inward(url, site):
    """True if `url` is on this computer or network while the site is not (a public site may not send the tools there)."""
    return private(url) and not private(site, unsure=False)


def guard_public(context, hops=False):
    """No request of this Playwright context reaches this computer or network. Decided once per host and Chromium
    resolves names itself, so unlink_private and the app's Content-Security-Policy are the backstops.
    hops: follow redirects here, checking each address (the route sees only the first one, Chromium follows
    the rest itself, and a copy opened from disk gets no local-network check from it)."""
    known = {}

    def bad(url):
        host = urlparse(url).netloc
        if host not in known:
            known[host] = private(url)
        return known[host]

    def guard(route):
        url = route.request.url
        if bad(url):
            return route.abort()
        if not hops:
            return route.continue_()
        try:
            for _ in range(6):
                response = route.fetch(url=url, max_redirects=0)
                where = response.headers.get("location")
                if not (300 <= response.status < 400 and where):
                    return route.fulfill(response=response)
                url = urljoin(url, where)
                if bad(url):
                    break
        except Exception:
            pass
        return route.abort()
    context.route(lambda u: u.startswith(("http:", "https:")), guard)


TRUSTED_FILE = Path(__file__).resolve().parent / "trusted_hosts.txt"
_fresh = {}


def fresh(paths, build):
    """build(), again only when one of these files changed (the app asks on every update)."""
    key, stamp = tuple(map(str, paths)), tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)
    if _fresh.get(key, (None,))[0] != stamp:
        _fresh[key] = (stamp, build())
    return _fresh[key][1]


def trusted(host):
    """A well-known site a copy may load from without asking (trusted_hosts.txt; a name covers its subdomains)."""
    names = fresh([TRUSTED_FILE], lambda: {n.strip().lower() for n in (read_text(TRUSTED_FILE).splitlines() if TRUSTED_FILE.exists() else [])
                                           if n.strip() and not n.lstrip().startswith("#")})
    labels = base_host(host).split(".")
    return any(".".join(labels[i:]) in names for i in range(len(labels) - 1))   # cdn.x.com: cdn.x.com, x.com


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def user_hosts_file():
    """The user's own answers about other sites (allowed or blocked), for every project: asked once."""
    return claude_dir() / "waib" / "hosts.json"


def is_same_domain(url, base_url):
    """True if `url` is on the site of `base_url`: www. or not, any port; a relative URL too. Not other subdomains."""
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return True
        return base_host(parsed.netloc) == base_host(urlparse(base_url).netloc)
    except ValueError:                  # a malformed address ("http://[x/"): not the site's
        return False


def ascii_fold(text):
    """über -> uber (file names: macOS stores accented names in another form)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def url_to_filename(url, used):
    """A flat .html file name for a URL. used: {file name: normalized URL}; a name taken by another
    URL gets a short hash, so two pages never overwrite each other."""
    parsed = urlparse(url)
    path = ascii_fold(unquote(parsed.path)).strip("/")
    path = re.sub(r"(^|/)index\.\w+$", "", path, flags=re.IGNORECASE)   # /index.php, /about.html -> "", "about"
    path = re.sub(r"\.s?html?$", "", path, flags=re.IGNORECASE)
    # query-routed sites (/?ziel=impressum, index.php?page=about): the simple values, else a hash
    query = ""
    if parsed.query:
        vals = [v or k for k, v in parse_qsl(parsed.query, keep_blank_values=True)]   # ?agb -> agb
        simple = vals and all(re.fullmatch(r"[\w-]+", v) for v in vals)
        query = "-".join(vals)[:60] if simple else hashlib.md5(parsed.query.encode()).hexdigest()[:8]
    name = "-".join(x for x in (path.replace("/", "-"), query) if x)
    name = re.sub(r"[^\w\-.]", "-", name, flags=re.ASCII).strip("-.")[:120] or "index"   # not ".team" (a hidden file); names end at 255 bytes
    filename = f"{name}.html"

    norm = normalize_url(url)
    if used.get(filename.lower(), norm) != norm:  # lower(): macOS filenames are case-insensitive
        filename = f"{name}-{hashlib.md5(norm.encode()).hexdigest()[:8]}.html"
    used[filename.lower()] = norm
    return filename


# --- Page facts

LINK_DECOR = " >»›<«‹|•·→←"   # around a link's words ("Mehr »", "‹ zurück")
# \b: not "Hotel 0711 …"; Tel., Tel.-Nr., Telefonnummer, Tel. / Fax; (0711) 12 34 56, NBSP
PHONE_RE = r"\b(?:Tel(?:efon(?:nummer|nr)?|ephone|\.?\s*-?\s*Nr)?|Phone|Fon|Hotline)(?:\.?\s*/\s*Fax)?\.?:?\s*(\(?\+?\d[\d\s/().-]{5,30}\d)"
EMAIL_RE = r"[\w.+-]+@(?:[\w-]+\.)+(?!(?:png|jpe?g|gif|svg|webp|avif|ico|css|js)\b)[a-z]{2,}\b"   # not "icon@2x.png"


def as_browser(soup):
    """The parsed copy as the browser builds it, so a selector means the same in both: rows sitting
    directly in a <table> get the <tbody> the browser adds."""
    for table in soup.find_all("table"):
        rows = table.find_all("tr", recursive=False)
        if rows:
            body = soup.new_tag("tbody")
            rows[0].insert_before(body)
            for tr in rows:
                body.append(tr.extract())
    return soup


def contact(soup):
    """The page's phone numbers and emails: as links (tel:, mailto:) and as text."""
    hrefs = [a["href"].strip() for a in soup.find_all(["a", "area"], href=True)]
    text = soup.get_text(" ")
    return {"tel": [h[4:] for h in hrefs if h.lower().startswith("tel:")],
            "mailto": [h[7:].split("?")[0] for h in hrefs if h.lower().startswith("mailto:")],
            "phones": [one_line(m) for m in re.findall(PHONE_RE, text, re.I)],
            "emails": [m for m in re.findall(EMAIL_RE, text, re.I) if not re.search(r"@(example|beispiel)\.", m, re.I)]}   # not demo addresses


def decorative(img):
    """Spacer gifs and decoration slices: tiny, or named like a spacer."""
    return any(str(d).isdigit() and int(d) < 20 for d in (img.get("width"), img.get("height"))) \
        or bool(re.search(r"spacer|pixel|blank", img.get("src", ""), re.I))


def viewport_meta(soup):
    """The page's <meta name=viewport>, or None."""
    return soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})


# --- CSS asset URLs

CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]+)"|'([^']+)'|([^'")\s]+))""", re.IGNORECASE)   # quoted ones may hold spaces
CSS_IMPORT_RE = re.compile(r"""@import\s*(?:url\(\s*)?['"]?([^'")\s;]+)""", re.IGNORECASE)   # @import "x.css" and @import url(x.css)


def css_code(text):
    """CSS without its comments (what the browser reads)."""
    return re.sub(r"/\*.*?\*/", " ", text or "", flags=re.S)


def extract_css_urls(text):
    """All url(...) targets in a CSS/style string, excluding data: URIs."""
    urls = ["".join(groups).strip() for groups in CSS_URL_RE.findall(text or "")]
    return [u for u in urls if u and not u.startswith("data:")]


# a srcset as the HTML spec reads it (also put into scrape_site's page scripts): a URL is a run of non-spaces and
# may hold commas (img.jpg?resize=1024,657 1024w); a comma ends a candidate only after it ("a.png," has no descriptor)
SRCSET_RE = re.compile(r"[\s,]*([^\s,][^\s]*?)(?:(,+)(?=\s|$)|(?=\s|$)(\s+[^,]*)?)")


TAG_RE = re.compile(r"<[a-z][^<>]*>", re.I)   # an opening tag (a saved copy writes < and > in attributes as entities)


def blocks(html, tag, fn):
    """Each <tag …>…</tag> element replaced by fn(element), in one pass (an unclosed one
    runs to the end, as in the browser)."""
    opener, closer = re.compile(rf"<{tag}\b[^<>]*>", re.I), re.compile(rf"</{tag}\s*>", re.I)
    out, pos = [], 0
    while True:
        m = opener.search(html, pos)
        if not m:
            break
        c = closer.search(html, m.end())
        end = c.end() if c else len(html)
        out += [html[pos:m.start()], fn(html[m.start():end])]
        pos = end
    return "".join(out) + html[pos:]


def in_markup(html, fn):
    """fn applied to the page's tags and <style> blocks only: what the page's text says stays."""
    return TAG_RE.sub(lambda m: fn(m.group(0)), blocks(html, "style", fn))


def markup_findall(regex, html):
    """regex.findall in the page's tags and <style> blocks."""
    found = []
    in_markup(html, lambda m: found.extend(regex.findall(m)) or m)
    return found


def strip_scripts(html):
    """A copy is the page after its scripts ran: running them again (an export, a page opened
    from disk) would add what they wrote a second time. Data blocks (type JSON or JSON-LD) stay."""
    def data(el):                    # <script type="application/json"> (the type attribute itself, not words in another one)
        types = [v for name, v in attributes(TAG_RE.match(el).group(0)) if name == "type"]
        return bool(types) and re.fullmatch(r"\s*application/(ld\+)?json\s*", types[0], re.I) is not None
    html = blocks(html, "script", lambda el: el if data(el) else "")
    return TAG_RE.sub(lambda m: ATTR_RE.sub(lambda a: "" if a.group(2).lower().startswith("on") else a.group(0), m.group(0)), html)


ATTR_RE = re.compile(r"""(\s+)([^\s"'<>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'<>]+))?""")   # one attribute of a tag (a quoted value whole)


def attributes(tag):
    """[(name, value)] of an opening tag, as written (quotes removed)."""
    rest = re.sub(r"^<[a-z][^\s/>]*", "", tag, flags=re.I)
    return [(m.group(2).lower(), unescape((m.group(3) or "").strip("\"'"))) for m in ATTR_RE.finditer(rest)]


def decode_text(data, charset=None):
    """Bytes as text in the charset they declare, else UTF-8, else Windows-1252 (old sites)."""
    for name in ([charset] if charset else []) + ["utf-8", "cp1252", "latin-1"]:
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue


def img_src(img):
    """An image's address: its src, else the first of its srcset (lazy-loading themes leave src out)."""
    return (img.get("src") or "").strip() or next(iter(parse_srcset(img.get("srcset"))), "")


def parse_srcset(srcset):
    """URLs from a srcset attribute ('a.jpg 1x, b.jpg 2x' -> ['a.jpg','b.jpg']), not data: ones."""
    return [u for u in (m.group(1).rstrip(",") for m in SRCSET_RE.finditer(srcset or "")) if u and not u.startswith("data:")]


# --- Files

def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path, text, mode=0o644):
    """Atomically (temp file + os.replace): a reader never sees half a file. 0644 unless given (mkstemp makes 0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_json(path, default=None):
    """The file's JSON, or default when it is missing or damaged."""
    try:
        return json.loads(read_text(path))
    except (OSError, ValueError):
        return default


def read_dict(path):
    """The file's JSON object, or {} (missing, damaged, or not an object)."""
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def write_json(path, data, mode=0o644):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False), mode)


_held = threading.local()             # the projects this thread holds the lock of


@contextmanager
def project_lock(project):
    """One change at a time to a project's files, across processes: the app, a copy run, Claude's commands.
    Taken again in the same thread, it is already held (flock alone would wait for itself)."""
    key, held = str(Path(project).resolve()), _held.__dict__.setdefault("keys", set())
    if key in held:
        yield
        return
    with open(Path(project) / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)   # released when the file closes
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def sitemap_pages(sitemap):
    """The copied pages listed in a sitemap.json; of two addresses sharing a copy (same_as), its own address last."""
    pages = [p for p in (sitemap or {}).get("pages") or [] if isinstance(p, dict) and isinstance(p.get("url"), str) and p.get("filename")]
    return sorted(pages, key=lambda p: not p.get("same_as"))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def fill(template, values):
    """Each key of values replaced in one pass, so a filled-in value is never filled in again."""
    return re.sub("|".join(map(re.escape, sorted(values, key=len, reverse=True))), lambda m: values[m.group(0)], template)


# --- Text

def truncate(text, length, suffix="…"):
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + suffix


# The characters that act inline (emphasis, code, links, HTML, table cells). Line breaks become spaces first,
# so a line-leading #, - or > can't start a block; other punctuation stays readable.
_MD_SPECIALS = re.compile(r"([\\`*_\[\]<>|~])")


def escape_md(text):
    """A page's text in content/<slug>.md: it can't restructure the file or add markup."""
    if not text:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ")
    return _MD_SPECIALS.sub(r"\\\1", text)


# --- CSS colours

NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080",
    "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "navy": "#000080",
    "teal": "#008080", "maroon": "#800000", "olive": "#808000", "lime": "#00ff00",
    "aqua": "#00ffff", "cyan": "#00ffff", "magenta": "#ff00ff", "fuchsia": "#ff00ff",
    "gold": "#ffd700", "pink": "#ffc0cb", "brown": "#a52a2a", "beige": "#f5f5dc",
    "coral": "#ff7f50", "crimson": "#dc143c", "indigo": "#4b0082", "violet": "#ee82ee",
    "turquoise": "#40e0d0", "tan": "#d2b48c", "salmon": "#fa8072", "khaki": "#f0e68c",
}

GENERIC_FAMILIES = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded", "math", "emoji", "fangsong",
}

FONT_KEYWORDS = {
    "inherit", "initial", "unset", "revert", "revert-layer", "normal", "italic",
    "oblique", "bold", "bolder", "lighter", "caption", "icon", "menu", "message-box",
    "small-caption", "status-bar", "smaller", "larger", "small", "medium", "large",
    "x-small", "xx-small", "x-large", "xx-large", "none", "auto",
}


def _clamp(n, lo=0, hi=255):
    return max(lo, min(hi, n))


def _hsl_to_hex(h, s, l):
    return "#" + "".join(f"{_clamp(round(v * 255)):02x}" for v in colorsys.hls_to_rgb(h / 360 % 1, min(l, 100) / 100, min(s, 100) / 100))


def _channel(tok):
    """Parse an rgb channel token: '255', '100%' -> 0..255 int."""
    tok = tok.strip()
    if tok.endswith("%"):
        return _clamp(round(float(tok[:-1]) * 255 / 100.0))
    return _clamp(int(round(float(tok))))


def normalize_hex(color_str):
    """A CSS colour (hex, rgb(), hsl(), a name) as #rrggbb without its alpha, or None."""
    if not color_str:
        return None
    c = color_str.strip().lower()

    if re.fullmatch(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", c):   # the alpha goes
        return c[:7] if len(c) > 5 else f"#{c[1]*2}{c[2]*2}{c[3]*2}"

    if c in NAMED_COLORS:
        return NAMED_COLORS[c]

    try:                               # rgb(1.2.3, 0, 0), a 400-digit channel: no colour
        m = re.match(r"rgba?\(\s*([0-9.]+%?)[\s,]+([0-9.]+%?)[\s,]+([0-9.]+%?)", c)
        if m:
            return "#{:02x}{:02x}{:02x}".format(_channel(m.group(1)), _channel(m.group(2)), _channel(m.group(3)))
        m = re.match(r"hsla?\(\s*([0-9.]+)(?:deg)?[\s,]+([0-9.]+)%[\s,]+([0-9.]+)%", c)
        return _hsl_to_hex(float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else None
    except (ValueError, OverflowError):
        return None


def extract_colors(value):
    """All colours in a CSS value as hex; not words in a url(…) ('green-bg.png')."""
    if not value:
        return []
    scrubbed = CSS_URL_RE.sub("url(", value)

    def clear(token):                # fully see-through: no colour on the page
        if token.startswith("#"):
            return len(token) in (5, 9) and int(token[-1 if len(token) == 5 else -2:], 16) == 0
        alpha = re.search(r"[,/]\s*(\d*\.?\d+)(%?)\s*\)$", token) if token[:4].lower() in ("rgba", "hsla") or "/" in token else None
        return bool(alpha) and float(alpha.group(1) or 0) == 0

    colors = [h for h in (None if clear(m) else normalize_hex(m) for m in re.findall(r"#[0-9a-fA-F]{3,8}|(?:rgba?|hsla?)\([^)]*\)", scrubbed)) if h]
    low = scrubbed.lower()
    return colors + [h for name, h in NAMED_COLORS.items() if re.search(rf"(?<![\w-]){name}(?![\w-])", low)]


def extract_font_families(value, chosen=False, shorthand=False):
    """The family names in a font-family or font value: no sizes, keywords or generic families."""
    if not value:
        return []
    v = value.strip()

    # In a `font:` shorthand the family list follows the font-size (opt. /line-height).
    size = r"\d*\.?\d+(?:px|pt|em|rem|%|vw|vh|ex|ch)" + (r"|(?:xx?x?-)?(?:small|large)|medium|smaller|larger" if shorthand else "")   # (a family may be "Small Fonts")
    m = re.search(r"(?:^|\s)(?:" + size + r")(?:\s*/\s*\S+)?\s+", v, re.I)
    if m:
        v = v[m.end():]

    families = []
    for n, part in enumerate(v.split(",")):
        p = part.strip().strip("'\"").strip()
        pl = p.lower()
        if chosen and (n > 0 or pl in GENERIC_FAMILIES or pl.startswith(("ui-", "-apple", "system")) or "monospace" in v.lower()):
            break                       # chosen: the site's own first font only, not a fallback or a system/code stack
        # not keywords, sizes or leftovers ('12px/1.5', 'var(--x)'); names with digits ('Source Sans 3') stay
        if p and pl not in FONT_KEYWORDS and pl not in GENERIC_FAMILIES and not re.fullmatch(r"[\d.]+(px|pt|em|rem|%)?", pl) \
                and not re.search(r"\d(px|pt|em|rem|%|vw|vh)\b|[/()]", pl):
            families.append(p)
    return families


# --- Media a copy leaves out
# A copy keeps one size of each image and leaves out videos, the sound of audio players, images over MEDIA_LIMIT
# and, once the project's copies hold MEDIA_BUDGET of images, every further image. A same-size <waib-media>
# placeholder takes their place, the original element kept in a <template>; the app downloads it on request.

MEDIA_LIMIT = 2_000_000      # bytes per image
MEDIA_BUDGET = 50_000_000    # bytes of images per project (IMAGE_ENDINGS: a downloaded video doesn't count)
IMAGE_ENDINGS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".tif", ".tiff")
MEDIA_MAX = 500_000_000      # bytes: the most one download on request may be
IMAGE_WIDTH = 2000           # px: of an image's sizes (srcset), the largest up to this one is copied
MEDIA_RE = re.compile(r"<waib-media\b([^<>]*)>(.*?)</waib-media\s*>", re.S | re.I)
TYPE_ENDINGS = {"text/css": (".css",), "text/javascript": (".js", ".mjs"), "application/javascript": (".js", ".mjs"),
                "image/svg+xml": (".svg",), "image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"), "image/gif": (".gif",),
                "image/webp": (".webp",), "image/avif": (".avif",), "font/woff2": (".woff2",), "font/woff": (".woff",),
                "video/mp4": (".mp4", ".m4v"), "video/webm": (".webm",), "video/quicktime": (".mov",)}


def asset_filename(url, kind=""):
    """A downloaded file's name in scraped/assets/: its own, a hash of the address (two files of one name stay
    apart) and an ending for its content type when its own doesn't say it (style.php: .css)."""
    path = urlparse(url).path
    ext = Path(path).suffix
    stem = re.sub(r"[^\w.-]", "-", Path(path).stem or "asset")[:50]   # not inside the f-string: Python < 3.12 allows no backslash there
    name = f"{stem}-{hashlib.md5(url.encode()).hexdigest()[:8]}{ext}"
    typed = TYPE_ENDINGS.get((kind or "").split(";")[0].strip().lower())
    return name + typed[0] if typed and ext.lower() not in typed else name


def megabytes(n):
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1000))} KB" if n else ""


_ICONS = {"image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
          "video": '<rect x="2" y="5" width="15" height="14" rx="2"/><path d="M17 10l5-3v10l-5-3z"/>',
          "audio": '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
          "download": '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v3h16v-3"/>'}


def media_icon(name, size):
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true" style="display:block;flex:none;width:{size}px;height:{size}px">{_ICONS[name]}</svg>')


# the placeholder's own look, inline (a copy's stylesheets don't know the element); the copy adds position and size
MEDIA_STYLE = ("box-sizing:border-box;max-width:100%;flex-direction:column;align-items:center;justify-content:center;gap:6px;overflow:hidden;"
               "padding:8px;text-align:center;font:12px/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;color:#4b5260;")
HATCHED = "background:repeating-linear-gradient(135deg,#f1f3f5 0 10px,#e9ecef 10px 20px);border:1px dashed #a9b0ba;"
QUIET = "background:#f4f5f7;border:1px solid #dfe3e8;"
BUTTON = ("display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 10px;margin:2px 0 0;border:1px solid #d9dde3;border-radius:6px;"
          "background:#fff;color:#23272e;font:500 12px -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;cursor:pointer;")


def media_placeholder(kind, name, size, w, h, duration=0, poster=""):
    """(inner HTML, style, title) of a placeholder. Audio has no download: a design doesn't need its sound."""
    small = w < 160 or h < 110
    what = {"image": "Image not copied", "video": "Video not copied", "audio": "Audio"}[kind]
    meta = " · ".join(x for x in (name, megabytes(size), f"{duration // 60}:{duration % 60:02d}" if duration else "") if x)
    if kind == "audio":
        meta = (name + " · " if name else "") + "not copied, not needed for the design"
    title = f"{what}: {meta}"
    button = "" if kind == "audio" else (f'<button type="button" data-waib-download title="Download {escape_attr(name)}" style="{BUTTON}">'
                                         f'{media_icon("download", 13)}{"" if small else "Download"}</button>')
    if kind == "video" and poster:     # the poster stays (an image like any other), with a note over it
        inner = (f'<span style="display:flex;align-items:center;gap:8px;max-width:100%;padding:5px 6px 5px 12px;border-radius:999px;'
                 f'background:rgba(35,39,46,.82);color:#fff">{media_icon("video", 16)}<span>{"" if small else what + (" · " + megabytes(size) if size else "")}</span>{button}</span>')
        return inner, f"background:#23272e url({poster}) center/cover no-repeat;border:0;", title
    look = QUIET if kind == "audio" else HATCHED
    if small:                          # icon and button side by side, the rest in the title
        return media_icon(kind, 22) + button, look + "flex-direction:row;flex-wrap:wrap;padding:4px;gap:4px;", title
    return (f'{media_icon(kind, 32)}<span style="font-weight:600;color:#23272e">{what}</span>'
            f'<span style="font-size:11px;color:#5f6570;max-width:100%;overflow-wrap:anywhere">{escape_attr(meta)}</span>{button}'), look, title


def escape_attr(text):
    return str(text).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def media_in(html):
    """The media a copy left out: [{kind, src, bytes, name}] from its placeholders."""
    out = []
    for m in MEDIA_RE.finditer(html):
        a = dict(attributes("<x" + m.group(1) + ">"))
        if a.get("data-src"):
            out.append({"kind": a.get("data-kind", "image"), "src": one_line(a["data-src"]), "name": one_line(a.get("data-name", ""))[:120],
                        "bytes": int(a["data-bytes"]) if str(a.get("data-bytes", "")).isdigit() else 0})
    return out


def restore_media(html, src=None, local=None, stamp=None):
    """The original elements back in place of the placeholders (all, or src's). local: the downloaded file replaces
    src; stamp marks the element as just downloaded (the app shows that)."""
    def back(m):
        a = dict(attributes("<x" + m.group(1) + ">"))
        if src is not None and a.get("data-src") != src:
            return m.group(0)
        t = re.search(r"<template\b[^<>]*>(.*)</template\s*>", m.group(2), re.S | re.I)
        if not t:
            return m.group(0)
        inner = t.group(1)
        if local is not None:
            for form in dict.fromkeys((src, escape_attr(src))):
                inner = in_markup(inner, lambda markup, f=form: markup.replace(f, local))
            inner = re.sub(r"<source\b[^<>]*>", lambda s: s.group(0) if local in s.group(0) else "", inner, flags=re.I)   # so the downloaded version shows
            if stamp:
                inner = re.sub(r"^(\s*<[a-z][\w-]*)", lambda x: f'{x.group(1)} data-waib-fetched="{stamp}"', inner, count=1, flags=re.I)
        return inner
    return MEDIA_RE.sub(back, html)

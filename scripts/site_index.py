"""
Which pages a site has, for the app's page picker (standard library only): all of its sitemap if it has one,
else the links on up to AROUND pages around the user's page, and more when the user opens a folder; never a
full crawl. Kept in <project>/pages.json and shown as a tree of URL folders.
"""

import re
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from pyexpat import ExpatError, ParserCreate
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from common import UA, base_host, canonical, decode_text, is_same_domain, leads_inward, local_ip, private, read_dict, site_root, with_scheme, write_json

MAX_URLS = 200_000        # sitemap entries kept
MAX_SITEMAPS = 500        # sitemap files read (indexes can list thousands)
MAX_BYTES = 50_000_000    # per download
AROUND = 20               # pages whose links are read when there is no sitemap
SECTION = 20              # the most pages "the whole section" offers
AROUND_PICKS = 4          # "This page and 4 around it"
EXPLORE = 10              # pages read per opened folder
NOT_SITE = re.compile(r"^/(cdn-cgi|wp-admin|wp-json)/|/wp-login\.php$", re.I)   # the host's machinery, not pages
NOT_PAGES = re.compile(r"\.(pdf|zip|rar|7z|docx?|xlsx?|pptx?|jpe?g|png|gif|webp|svg|ico|mp[34]|mov|avi|wav|css|js|xml|txt|gz)$", re.I)


def web(url):
    return urlparse(url).scheme in ("http", "https")


class WebOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not web(newurl):            # urllib would follow ftp: too
            raise HTTPError(newurl, code, f"redirect to a non-web address: {newurl}", headers, fp)
        if leads_inward(newurl, req.full_url):   # a public page may not lead into this computer or network
            raise HTTPError(newurl, code, f"redirect to a local or private address: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _checked(connection):
    """A connection that refuses to end on this computer or network: a name can point elsewhere when
    urllib connects than when private() looked it up (DNS rebinding)."""
    class Checked(connection):
        def connect(self):
            super().connect()
            if local_ip(self.sock.getpeername()[0]):
                self.sock.close()
                raise OSError(f"{self.host} leads to this computer or its network")
    return Checked


class Public(HTTPSHandler, HTTPHandler):
    """Connections to a public site's addresses are checked (through a proxy, the proxy connects instead)."""
    def http_open(self, req):
        return self.do_open(HTTPConnection if req.has_proxy() else CHECKED_HTTP, req)

    def https_open(self, req):
        return self.do_open(HTTPSConnection if req._tunnel_host else CHECKED_HTTPS, req, context=self._context)


CHECKED_HTTP, CHECKED_HTTPS = _checked(HTTPConnection), _checked(HTTPSConnection)
OPEN, OPEN_LOCAL = build_opener(WebOnly, Public).open, build_opener(WebOnly).open   # OPEN_LOCAL: a site on this computer or network


def inflate(body, coding, url):
    """A compressed answer unpacked, at most MAX_BYTES of it (a gzip bomb stops there)."""
    for wbits in ((zlib.MAX_WBITS | 32,) if "gzip" in coding else (zlib.MAX_WBITS, -zlib.MAX_WBITS)):   # deflate: wrapped or raw
        try:
            body = zlib.decompressobj(wbits).decompress(body, MAX_BYTES + 1)
            break
        except zlib.error as e:
            error = e
    else:
        raise ValueError(f"{url}: the compressed answer can't be read ({error})")
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url} is larger than {MAX_BYTES // 1_000_000} MB")
    return body


def fetch(url, timeout=15, local=False):
    """(final url, body bytes, content type) of a GET; raises OSError/ValueError. local: the site is on this computer or network."""
    if not web(url):                   # never file: and the like
        raise ValueError(f"not a web address: {url}")
    with (OPEN_LOCAL if local else OPEN)(Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"}), timeout=timeout) as r:
        body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(f"{url} is larger than {MAX_BYTES // 1_000_000} MB")
        coding = r.headers.get("Content-Encoding", "identity").lower()   # some servers compress although asked not to
        if coding not in ("identity", "", "gzip", "x-gzip", "deflate"):
            raise ValueError(f"the server sent the page compressed ({coding}), which can't be read here")
        return r.geturl(), body if coding in ("identity", "") else inflate(body, coding, url), r.headers.get("Content-Type", "")


def parse_xml(body):
    """A sitemap's root element. A sitemap with a DTD is refused: its entities could
    expand to gigabytes ("billion laughs"; the expat of older Pythons has no limit)."""
    def refuse(*_):
        raise ValueError("a sitemap with a DTD")
    check = ParserCreate()
    check.StartDoctypeDeclHandler = check.EntityDeclHandler = refuse
    check.Parse(body, True)            # stops at the DTD, before anything is expanded
    return ET.fromstring(body)


def natural_key(text):
    """Sorts "News 2" before "News 10"."""
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"([0-9]+)", text))]   # (not "¹": isdigit, but no int)


def decode(body, content_type):
    m = re.search(r"charset=[\"']?([\w-]+)", content_type) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
    charset = m and m.group(1)
    return decode_text(body, charset.decode() if isinstance(charset, bytes) else charset)


class Links(HTMLParser):
    """The page title and every <a href> of an HTML page."""

    def __init__(self, base):
        super().__init__()
        self.base, self.links, self.title, self._in_title, self._titled = base, [], "", False, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        elif tag in ("a", "area") and a.get("href") and not a["href"].strip().startswith("#"):   # image maps are menus on old sites; "#" is this page
            try:
                self.links.append(urljoin(self.base, a["href"].strip()))
            except ValueError:              # e.g. "http://[x/": one bad link, not the whole page
                pass
        elif tag == "title" and not self._titled:   # the page's title, not one inside an inline SVG
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title, self._titled = False, True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def here(url, home):
    """The same path on `home`'s scheme and host."""
    p, h = urlparse(url), urlparse(home)
    return p._replace(scheme=h.scheme, netloc=h.netloc).geturl()


def page_url(url, site):
    """The canonical URL if it is a web page on `site`, else None; on the site's own scheme and host
    (http or https, www. or not), so a page is listed once."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or NOT_PAGES.search(p.path) or NOT_SITE.search(p.path) or not is_same_domain(url, site):
            return None
        return canonical(here(url, site))
    except ValueError:                  # a malformed address (e.g. "http://[x/")
        return None


def folder_path(folder):
    return tuple(s for s in folder.strip("/").split("/") if s)


def segments(url):
    """URL -> folder path: '/a/b/c.html?x=1' -> ('a', 'b', 'c.html', '?x=1'); the homepage is ()."""
    p = urlparse(url)
    segs = tuple(unquote(s) for s in p.path.strip("/").split("/") if s)
    return segs + (("?" + p.query,) if p.query else ())


def pretty(seg):
    """A readable name for a URL segment: 'kita-platz_anmelden.html' -> 'Kita platz anmelden'."""
    name = re.sub(r"\.(s?html?|php|aspx?|jsp)$", "", seg, flags=re.I).lstrip("?")
    name = re.sub(r"[-_+=&]+", " ", name).strip() or seg
    return name[:1].upper() + name[1:]


class SiteIndex:
    """The pages of one site. Thread-safe; discovery and exploring run in the background."""

    def __init__(self, project):
        self.file = Path(project) / "pages.json"
        data = read_dict(self.file)
        self.lock = threading.Lock()
        self.asked = data.get("asked", "")          # the address looked up (start: where it led)
        self.start = data.get("start", "")          # the user's page
        self.home = data.get("home", "")            # https://host/
        self.source = data.get("source", "")        # sitemap | links
        self.titles = data.get("pages", {})         # url -> title ('' if unknown)
        self.read = set(data.get("read", []))       # pages whose links were read
        self.busy, self.error = False, data.get("error", "")
        self.local = bool(self.home) and private(self.home, unsure=False)   # the site is on this computer or network
        self._segs, self._shared = {}, None
        self.names = {}                              # url -> the user's own name for the page (project.json)

    # --- state ------------------------------------------------------------------
    def status(self):
        with self.lock:
            return {"asked": self.asked, "start": self.start, "home": self.home, "source": self.source, "count": len(self.titles),
                    "busy": self.busy, "error": self.error, "domain": urlparse(self.home).netloc}

    def save(self):
        with self.lock:
            data = {"asked": self.asked, "start": self.start, "home": self.home, "source": self.source, "error": self.error,
                    "read": sorted(self.read), "pages": self.titles}
        write_json(self.file, data)

    def add(self, url, title="", copied=False):
        """Call with the lock held. copied: the title of the page's copy, which replaces an older one (the site changed it)."""
        twin = url[:-1] if url.endswith("/") and len(urlparse(url).path) > 1 else url + "/"
        if url not in self.titles and twin in self.titles:   # /a and /a/: one page, the form seen first
            url = twin
        title = " ".join(title.split())[:200]
        if url not in self.titles or (title and (not self.titles[url] or copied and self.titles[url] != title)):
            self.titles[url] = title
            self._shared = None

    def segs(self, url):
        s = self._segs.get(url)
        if s is None:
            s = self._segs[url] = segments(url)
        return s

    # --- discovery ----------------------------------------------------------------
    def discover(self, url):
        """Start reading the site of `url` in the background (replaces any earlier index)."""
        with self.lock:
            if self.busy:
                return False
            self.busy, self.error, self.asked = True, "", canonical(with_scheme(url))
            self._reset()                   # nothing of the previous site while this one loads
        threading.Thread(target=self._discover, args=(url,), daemon=True).start()
        return True

    def _reset(self, start="", home=""):
        """Forget the site read so far (with the lock held)."""
        self.start, self.home, self.source, self.titles, self.read, self._segs = start, home, "", {}, set(), {}

    def forget(self, error):
        """An address that isn't one: show nothing of the previous site (Start must not use it)."""
        with self.lock:
            if self.busy:
                return
            self._reset()
            self.error, self.asked = "", ""   # (the last good address is looked up again on the next visit)
        self.save()                         # the error is for now, not for the next visit
        with self.lock:
            self.error = error

    def _discover(self, url):
        try:
            url = with_scheme(url)
            self.local = private(url, unsure=False)
            try:
                try:
                    final, body, ctype = fetch(url, local=self.local)
                except URLError as e:           # no answer over https (old sites often have no working certificate): try http
                    if isinstance(e, HTTPError) or not url.startswith("https://"):
                        raise
                    url = "http://" + url[len("https://"):]   # the address that answers (or the error) is the http one
                    final, body, ctype = fetch(url, local=self.local)
            except (OSError, ValueError) as e:
                with self.lock:
                    self._reset(canonical(url))
                    self.titles = {canonical(url): ""}
                    why = (e.msg if 300 <= e.code < 400 else f"HTTP {e.code}" + (" (the site blocks automated requests)" if e.code in (401, 403, 429) else "")) \
                        if isinstance(e, HTTPError) else getattr(e, "reason", e)   # a refused redirect says why
                    self.error = f"Could not open {url}: {why}"
                return
            if leads_inward(final, url):   # a public address that leads into this computer or network
                with self.lock:
                    self._reset()
                    self.error = f"{url} redirects to a local or private address ({final}): not read."
                return
            start = canonical(final)
            home = site_root(start)
            parser = Links(final)
            parser.feed(decode(body, ctype))
            with self.lock:
                self._reset(start, home)
                self.read = {start}
                self.add(start, parser.title)
                for link in parser.links:
                    u = page_url(link, home)
                    if u:
                        self.add(u)
            if self._read_sitemaps(home, start):
                with self.lock:
                    self.source = "sitemap"
                    self.add(start, parser.title)   # a sitemap does not always list every page
            else:
                with self.lock:
                    self.source = "links"
                self._read_around(start, AROUND - 1)
        except Exception as e:  # shown to the user, never fatal for the app
            with self.lock:
                self.error = f"Reading the site failed: {e}"
        finally:
            with self.lock:
                self.busy = False
            self.save()

    def _read_sitemaps(self, home, start=""):
        """Collect page URLs from the site's sitemaps; False if it has none."""
        queue, local = [], self.local
        try:
            _, body, ctype = fetch(urljoin(home, "/robots.txt"), local=local)
            queue = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", decode(body, ctype))
        except (OSError, ValueError):
            pass
        public = {}

        def ours(u):                    # the site's own addresses; a site running here from its code: those of its one public host too, moved here
            if not web(u) or not local and not is_same_domain(u, home):
                return None
            if is_same_domain(u, home):
                return u
            return here(u, home) if public.setdefault("host", base_host(urlparse(u).netloc)) == base_host(urlparse(u).netloc) else None
        queue = [m for m in map(ours, queue) if m] or [urljoin(home, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]

        def read(sm):
            """(is an index, the <loc> entries) of one sitemap file; None if unreadable."""
            if leads_inward(sm, home):         # robots.txt may name a www. twin that points home
                return None
            for attempt in range(4):                # busy servers answer 429/503: wait and ask again
                try:
                    _, body, _ = fetch(sm, timeout=30, local=local)
                    break
                except HTTPError as e:
                    if e.code not in (429, 502, 503, 504) or attempt == 3:
                        return None
                    wait = e.headers.get("Retry-After", "")
                    time.sleep(min(int(wait) if wait.isdigit() else 2 ** attempt, 10))
                except (OSError, ValueError):
                    return None
            try:
                root = parse_xml(inflate(body, "gzip", sm) if body[:2] == b"\x1f\x8b" else body)   # sitemap.xml.gz
            except (OSError, ValueError, ET.ParseError, ExpatError):
                return None
            return root.tag.endswith("sitemapindex"), [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]

        seen, found = set(), False
        with ThreadPoolExecutor(4) as pool:        # big sites split their sitemap into hundreds of files
            while queue and len(seen) < MAX_SITEMAPS:
                # the sitemaps of the user's part of the site first (de-DE, /de/de/): big sites list
                # other countries first, and the page limit would be full before theirs is read
                mine = [s.lower() for s in segments(start)[:2] if len(s) >= 2]
                near = lambda u: not any(re.search(r"(^|[^a-z])" + re.escape(s) + r"([^a-z]|$)", urlparse(u).path.lower()) for s in mine)   # noqa: E731  (the path: a .de host is in every URL)
                batch = sorted((u for u in dict.fromkeys(queue) if u not in seen), key=near)[:MAX_SITEMAPS - len(seen)]
                seen.update(batch)
                queue = []
                for result in pool.map(read, batch):
                    if not result:
                        continue
                    is_index, locs = result
                    if is_index:
                        queue += [m for m in map(ours, locs) if m]
                        continue
                    with self.lock:
                        for loc in locs:
                            if len(self.titles) >= MAX_URLS:
                                return True
                            m = ours(loc)
                            u = page_url(m, home) if m else None
                            if u:
                                self.add(u)
                                found = True
        return found

    def _nearest(self, url, n, under=None):
        """Up to n known, unread pages closest to `url` (sharing the longest folder prefix, then shallow first)."""
        mine = self.segs(url)

        def distance(u):
            s = self.segs(u)
            common = next((i for i, (a, b) in enumerate(zip(mine, s)) if a != b), min(len(mine), len(s)))
            return (len(mine) - common) + (len(s) - common), len(s), u

        with self.lock:
            pool = [u for u in self.titles if u not in self.read and (under is None or self.segs(u)[:len(under)] == under)]
        return sorted(pool, key=distance)[:n]

    def _read_around(self, url, n, under=None):
        """Read up to n pages near `url`, again and again: each page read may link to nearer ones."""
        while n > 0:
            batch = self._nearest(url, n, under)
            if not batch:
                return
            self._read_links(batch)
            n -= len(batch)

    def _read_links(self, urls):
        for u in urls:
            try:
                if leads_inward(u, self.home or u):   # a www. twin that points into this computer or network
                    raise ValueError("a local or private address")
                final, body, ctype = fetch(u, local=self.local)
            except (OSError, ValueError):
                with self.lock:
                    self.read.add(u)
                continue
            parser = Links(final)
            parser.feed(decode(body, ctype))
            with self.lock:
                self.read.add(u)
                self.add(u, parser.title)
                for link in parser.links:
                    v = page_url(link, self.home)
                    if v:
                        self.add(v)

    def explore(self, folder):
        """Read more links in a folder ('a/b'), for sites without a sitemap."""
        prefix = folder_path(folder)
        with self.lock:
            if self.busy or self.source != "links":
                return False
            self.busy = True

        def run():
            try:
                self._read_around(self.home + "/".join(prefix), EXPLORE, under=prefix)
            finally:
                with self.lock:
                    self.busy = False
                self.save()
        threading.Thread(target=run, daemon=True).start()
        return True

    # --- views ------------------------------------------------------------------------
    def label(self, url):
        """The user's name for the page, else its title, unless many pages share it
        (old sites often reuse one): then its address."""
        if self.names.get(url):
            return self.names[url]
        shared = self._shared
        if shared is None:
            with self.lock:
                counts = Counter(t for t in self.titles.values() if t)
            shared = self._shared = {t for t, n in counts.items() if n > 1}
        s, title = self.segs(url), self.titles.get(url, "")
        return title if title and title not in shared else (pretty(s[-1]) if s else "Home")

    def tree(self, folder=""):
        """The folder's own page and its children: [{name, path, url, count, folder, label}];
        trail: the folders' names from the top, for the crumb."""
        prefix = folder_path(folder)
        n = len(prefix)
        nodes = {}
        with self.lock:
            urls = list(self.titles)
        for u in urls:
            s = self.segs(u)
            if s[:n] != prefix or len(s) == n:
                continue
            node = nodes.setdefault(s[n], {"name": s[n], "path": "/".join(s[:n + 1]), "url": None, "pages": set()})
            node["pages"].add(s)            # /a and /a/ are one place in the tree
            if len(s) == n + 1 and (not node["url"] or not self.titles.get(node["url"]) and self.titles.get(u)):
                node["url"] = u             # of two, the one that was read (it has a title)
        out = []
        for node in nodes.values():
            node["count"] = len(node.pop("pages"))
            node["folder"] = node["count"] > (1 if node["url"] else 0)
            node["label"] = self.label(node["url"]) if node["url"] else pretty(node["name"])
            out.append(node)
        own = next((u for u in urls if self.segs(u) == prefix), None)
        out.sort(key=lambda x: (x["folder"] and not x["url"], natural_key(x["label"])))
        return {"path": "/".join(prefix), "url": own, "trail": [pretty(s) for s in prefix], "label": self.label(own) if own else (pretty(prefix[-1]) if prefix else self.domain()),
                "count": sum(x["count"] for x in out) + (1 if own else 0), "children": out}

    def under(self, folder=""):
        """Every page in the folder and its subfolders (its own page too), once each (/a and /a/ are one)."""
        prefix = folder_path(folder)
        with self.lock:
            urls = list(self.titles)
        found = {}
        for u in urls:
            s = self.segs(u)
            if s[:len(prefix)] == prefix and (s not in found or not self.titles.get(found[s]) and self.titles.get(u)):
                found[s] = u                    # of two, the one that was read (it has a title)
        return sorted(found.values(), key=self.natural)

    def natural(self, url):
        return [natural_key(seg) for seg in self.segs(url)]

    def domain(self):
        return urlparse(self.home).netloc or "Site"

    def search(self, q, limit=50):
        q = q.lower().strip()
        with self.lock:
            items = list(self.titles.items())
        flat = lambda s: re.sub(r"[-_+=&]+", " ", s.lower())   # noqa: E731  ("team 3" finds /team-3)
        hits = [u for u, t in items if q in u.lower() or q in t.lower() or flat(q) in flat(u) or q in self.label(u).lower()]   # also the shown and the user's names
        return [{"url": u, "label": self.label(u), "path": "/".join(self.segs(u))} for u in hits[:limit]]

    def options(self):
        """The Start screen's choices for the user's page: AROUND_PICKS pages around it, and its section if small."""
        start = self.start
        if not start:
            return {"around": [], "section": None}
        mine = self.segs(start)
        parent = mine[:-1]
        with self.lock:
            urls = list(self.titles)
        natural = self.natural
        parent_page = next((u for u in urls if self.segs(u) == parent), None) if mine else None
        siblings = sorted((u for u in urls if len(self.segs(u)) == len(mine) and self.segs(u)[:-1] == parent and u != start), key=natural)
        # nearest siblings first: alternate after and before the user's page in that order
        before = [u for u in siblings if natural(u) < natural(start)][::-1]
        after = [u for u in siblings if natural(u) > natural(start)]
        near = [u for pair in zip(after, before) for u in pair] + after[len(before):] + before[len(after):]
        children = sorted((u for u in urls if self.segs(u)[:-1] == mine and u != start), key=natural)
        section = list({self.segs(u): u for u in urls if self.segs(u)[:len(parent)] == parent}.values()) if mine else []   # /a and /a/ once
        section.sort(key=natural)
        # four around it: parent, nearest siblings, children; then the section (file-style sites), then the
        # site's shallowest pages (a homepage whose pages all sit in folders)
        shallow = sorted(urls, key=lambda u: (len(self.segs(u)), natural(u)))
        picks = [u for u in ([parent_page] if parent_page else []) + near + children + section + shallow if u and u != start]
        around = [start] + list(dict.fromkeys(picks))[:AROUND_PICKS]
        return {"around": around,
                "section": {"path": "/".join(parent), "count": len(section), "pages": section if len(section) <= SECTION else []}}

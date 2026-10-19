#!/usr/bin/env python3
"""
Copy pages of a website with Playwright: the rendered HTML and its assets.

  scrape_site.py <url> [<url> …] --project-dir <project>

Each page is saved as <slug>.html (its address relative to the site root; the homepage is index.html)
in <project>/scraped/ and listed in its sitemap.json (url, title, filename, network facts, when copied).
Media a copy leaves out get placeholders (common.py, "Media a copy leaves out").
"""

import argparse
import re
import sys
import time
from glob import escape as glob_escape
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_index  # noqa: E402
from measure import DESKTOP  # noqa: E402
from common import (  # noqa: E402
    CONSENT_PARTS, CSS_IMPORT_RE, HOST, IMAGE_ENDINGS, IMAGE_WIDTH, MEDIA_BUDGET, MEDIA_LIMIT, MEDIA_STYLE, SRCSET_RE, UA, asset_filename, blocks, canonical,
    css_code, decode_text, extract_css_urls, guard_public, in_markup, is_same_domain, local_ip, media_placeholder, megabytes, now, one_line, parse_srcset, plural,
    private, project_lock, read_dict, read_text, site_root, sitemap_pages, strip_scripts, url_to_filename, with_scheme, write_json, write_text,
)


def extract_asset_urls(page, base_url):
    """The files a page loads: images, stylesheets, icons, media and url(…) in its styles (not its scripts: a copy has none)."""
    assets, page_itself = set(), base_url.split("#")[0]

    def add(value):
        if value and not value.strip().startswith(("data:", "#")):   # url(#gradient) points into the page
            u = urljoin(base_url, value.strip()).split("#")[0]
            if u != page_itself:               # never the page as its own asset (its path would replace words in it)
                assets.add(u)

    # attribute -> the elements that load a file through it (modern sites ship images via srcset only)
    for attr, sel in (("src", "img, source, video, audio, input[type=image]"), ("poster", "video"),
                      ("href", 'link[rel="stylesheet"], link[rel*="icon"], link[rel="preload"], image, use, feImage'),
                      ("xlink:href", "image, use, feImage"), ("background", "body, table, td, th")):   # use: sprite.svg#icon, the file
        for el in page.query_selector_all(sel):
            add(el.get_attribute(attr))
    for el in page.query_selector_all("img[srcset], source[srcset], link[imagesrcset]"):
        for u in parse_srcset(el.get_attribute("srcset") or el.get_attribute("imagesrcset")):
            add(u)
    for el in page.query_selector_all("[style], style"):   # inline styles, <style> (also inside an <svg>)
        for u in extract_css_urls(el.get_attribute("style") if el.get_attribute("style") is not None else el.text_content() or ""):
            add(u)
    return assets


MAX_ASSET = 20_000_000  # bytes per asset
MAX_RUN = 500_000_000   # bytes of assets per copy run
MAX_ASSETS = 2000        # files per run
MAX_SIZE_ASKS = 30       # per page: images whose size the page didn't tell, asked of the server


def read_css(path):
    """A downloaded stylesheet as text, in its own charset (old sites: iso-8859-1); it is saved
    back as UTF-8, so its @charset rule goes."""
    data = path.read_bytes()
    m = re.match(rb"""\s*@charset\s+["']([\w-]+)["']\s*;""", data)
    text = decode_text(data, m and m.group(1).decode())
    return re.sub(r"""^\ufeff?\s*@charset\s+["'][\w-]+["']\s*;""", "", text)


def opener(local_ok):
    """urllib's, with every redirect and connection checked (site_index): local_ok, the site itself is local."""
    return site_index.OPEN_LOCAL if local_ok else site_index.OPEN


def size_of(url, local_ok):
    """A file's size as its server says (Content-Length of a HEAD), or 0."""
    try:
        with opener(local_ok)(Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=5) as r:
            size = r.headers.get("Content-Length", "")
        return int(size) if size.isdigit() else 0
    except Exception:
        return 0


def copied_before(assets_dir, url):
    """The files of url already in assets/ (maybe with a type ending), not a download still under way (.part)."""
    return sorted(f for f in assets_dir.glob(glob_escape(asset_filename(url)) + "*") if f.suffix != ".part")


def download_asset(url, assets_dir, local_ok, room):
    """Download an asset (again once per run: the site may have changed it) and return its file name, or None.
    An image counts against MEDIA_LIMIT and room["left"] by its real size (a new one replaces the old file).
    room["final"]: url -> the address it came from after redirects (a stylesheet's own references resolve there)."""
    old = copied_before(assets_dir, url)
    if url in room["fetched"]:
        return old[0].name if old else None
    room["fetched"].add(url)
    try:
        if not local_ok and private(url):
            raise ValueError("it leads to a local or private address")
        with opener(local_ok)(Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            body, kind, coding, room["final"][url] = site_index.read_all(r, MAX_ASSET, 120), r.headers.get("Content-Type", ""), r.headers.get("Content-Encoding", "").lower(), r.geturl()
        if coding in ("gzip", "x-gzip", "deflate"):
            body = site_index.inflate(body, coding, url)
        if len(body) > MAX_ASSET:
            raise ValueError(f"larger than {MAX_ASSET // 1_000_000} MB")
        if room["bytes"] + len(body) > MAX_RUN:
            raise ValueError(f"past {MAX_RUN // 1_000_000} MB of files in this copy")
        name = asset_filename(url, kind)   # style.php, /css?family=…: an ending for its type (the CSS step and an export's host need it)
        if kind.startswith("image/") or Path(name).suffix.lower() in IMAGE_ENDINGS:
            more = len(body) - (old[0].stat().st_size if old else room["charged"].get(url, 0))   # counted already: the copy's file, or what leave_out was told
            if len(body) > MEDIA_LIMIT or more > max(room["left"], 0):
                raise ValueError("an image over 2 MB, or past the project's 50 MB of images: it loads from the site")
            room["left"] -= more
            room["charged"][url] = len(body)
        room["bytes"] += len(body)
        (assets_dir / name).write_bytes(body)
        return name
    except Exception as e:
        print(f"  Note: not copied: {url} ({one_line(str(e))[:200]})", file=sys.stderr)
        return old[0].name if old else None


# a srcset's [url, descriptor] candidates, read as common.SRCSET_RE reads them
CANDIDATES = r"""set => [...set.matchAll(/__SRCSET__/g)].map(m => [m[1].replace(/,+$/, ''), (m[3] || '').trim()]).filter(([u]) => u)""" \
    .replace("__SRCSET__", SRCSET_RE.pattern)

# Of each srcset, one size: the largest up to IMAGE_WIDTH wide (or 2x), else the smallest; an image's src
# becomes that one too. Then the media the copy may leave out, each marked with data-waib-media.
MEDIA_FIND = r"""(maxWidth) => {
  for (const x of document.querySelectorAll('waib-media')) x.replaceWith(...x.childNodes);   // the site's own: only this script makes placeholders
  const candidates = __CANDIDATES__;
  const pick = set => {
    const all = candidates(set).filter(([u]) => !/^data:/i.test(u))
      .map(([u, d]) => { const w = /^(\d+)w$/i.exec(d), x = /^([\d.]+)x$/i.exec(d);
        return {u, d, w: w ? +w[1] : 0, x: x ? +x[1] : w ? 0 : 1}; });
    if (all.length < 2) return null;
    const by = all.some(c => c.w) ? 'w' : 'x', limit = by === 'w' ? maxWidth : 2, fit = all.filter(c => c[by] <= limit);
    return fit.length ? fit.reduce((a, b) => b[by] > a[by] ? b : a) : all.reduce((a, b) => b[by] < a[by] ? b : a);
  };
  for (const [sel, attr] of [['img[srcset], source[srcset]', 'srcset'], ['link[imagesrcset]', 'imagesrcset']])
    for (const el of document.querySelectorAll(sel)) {
      const c = pick(el.getAttribute(attr));
      if (!c) continue;
      el.setAttribute(attr, c.u + (c.d ? ' ' + c.d : ''));
      if (el.localName === 'img' && el.hasAttribute('src')) el.setAttribute('src', c.u);
    }
  const sizes = {};
  for (const r of performance.getEntriesByType('resource')) sizes[r.name] = r.encodedBodySize || r.decodedBodySize || 0;   // (from the cache: unknown, not its header's size)
  const box = el => { const r = el.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height)}; };
  const found = [];
  let n = 0;
  for (const v of document.querySelectorAll('video, audio')) {
    const srcs = [v.getAttribute('src'), ...[...v.querySelectorAll('source')].map(s => s.getAttribute('src'))].filter(u => u && /^https?:/i.test(u));
    const src = /^https?:/i.test(v.currentSrc) ? v.currentSrc : srcs.find(u => /\.mp4(\?|$)/i.test(u)) || srcs[0];
    if (!src) continue;              // a stream (blob:) or nothing to download
    v.dataset.waibMedia = ++n;
    found.push({id: n, kind: v.localName, src, poster: v.getAttribute('poster') || '', duration: isFinite(v.duration) ? Math.round(v.duration) : 0, ...box(v)});
  }
  for (const img of document.querySelectorAll('img')) {
    const inPicture = img.parentElement && img.parentElement.localName === 'picture', set = img.getAttribute('srcset');
    const src = inPicture && /^https?:/i.test(img.currentSrc) ? img.currentSrc : set ? (candidates(set)[0] || [])[0] : img.getAttribute('src');   // a picture: what the browser chose
    if (!src || !/^https?:/i.test(src)) continue;
    img.dataset.waibMedia = ++n;
    found.push({id: n, kind: 'image', src, bytes: img.complete && img.naturalWidth ? sizes[src] || 0 : 0, ...box(img)});   // (a load that stopped tells too little)
  }
  return found;
}""".replace("__CANDIDATES__", CANDIDATES)

# Each left-out medium becomes a <waib-media> placeholder where it was (an image in a <picture>: the picture),
# with its place in the layout (position, margins, size) and the original element kept in a <template>.
MEDIA_REPLACE = r"""(items) => {
  const keep = ['position', 'top', 'right', 'bottom', 'left', 'z-index', 'float', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
                'flex', 'align-self', 'justify-self', 'grid-area', 'order', 'vertical-align'];
  for (const it of items) {
    const el = document.querySelector('[data-waib-media="' + it.id + '"]');
    if (!el) continue;
    const target = el.localName === 'img' && el.parentElement && el.parentElement.localName === 'picture' ? el.parentElement : el;
    const cs = getComputedStyle(target), r = target.getBoundingClientRect(), shown = r.width > 0 && r.height > 0 && cs.display !== 'none';
    const ph = document.createElement('waib-media');
    for (const [k, v] of [['kind', it.kind], ['src', it.src], ['bytes', String(it.bytes || '')], ['name', it.name]]) ph.setAttribute('data-' + k, v);
    ph.title = it.title;
    if (target.getAttribute('class')) ph.setAttribute('class', target.getAttribute('class'));   // (a phone-only image stays phone-only)
    const hidden = target.hidden || /display\s*:\s*none/i.test(target.getAttribute('style') || '');
    const plain = new Set(['static', 'auto', '0px', 'none', '0 1 auto', '0', 'baseline', 'normal']);   // the defaults need no saying
    ph.setAttribute('style', keep.map(p => [p, cs.getPropertyValue(p)]).filter(([p, v]) => /^(top|right|bottom|left)$/.test(p) ? v !== 'auto' && cs.position !== 'static' : !plain.has(v)).map(([p, v]) => p + ':' + v + ';').join('') + it.style
      + 'width:' + Math.round(r.width) + 'px;height:' + Math.round(r.height) + 'px;' + (hidden ? 'display:none' : !shown ? '' : 'display:' + (cs.display.startsWith('inline') ? 'inline-flex' : 'flex')));
    const t = document.createElement('template'), original = target.cloneNode(true);
    for (const x of [original, ...original.querySelectorAll('[data-waib-media]')]) x.removeAttribute('data-waib-media');
    t.content.appendChild(original);
    ph.innerHTML = it.html;
    ph.prepend(t);
    target.replaceWith(ph);
  }
  for (const x of document.querySelectorAll('[data-waib-media]')) x.removeAttribute('data-waib-media');
}"""


def media_kept(assets_dir):
    """Bytes of images the project's copies hold already."""
    return sum(f.stat().st_size for f in assets_dir.glob("*") if f.suffix.lower() in IMAGE_ENDINGS and f.is_file())


def leave_out(page, local_ok, room, assets_dir):
    """One size per image, and placeholders for the media the copy leaves out. room (shared by the pages of a run):
    "left" bytes of images, the srcs "kept", "out" and "charged" (counted in "left"). Returns what was left out."""
    left, asked = [], 0
    for it in page.evaluate(MEDIA_FIND, IMAGE_WIDTH):
        src = it["src"]
        if not local_ok and private(src):   # never fetched anyway
            continue
        if copied_before(assets_dir, src):   # (also downloaded on the user's request): it stays
            room["kept"].add(src)
            continue
        size = it.get("bytes") or 0
        if it["kind"] == "image":
            if not size and asked < MAX_SIZE_ASKS:   # the page didn't say (another site's image)
                size, asked = size_of(src, local_ok), asked + 1
            if src in room["kept"] or size <= MEDIA_LIMIT and src not in room["out"] and size <= room["left"]:
                if src not in room["kept"] and size:   # an unknown size is counted when it downloads
                    room["left"] -= size
                    room["charged"][src] = size
                room["kept"].add(src)
                continue
            room["out"].add(src)
        elif it["kind"] == "video":
            size = size_of(src, local_ok)
        name = one_line(unquote(Path(urlparse(src).path).name) or urlparse(src).netloc)[:120]
        html, style, title = media_placeholder(it["kind"], name, size, it["w"], it["h"], it.get("duration") or 0, it.get("poster") or "")
        left.append({**it, "bytes": size, "name": name, "html": html, "style": MEDIA_STYLE + style, "title": title})
    page.evaluate(MEDIA_REPLACE, left)
    room["out"].update(x["src"] for x in left)   # never downloaded with the page's other files (a preload, a <source>, CSS)
    return left


def rewrite_asset_urls(html, asset_map):
    """The saved HTML's asset addresses pointed at their local copies: absolute ones (ABSOLUTIZE made them so)
    between URL delimiters, so a path in the text stays. style.css?v=2 has its own file."""
    local = {}
    for original_url, name in ((u, n) for u, n in asset_map.items() if n):
        parsed = urlparse(original_url)
        for cand in (original_url, f"//{parsed.netloc}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")):
            local[cand] = local[cand.replace("&", "&amp;")] = f"assets/{name}"   # saved HTML writes & as &amp; in attributes
    if not local:
        return html
    found = re.compile(r'(?:(?<=["\'(=,])|(?<=, ))(' + "|".join(re.escape(c) for c in sorted(local, key=lambda c: (-len(c), c)))
                       + r')(?=["\'\s)>,#])')   # (sprite.svg#icon)
    return in_markup(html, lambda text: found.sub(lambda u: local[u.group(1)], text))


def try_dismiss_cookies(page):
    """Close a consent banner by rejecting it ("Alle ablehnen", "Nur notwendige"), never accepting. A banner
    with no way to say no stays in the copy; the app and the screenshots hide it."""
    button = ':is(button, [role=button], input[type=submit], input[type=button])'   # consent tools often use links with role=button
    said = lambda *texts: [f'{button}:has-text("{t}")' for t in texts] + [f'input:is([type=submit], [type=button])[value="{t}" i]' for t in texts]   # noqa: E731
    # "Reject all" is a consent button anywhere; a plain "Ablehnen" only inside a consent box (else any button could match);
    # never a page form's button (it would send the form to the site)
    selectors = [f'{s}:not(form *)' for s in said("Alle ablehnen", "Alle Cookies ablehnen", "Reject all", "Decline all", "Deny all",
                                                    "Nur notwendige", "Nur essenzielle", "Nur erforderliche", "Only necessary", "Only essential",
                                                    "Necessary only", "Use necessary cookies only")] + \
        [f'{box} {w}:not(form *)' for box in CONSENT_PARTS for w in said("Ablehnen", "Nicht zustimmen", "Reject", "Decline", "Deny", "Refuse")]
    here = page.url
    for selector in selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click(timeout=2000)          # covered by a consent iframe: give up quickly
                page.wait_for_timeout(500)
                if canonical(page.url) != canonical(here):   # it was a page's own button (a form): back to the page
                    print(f"    Note: the consent click left the page ({page.url}); back to it", file=sys.stderr)
                    page.goto(here, wait_until="domcontentloaded", timeout=30000)
                return True
        except Exception:
            continue
    return False


SCROLL_THROUGH = r"""async () => {
  const step = innerHeight * 0.8, end = Math.min((document.scrollingElement || document.documentElement).scrollHeight, 40000);
  for (let y = 0; y < end; y += step) { scrollTo({top: y, behavior: 'instant'}); await new Promise(r => setTimeout(r, 120)); }
  scrollTo({top: 0, behavior: 'instant'});
}"""


# Make every address in the page absolute, so the saved copy keeps working from
# scraped/ (relative paths like ../img/x.png would break) and its asset URLs match
# the downloaded files exactly.
ABSOLUTIZE = r"""() => {
  // CSS-in-JS (styled-components, emotion) adds its rules through the CSSOM: they are not in the
  // HTML, so the copy would have no styles. Write them into their <style> elements.
  const text = rules => [...rules].map(r => r.cssText).join('\n');
  for (const s of document.querySelectorAll('style')) {
    try { const rules = s.sheet && s.sheet.cssRules;
      if (rules && rules.length > s.textContent.split('{').length - 1) s.textContent = text(rules); } catch (e) {}
  }
  for (const sheet of document.adoptedStyleSheets || []) {
    try { const s = document.createElement('style'); s.textContent = text(sheet.cssRules); document.head.append(s); } catch (e) {}
  }
  const abs = u => { try { return new URL(u, document.baseURI).href; } catch (e) { return u; } };
  const attrs = [['img', 'src'], ['source', 'src'], ['video', 'src'], ['video', 'poster'], ['audio', 'src'],
                 ['link', 'href'], ['a', 'href'], ['form', 'action'],
                 ['iframe', 'src'], ['frame', 'src'], ['body', 'background'], ['table', 'background'],
                 ['td', 'background'], ['th', 'background'], ['input', 'src'], ['area', 'href']];
  for (const [tag, attr] of attrs)
    for (const el of document.querySelectorAll(tag + '[' + attr + ']')) {
      const v = el.getAttribute(attr);
      if (v && !/^(#|data:|mailto:|tel:|javascript:)/i.test(v)) el.setAttribute(attr, abs(v));
    }
  for (const el of document.querySelectorAll('image, use, feImage'))   // SVG: href and xlink:href
    for (const name of ['href', 'xlink:href']) { const v = el.getAttribute(name); if (v && !/^(#|data:)/i.test(v)) el.setAttribute(name, abs(v)); }
  const candidates = __CANDIDATES__;   // a URL may hold commas (img.jpg?resize=1024,657 1024w)
  for (const attr of ['srcset', 'imagesrcset'])   // imagesrcset: a preload's srcset
    for (const el of document.querySelectorAll('[' + attr + ']'))
      el.setAttribute(attr, candidates(el.getAttribute(attr)).map(([u, d]) => [/^data:/i.test(u) ? u : abs(u), d])
        .filter(([u]) => !/^\s*file:/i.test(u)).map(([u, d]) => u + (d ? ' ' + d : '')).join(', '));
  // the copy's files are rewritten and served from elsewhere: no integrity hashes, no CORS mode (file:// refuses it)
  for (const el of document.querySelectorAll('[integrity], [crossorigin]')) { el.removeAttribute('integrity'); el.removeAttribute('crossorigin'); }
  // url(...) in style attributes and <style> blocks is relative to the page, not to scraped/
  // (unquoted: a quote in a style attribute is saved as &quot;, which the asset rewrite wouldn't see)
  const url = u => 'url(' + abs(u.trim()).replace(/[()\s'"]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')) + ')';
  const css = text => text.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^'"()\s]*))\s*\)/gi, (m, a, b, c) => {   // "photo (1).png" too
    const u = a ?? b ?? c ?? '';
    return !u.trim() || /^(data:|#)/i.test(u.trim()) ? m : url(u); })
    .replace(/@import\s+(['"])([^'"]+)\1/gi, (m, q, u) => '@import ' + url(u));   // @import "x.css" too
  for (const el of document.querySelectorAll('[style]')) el.setAttribute('style', css(el.getAttribute('style')));
  for (const el of document.querySelectorAll('style')) el.textContent = css(el.textContent);
  for (const el of document.querySelectorAll('base')) el.remove();   // a <base> would send the absolute addresses back to the site
  // What a copy must never do when it is opened from disk or in the app: load local files, or move on to another address.
  // <noscript> too: here it is inert text, but a copy rendered without scripts would bring its tags to life
  for (const el of document.querySelectorAll('object, embed, iframe[srcdoc], frame[srcdoc], meta[http-equiv=refresh i], noscript')) el.remove();
  // a text element whose text closes it (a script can set <style> to "</style><meta …>"): saved, it would read back as tags
  for (const el of document.querySelectorAll('style, script, xmp, iframe, noembed, noframes, plaintext, textarea, title'))
    if (el.textContent.toLowerCase().includes('</' + el.localName)) el.remove();
  for (const el of document.querySelectorAll('*'))
    for (const a of [...el.attributes]) if (/^\s*f\s*i\s*l\s*e\s*:/i.test(a.value) || /url\(\s*['"]?\s*f\s*i\s*l\s*e\s*:/i.test(a.value)) el.removeAttribute(a.name);   // (tabs inside count for nothing)
}""".replace("__CANDIDATES__", CANDIDATES)


def defuse(html):
    """The saved copy once more, outside the page: its own scripts could have put back what
    ABSOLUTIZE took out. Nothing in a copy may load local files or move on by itself."""
    html = strip_scripts(html)
    for tag in ("noscript", "object"):
        html = blocks(html, tag, lambda el: "")
    html = re.sub(r"<(?:embed|base)\b[^<>]*>", "", html, flags=re.I)
    html = re.sub(r"<meta\b[^<>]*>", lambda m: "" if re.search(r"""http-equiv\s*=\s*["']?\s*refresh""", unescape(m.group(0)), re.I)
                  else m.group(0), html, flags=re.I)   # also written with entities ("&#114;efresh")
    html = re.sub(r"""\ssrcdoc\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", html, flags=re.I)
    # file: addresses, where an address goes (an attribute, url(), a srcset candidate), never words in the text
    file = r"f[\t\n\r]*i[\t\n\r]*l[\t\n\r]*e[\t\n\r]*:"   # browsers drop tabs and line breaks in an address
    return in_markup(html, lambda m: re.sub(rf"""(?<=["'(=,])(\s*){file}[^"'\s),<>]*|(?<=, ){file}[^"'\s),<>]*""", r"\1", m, flags=re.I))


def unlink_private(html, css=False):
    """No address of this computer or network in a public site's copy (tags, <style>, stylesheets; not the text),
    also not written with escapes or backslashes ("http:\\2f\\2f 10.0.0.1/…", "http:\\\\10.0.0.1")."""
    def char(n):                     # as CSS reads an escape: out of range is U+FFFD
        return chr(n) if 0 < n <= 0x10FFFF and not 0xD800 <= n <= 0xDFFF else "\ufffd"

    def bad(text):                   # an escaped address that leads here
        text = re.sub(r"\\([0-9a-f]{1,6})\s?|\\(.)", lambda e: char(int(e.group(1), 16)) if e.group(1) else e.group(2), text, flags=re.I)
        text = re.sub(r"^(https?:)?[/\\]+", lambda e: (e.group(1) or "http:") + "//", text.strip().replace("\\", "/"), flags=re.I)
        return bool(re.match(r"https?://", text, re.I)) and private(text)

    # each quoted string, or unquoted url(…), once; an unclosed string ends at the line end (linear)
    token = re.compile(r"""(["'])((?:\\[\s\S]?|(?!\1)[^\\\n])*)(?:\1|(?=\n)|$)|(url\(\s*)((?:\\[0-9a-f]{1,6}\s?|\\[^\n0-9a-f]|[^"'()\s\\])+)(\s*\))""", re.I)

    def escaped(m):
        if m.group(1) is None:       # url(…) without quotes
            return m.group(3) + m.group(5) if "\\" in m.group(4) and bad(m.group(4)) else m.group(0)
        if "\\" not in m.group(2):
            return m.group(0)
        if bad(m.group(2)):
            return m.group(1) * 2
        inner = token.sub(escaped, m.group(2)) if "url(" in m.group(2).lower() else m.group(2)   # style="…url(…)…"
        return m.group(1) + inner + m.group(0)[len(m.group(1)) + len(m.group(2)):]

    known = {}                       # host -> leads here; past MAX_HOSTS lookups, a name counts as leading here (an address is read without DNS)

    def here(host):
        if host not in known:
            ip = re.fullmatch(r"[\d.]+|\[[\da-f:.]+\]", re.sub(r":\d+$", "", host), re.I)
            known[host] = private("http://" + host + "/") if ip or len(known) < MAX_HOSTS else True
        return known[host]
    address = re.compile(r"""(?:https?:)?//([^/"'\s)<>,\\]+)[^"'\s)<>,]*""", re.I)   # also //host (protocol-relative)

    def clean(code):
        code = token.sub(escaped, code)
        return address.sub(lambda m: "" if here(m.group(1)) else m.group(0), code)
    return clean(html) if css else in_markup(html, clean)


HOST_RE = re.compile(r"(?:https?:)?//(" + HOST + r")(?=[/\"'\s)?#,;]|$)", re.I)
MAX_HOSTS = 200


def public_hosts(output_path):
    """The other hosts a public site's copies load from (not links): the app's Content-Security-Policy lets
    a copy load only from the site, well-known sites and the ones the user allows of these."""
    found = set()

    def loads(markup):               # what a page loads from, not where its links lead
        if not re.match(r"<(a|area)\b", markup, re.I):
            found.update(h.lower().rstrip(".") for h in HOST_RE.findall(markup))
        return markup
    for f in sorted(output_path.glob("*.html")):
        in_markup(read_text(f), loads)
    for f in sorted((output_path / "assets").glob("*.css")):
        loads(read_text(f))
    return sorted(h for h in sorted(found)[:MAX_HOSTS] if "." in h and not private("http://" + h + "/"))


def utf8(html):
    """The copy is saved as UTF-8: replace the page's own charset declaration (old
    sites often say iso-8859-1), else every umlaut shows up garbled."""
    html = re.sub(r"<meta[^>]+charset\s*=[^>]*>", "", html, flags=re.I)
    return re.sub(r"(<head[^>]*>)", r'\1<meta charset="utf-8">', html, count=1, flags=re.I)


# How the live page loaded: the load event, bytes transferred (resources from other
# sites may report 0 without Timing-Allow-Origin, so this is a lower bound) and requests.
NETWORK = """() => {
  const n = performance.getEntriesByType('navigation')[0] || {}, r = performance.getEntriesByType('resource');
  const done = n.loadEventEnd > 0;     // not reached: the time waited, and say so
  return {load_ms: Math.round(done ? n.loadEventEnd : performance.now()), load_incomplete: !done, requests: r.length + 1,
          bytes: (n.transferSize || n.encodedBodySize || 0) + r.reduce((a, x) => a + (x.transferSize || x.encodedBodySize || 0), 0)};
}"""


def scrape(urls, output_dir):
    """Copy these pages (rendered HTML and their assets) into output_dir and its sitemap.json; a page copied again is refreshed."""
    output_path = Path(output_dir)
    assets_dir = output_path / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    home = site_root(urls[0])
    sitemap = read_dict(output_path / "sitemap.json")
    pages = {canonical(p["url"]): p for p in sitemap_pages(sitemap)}
    used = {p["filename"].lower(): u for u, p in pages.items()}
    failed = sitemap.get("failed") if isinstance(sitemap.get("failed"), dict) else {}   # url -> why (shown in the app)

    touched = set()                      # the pages and failures this run changed: merged into the file as it is then

    def skip(url, why, error=False):
        print(f"    Error: {why}" if error else f"    Skipped ({why})", file=sys.stderr if error else sys.stdout)
        failed[url] = why
        touched.add(url)

    def save(final=False):               # after each page, so a stopped run keeps its pages; merged with the file as it is then
        origins = public_hosts(output_path) if final and not site_private else None   # (DNS lookups: not under the lock)
        with project_lock(output_path.parent):
            disk = read_dict(output_path / "sitemap.json")
            merged = {canonical(p["url"]): p for p in sitemap_pages(disk)}
            merged.update({u: pages[u] for u in touched if u in pages})
            gone = disk.get("failed") if isinstance(disk.get("failed"), dict) else {}
            gone = {**{u: w for u, w in gone.items() if u not in touched}, **{u: w for u, w in failed.items() if u in touched}}
            write_json(output_path / "sitemap.json", {"home": home, "pages": sorted(merged.values(), key=lambda p: p["filename"]),
                                                      **({"local": True} if site_private else {"origins": origins if origins is not None else disk.get("origins") or []}),
                                                      **({"failed": gone} if gone else {})})
    done, aliased, all_asset_urls, this_run = [], [], set(), set()
    landed = {canonical(p["network"]["final_url"]): u for u, p in pages.items()   # final address -> the page that got there (also earlier runs)
              if not p.get("same_as") and isinstance(p.get("network"), dict) and isinstance(p["network"].get("final_url"), str)}
    site_private = private(urls[0], unsure=False)   # an intranet site may load intranet files; a public one may not
    room = {"left": MEDIA_BUDGET - media_kept(assets_dir), "kept": set(), "out": set(), "charged": {}, "fetched": set(), "final": {}, "bytes": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent=UA,
            viewport=DESKTOP,                 # as measure renders the copy
        )
        if not site_private:             # a public page loads nothing from this computer or network while it is copied
            guard_public(context)
        page = context.new_page()
        for n, raw in enumerate(urls, 1):
            url = canonical(raw)
            print(f"  [{n}/{len(urls)}] {url}")
            try:
                response = None
                for attempt in range(2):
                    try:
                        response = page.goto(raw, wait_until="domcontentloaded", timeout=30000)
                        break
                    except Exception as e:
                        if attempt == 1:
                            raise
                        print(f"    Retry: {e}")
                        time.sleep(3)
                for state, ms in (("load", 15000), ("networkidle", 5000)):   # a slow image must not stop the copy
                    try:
                        page.wait_for_load_state(state, timeout=ms)
                    except Exception:
                        pass
                if not response or response.status >= 400:
                    skip(url, f"HTTP {response.status if response else 'no response'}")
                    continue
                if private(page.url) and not site_private:   # also a www. twin or a rebinding name that points home
                    skip(url, f"it redirects to a local or private address: {page.url}")
                    continue
                server = response.server_addr()
                if site_private and server and not local_ip(server["ipAddress"].strip("[]")):   # "local" came from one DNS answer, the page from the internet
                    skip(url, f"it came from a public address ({server['ipAddress']}), not from this computer or network")
                    continue
                if not is_same_domain(page.url, home):   # e.g. bbc.co.uk -> bbc.com: copy what the visitor gets
                    print(f"    Note: redirects to {page.url}; copied from there")
                same = landed.get(canonical(page.url))
                refresh = same if same and same != url and same not in this_run else None   # the same page as one copied in an earlier run: copy it again
                if same and same != url and not refresh:   # redirects to a page copied a moment ago: one copy for both
                    pages[url] = {**pages[same], "url": url, "same_as": same}
                    failed.pop(url, None)
                    touched.add(url)
                    aliased.append(url)
                    print(f"    Same page as {same}: {pages[same]['filename']}")
                    continue
                landed[canonical(page.url)] = url
                network = page.evaluate(NETWORK)
                network["last_modified"] = response.headers.get("last-modified", "")
                network["final_url"] = page.url        # after redirects (http -> https)
                try_dismiss_cookies(page)
                page.evaluate(SCROLL_THROUGH)  # lazy images and scroll-in animations show up only when seen
                page.wait_for_timeout(1000)
                page.evaluate(ABSOLUTIZE)
                left = leave_out(page, site_private, room, assets_dir)
                if left:
                    kinds = ", ".join(f"{sum(x['kind'] == k for x in left)} {k}" for k in ("video", "image", "audio") if any(x["kind"] == k for x in left))
                    print(f"    Left out {kinds} ({megabytes(sum(x['bytes'] for x in left)) or 'size unknown'}): placeholders in the copy")
                all_asset_urls.update(extract_asset_urls(page, page.url))
                own = url in pages and not pages[url].get("same_as")   # (an address that led to another page gets its own file now)
                filename = pages[refresh]["filename"] if refresh else pages[url]["filename"] if own else url_to_filename(url, used)
                if page.url != network["final_url"] and private(page.url) and not site_private:   # moved on meanwhile (a script, a click)
                    skip(url, f"it went on to a local or private address: {page.url}")
                    continue
                html = defuse(page.content())
                html = utf8(html if site_private else unlink_private(html))   # (before the lock: DNS lookups)
                with project_lock(output_path.parent):   # (the app may be putting a downloaded image into it)
                    write_text(output_path / filename, html)
                pages[url] = {"url": url, "title": one_line(page.title()), "filename": filename, "network": network, "copied": now()}
                if refresh:                    # the page keeps its address; this one leads to it
                    pages[refresh], pages[url] = {**pages[url], "url": refresh}, {**pages[url], "same_as": refresh}
                    landed[canonical(page.url)] = refresh
                this_run.add(refresh or url)
                touched.update({url, refresh} - {None})
                done.append(filename)
                failed.pop(url, None)
                print(f"    \u2713 saved {filename}")
            except Exception as e:
                skip(url, one_line(str(e))[:300], error=True)
            finally:
                save()

        if not done:                   # nothing new to download: only addresses of pages copied before, or a failure
            browser.close()
            save(final=True)
            if aliased:
                return print(f"\nCopied 0 pages; {len(aliased)} of the addresses lead to pages copied before.")
            sys.exit("\nError: no page could be copied (site down, blocked, or every request failed — see above).")

        if room["left"] < 0 or any(u not in room["kept"] for u in room["out"]) and room["left"] < MEDIA_LIMIT:
            print(f"Note: the project's copies hold {MEDIA_BUDGET // 1_000_000} MB of images; further images are placeholders (the user can download them)")
        print(f"Downloading {len(all_asset_urls)} assets (and what their CSS uses)...")
        allowed = sorted(u for u in all_asset_urls if (site_private or not private(u)) and u not in room["out"])[:MAX_ASSETS]   # a public page can't fetch your network's files
        asset_map = {u: download_asset(u, assets_dir, site_private, room) for u in allowed}
        # what each stylesheet uses (old sites set logos as backgrounds), and the stylesheets it @imports
        queue, done_css = [(u, 0) for u, name in asset_map.items() if name and name.endswith(".css")], set()
        while queue:
            css_url, depth = queue.pop()
            if css_url in done_css or depth > 5:   # a hostile site can chain @imports forever
                continue
            done_css.add(css_url)
            css = read_css(assets_dir / asset_map[css_url])
            live = css_code(css)               # what the browser reads: not the comments
            refs = set(extract_css_urls(live)) | set(CSS_IMPORT_RE.findall(live))
            for ref in sorted(u for u in refs if not u.startswith("#") and not (re.fullmatch(r"[\w.-]+", u) and (assets_dir / u).is_file())):   # not done before (a local name only: never probe other paths)
                full = urljoin(room["final"].get(css_url, css_url), ref)   # (a stylesheet behind a redirect: from where it is)
                skip = full not in asset_map and (not site_private and private(full) or full in room["out"])   # (a left-out image as a background too)
                if not skip and full not in asset_map:
                    if len(asset_map) >= MAX_ASSETS:
                        print(f"  Warning: more than {MAX_ASSETS} assets, the rest is left out", file=sys.stderr)
                        break
                    asset_map[full] = download_asset(full, assets_dir, site_private, room)
                local = None if skip else asset_map[full]
                # not copied: its full address, so it loads from the site (relative, it would point into assets/)
                css = re.sub(r"""((?:url\(|@import)\s*['"]?)""" + re.escape(ref) + r"""(?=['")\s;])""", lambda m: m.group(1) + (local or full), css)
                if local and local.endswith(".css"):
                    queue.append((full, depth + 1))
            write_text(assets_dir / asset_map[css_url], css if site_private else unlink_private(css, css=True))   # a skipped local ref leaves nothing
        for filename in done:
            with project_lock(output_path.parent):
                write_text(output_path / filename, rewrite_asset_urls(read_text(output_path / filename), asset_map))
        browser.close()

    save(final=True)
    print(f"\nCopied {plural(len(done), 'page')}: {', '.join(done)}")


def main():
    parser = argparse.ArgumentParser(description="Copy pages of a website (rendered HTML + assets) with Playwright")
    parser.add_argument("urls", nargs="+", help="the pages to copy (all from one site)")
    parser.add_argument("--project-dir", required=True, help="the project: the copies go into its scraped/ folder")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # progress shows as it happens, also when the run is stopped
    first = {}
    for u in args.urls:
        first.setdefault(canonical(with_scheme(u)), with_scheme(u))   # / and /index.html are one page
    urls = list(first.values())
    if any(not is_same_domain(u, urls[0]) for u in urls):
        sys.exit("Error: all pages must be from one site.")
    scrape(urls, Path(args.project_dir) / "scraped")


if __name__ == "__main__":
    main()

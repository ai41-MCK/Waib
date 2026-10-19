#!/usr/bin/env python3
"""Measure copied pages, the evidence for the agent's page reports:

  content/<slug>.md          the page's text, section by section
  content/<slug>.json        facts, look (colors, fonts, logo), nav, contact, and
                             the verdicts of every measured check of the checklist
  content/<slug>.desktop.jpg / .phone.jpg   the copy rendered at 1280 and 390 px

    analyze.py --project-dir <out> [--pages slug1,slug2]
    analyze.py --project-dir <out> --verify slug1,slug2

A slug is a copied page's file name without `.html` (the homepage is `index`).
Without --pages, every page in sitemap.json is measured. --verify checks that
reports/<slug>.json is complete and prints what is missing.
"""
import argparse
import copy
import os
import re
import sys
from urllib.parse import urljoin, urlparse
from collections import Counter, defaultdict
from pathlib import Path

from bs4 import BeautifulSoup, Comment

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402
from common import (  # noqa: E402
    CONSENT, CONSENT_ANSWER, CONSENT_ANSWER_LINK, CONSENT_CONTROLS, CONSENT_LABEL_MAX, CSS_IMPORT_RE, LINK_DECOR, as_browser, contact, css_code, decorative,
    escape_md, extract_colors, extract_css_urls, extract_font_families, img_src, is_same_domain, normalize_hex, one_line, plural, private, read_dict,
    read_text, restore_media, sitemap_pages, truncate, write_json, write_text,
)

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
WRAPPERS = {"div", "table", "tbody", "tr", "td", "center", "form", "font", "section", "article", "main"}
TEXT_BLOCKS = ["p", "td", "th", "div", "blockquote", "center", "dd", "dt", "figcaption", "address"]
BLOCK_CHILDREN = ["p", "div", "td", "table", "ul", "ol", "blockquote", "section", "article"] + HEADINGS
LEGACY_COLORS = {"bgcolor": "background", "color": "text", "text": "text", "link": "link", "vlink": "link"}
PATHS = {"scraped": Path("."), "out": Path(".")}  # set by main()


def code(text):  # safe inside a `code span`
    return one_line(text).replace("`", "")


def asset(src):  # scraped `assets/x` -> path relative to the output dir (scraped/assets/x)
    return os.path.relpath(PATHS["scraped"] / src, PATHS["out"]) if src.startswith("assets/") else src


def link_label(a):
    img = a.find("img")
    text = one_line(a.get_text()) or (img and one_line(img.get("alt") or img.get("title"))) or one_line(a.get("title") or a.get("alt"))
    return (text or "").strip(LINK_DECOR)


# ---------------------------------------------------------------- content

def build_section(group):
    """One section: first heading + blocks (subheading/p/list/image/link) in document order."""
    sec, used, seen = {"heading": None, "blocks": []}, set(), set()
    paras = lambda text: [("p", t) for t in (one_line(x) for x in re.split(BR + r"\s*" + BR, text)) if t]  # noqa: E731  <br><br> ends a paragraph on old sites
    for top in group:
        for el in [top] + top.find_all(HEADINGS + ["ul", "ol", "img", "a"] + TEXT_BLOCKS):
            if el.name == "img":
                if img_src(el) and not img_src(el).startswith("data:") and not decorative(el):
                    sec["blocks"].append(("image", (asset(img_src(el)), one_line(el.get("alt") or el.get("title")))))
            elif any(id(p) in used for p in el.parents):
                continue  # already covered by an emitted paragraph/list/heading
            elif el.name == "a":
                href, text = el.get("href", "").strip(), link_label(el)
                if href and text and href not in seen and not href.startswith(("#", "javascript:")):
                    seen.add(href)
                    sec["blocks"].append(("link", (text, href)))
            elif el.name in ("ul", "ol"):
                used.add(id(el))
                items = [t for t in (one_line(li.get_text(" ")) for li in el.find_all("li", recursive=False)) if t]   # "One Sub", not "OneSub"
                sec["blocks"] += [("list", items)] if items else []
            elif el.name in HEADINGS:
                used.add(id(el))
                if one_line(el.get_text()) and sec["heading"]:
                    sec["blocks"].append(("subheading", one_line(el.get_text())))
                sec["heading"] = sec["heading"] or one_line(el.get_text())
            elif el.name == "p" or not el.find(BLOCK_CHILDREN):  # paragraph or leaf text container
                text = rest = one_line(el.get_text())
                for a in el.find_all("a"):
                    rest = rest.replace(one_line(a.get_text()), "")
                if re.search(r"\w", rest):  # link-only blocks are left to the link branch
                    used.add(id(el))
                    sec["blocks"] += paras(el.get_text())
            elif re.search(r"\w", own_text(el)):  # text sitting next to block children (legacy <td>)
                sec["blocks"] += paras(own_text(el, keep_breaks=True))
    return sec


def own_text(el, keep_breaks=False):
    """A container's loose text: its direct strings and inline children, not its blocks."""
    parts = [c.get_text(" ") if c.name else str(c) for c in el.children if not isinstance(c, Comment)
             and (not c.name or (c.name not in BLOCK_CHILDREN + ["ul", "ol", "script", "style"]
                                 and not c.find(BLOCK_CHILDREN)))]
    return " ".join(parts) if keep_breaks else one_line(" ".join(parts))


def _kids(el):
    return [k for k in el.find_all(True, recursive=False) if k.name not in ("script", "style", "noscript")
            and (one_line(k.get_text()) or k.name == "img" or k.find("img"))]


def extract_sections(soup):
    """Sections in document order: each outermost <section>, <article> or <main> (without inner ones), and the
    content around them. Without such tags: a new section at each wrapper (div, table row …) or heading."""
    body = soup.body or soup
    areas = [a for a in body.find_all(["section", "article", "main"])
             if not (a.name == "main" and a.find(["section", "article"]))]
    ids = {id(a) for a in areas}
    outer = {id(a) for a in areas if not any(id(p) in ids for p in a.parents)}
    groups, loose = [], []

    def walk(node):
        nonlocal loose
        for k in _kids(node):
            if id(k) in outer:
                groups.append([k])
                loose = []
            elif k.find(lambda t: id(t) in outer):
                loose = []  # a container starts a fresh group
                walk(k)
            else:
                if not loose or k.name in HEADINGS:
                    loose = []
                    groups.append(loose)
                loose.append(k)

    if outer:
        walk(body)
    else:
        root = body.select_one("[role=main]") or body
        kids = _kids(root)
        while len(kids) == 1 and kids[0].name in WRAPPERS and not re.search(r"\w", own_text(kids[0])):   # a wrapper with text of its own is content
            kids = _kids(kids[0])
        for k in kids:
            heading_only = groups and len(groups[-1]) == 1 and groups[-1][0].name in HEADINGS   # a heading keeps the wrapper after it
            if not groups or k.name in HEADINGS or k.name in WRAPPERS and not heading_only:
                groups.append([])
            groups[-1].append(k)
        groups = groups or [[root]]
    return [s for s in map(build_section, groups) if s["heading"] or s["blocks"]]


def collect_links(scopes):
    out, seen = [], set()
    for a in (a for scope in scopes for a in scope.find_all(["a", "area"], href=True)):   # image maps are menus on old sites
        href, text = a["href"].strip(), link_label(a)
        if text and href not in seen and not href.startswith(("#", "javascript:")):
            seen.add(href)
            out.append((truncate(text, 60), href))
    return out[:30]


def small(soup, els):
    """Drop class/id guesses that are really page-wide wrappers (<body class="has-nav">)."""
    total = len(soup.get_text()) or 1
    return [el for el in els if el.name != "body" and len(el.get_text()) < 0.5 * total]


def nav_elements(soup):
    """<nav>, else small elements with a nav/menu class or id."""
    links = lambda el: len("".join(a.get_text() for a in el.find_all("a"))) >= 0.5 * len(el.get_text())   # noqa: E731  mostly links: a menu, not a food menu
    return soup.find_all("nav") or [el for el in small(soup, soup.select("[class*=nav], [id*=nav], [class*=menu], [id*=menu]")) if links(el)] \
        or [m for m in soup.find_all("map") if len(m.find_all("area")) >= 3]   # an image map menu


def extract_nav(soup, sitemap):
    """(items, source): the nav elements, else a guess from the page's internal links."""
    items = collect_links(nav_elements(soup))
    if items:
        return items, "page navigation"
    home = sitemap.get("home", "")
    guess = [(t, h) for t, h in collect_links([soup])
             if not h.lower().startswith(("mailto:", "tel:")) and (not home or is_same_domain(h, home))]
    note = "guessed: no nav element, these are the page's internal links"
    return (guess, note) if 2 <= len(guess) <= 15 else ([], "")


def contact_info(soup):
    c = contact(soup)
    emails, phones = set(c["mailto"] + c["emails"]), set(c["tel"] + c["phones"])
    address = soup.find("address")
    return {"Email": ", ".join(sorted(emails)), "Phone": ", ".join(sorted(phones)),
            "Address": one_line(address.get_text(" ")) if address else ""}


HOME_PATH = r"/?(index\.\w+)?"   # the home page's path: "/", "/index.html" (copies have absolute links)


def find_logo(soup, st, url=""):
    def key(img):
        return " ".join(str(v) for v in (img.get("src"), img.get("id"), img.get("class"),   # not alt: "a photo of the Apple logo"
                                         img.parent.get("id"), img.parent.get("class"))).lower()
    def top(img):                       # where logos sit: the header or nav, or a link to the home page
        link = img.find_parent("a")
        return bool(img.find_parent(["header", "nav"])) or bool(link and home(link))
    def home(a):                        # a link to this site's home page (not to a sponsor's)
        href = a.get("href", "").strip()
        return bool(re.fullmatch(HOME_PATH, urlparse(urljoin(url or "http://x/", href)).path)) and (not url or is_same_domain(href, url))
    def elsewhere(img):                 # footers, payment and partner badges ("Verified by Visa")
        link = img.find_parent("a")
        if link and (re.match(r"https?://", link.get("href", "")) and url and not is_same_domain(link["href"], url)
                     or re.search(r"sponsor|partner|werbung|advert", " ".join([link.get("title", ""), link.get_text(" ")]), re.I)):
            return True                 # a link to another site, or a sponsor's
        return bool(img.find_parent("footer")) or any(re.search(r"payment|partner|sponsor|badge|footer", " ".join(p.get("class", [])) + " " + p.get("id", ""), re.I)
                                                    for p in [img] + list(img.parents)[:4] if hasattr(p, "get"))
    named = [i for i in soup.find_all("img") if re.search(r"logo|brand", key(i)) and not elsewhere(i)]
    logo = next((i for i in named if top(i)), None) or next((i for i in named if re.search(r"logo|brand", key(i).replace(i.get("src", "").lower(), ""))), None)
    home_links = [a for a in soup.find_all("a", href=True) if home(a)]
    logo = logo or next((a.find("img") for a in home_links if a.find("img") and not elsewhere(a.find("img"))), None)   # an image linking home
    if logo:
        return asset(img_src(logo)), one_line(logo.get("alt"))
    for selector, bg in st["logo_bg"]:  # a CSS background on a #logo/.logo element of this page, copied
        try:
            on_page = soup.select_one(re.sub(r"::?[\w-]+(\([^)]*\))?", "", selector))
        except Exception:               # a selector soupsieve can't read
            continue
        if on_page and "/" not in bg and (PATHS["scraped"] / "assets" / bg).is_file():
            return asset(f"assets/{bg}"), "CSS background"
    return None


# ---------------------------------------------------------------- style

def add_color(st, hexval, role):
    if hexval:
        st["colors"][hexval] += 1
        st["roles"][hexval][role] += 1


def scan_decls(st, selector, decls):
    is_link = re.search(r"(^|[\s,>+~])a([\s:.,\[#]|$)", selector.strip().lower())
    for decl in decls.split(";"):
        prop, _, val = decl.partition(":")
        prop, val = prop.strip(), re.sub(r"!\s*important", "", val).strip()
        if prop.startswith("--"):           # a custom property: its colours and font count where var() uses them
            st["vars"].setdefault(prop, extract_colors(val))
            st["font_vars"].setdefault(prop, extract_font_families(val, chosen=True))
            continue
        prop = prop.lower()
        if prop == "color" or prop.startswith(("background", "border", "outline")):
            role = (("link" if is_link else "text") if prop == "color" else prop.split("-")[0]).replace("outline", "border")
            for c in extract_colors(val):
                add_color(st, c, role)
            for name in re.findall(r"var\(\s*(--[\w-]+)", val):
                st["var_uses"][(name, role)] += 1
        if prop.startswith("background") and "logo" in selector.lower():
            st["logo_bg"] += [(selector, u) for u in extract_css_urls(val)]
        if prop in ("font-family", "font"):
            # the font the site chose (the first), not its fallbacks; code blocks are not the look
            if not re.search(r"(^|[\s,>+~])(code|pre|kbd|samp|tt)\b", selector.lower()):
                st["families"].update(extract_font_families(val, chosen=True, shorthand=prop == "font"))
                st["font_var_uses"].update(re.findall(r"^\s*var\(\s*(--[\w-]+)", val))   # WordPress: font-family: var(--wp--preset--…)
        elif prop in ("font-size", "font-weight") and val and not re.match(r"(var|calc|clamp|min|max)\(", val.lower()):   # sizes one can read
            st[prop][{"normal": "400", "bold": "700"}.get(val.lower(), val.lower())] += 1


def drop_print(css):
    """Without its @media print blocks: the print style is not the look."""
    out, i = [], 0
    for m in re.finditer(r"@media\s+print\s*\{", css):   # print only (not "print, screen")
        if m.start() < i:
            continue
        out.append(css[i:m.start()])
        depth, j = 1, m.end()
        while j < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    return "".join(out) + css[i:]


def scan_css(st, css):
    """Innermost `selector { decls }` blocks, so rules nested in @media/@supports count too."""
    css = re.sub(r"<!--|-->", " ", css_code(css))
    css = drop_print(css)
    for block in css.split("}"):  # linear, so a truncated file can't stall the run
        head, brace, decls = block.rpartition("{")
        selector = re.split(r"[;{}]", head)[-1].strip()   # not "@charset …; body"
        if brace and not selector.startswith("@"):
            st["rules"] += 1
            scan_decls(st, selector, decls)


def add_sheet(st, path, depth=0):
    """Scan a stylesheet a page links (also e.g. style.php, kept without .css), once,
    and the sheets it @imports (copied next to it)."""
    if re.match(r"[a-z][\w+.-]*:", str(path).replace(str(PATHS["scraped"]), "").lstrip("/\\"), re.I):
        return                          # still on the live site (https://…): not copied
    try:
        path = Path(path).resolve()
        if PATHS["scraped"].resolve() not in path.parents or not path.is_file():   # never read outside scraped/ (a copy can name any path)
            return
    except (OSError, ValueError):       # a name too long for the disk, and the like
        return
    if path not in st["sheets"] and depth <= 5:
        st["sheets"][path] = read_text(path)
        scan_css(st, st["sheets"][path])
        for ref in CSS_IMPORT_RE.findall(css_code(st["sheets"][path])):
            add_sheet(st, path.parent / ref.split("?")[0], depth + 1)


def resolve_vars(st):
    """Count the colours and fonts of custom properties as often as var() uses them."""
    for (name, role), n in st["var_uses"].items():
        for c in st["vars"].get(name, []):
            for _ in range(n):
                add_color(st, c, role)
    for name, n in st["font_var_uses"].items():
        for f in st["font_vars"].get(name, []):
            st["families"][f] += n


def new_stats():
    return {"colors": Counter(), "roles": defaultdict(Counter), "families": Counter(), "font-size": Counter(),
            "font-weight": Counter(), "rules": 0, "sheets": {}, "logo_bg": [],
            "vars": {}, "var_uses": Counter(), "font_vars": {}, "font_var_uses": Counter()}


def scan_html_styles(st, soup):
    for tag in soup.find_all("style"):
        scan_css(st, tag.get_text())
        for ref in CSS_IMPORT_RE.findall(css_code(tag.get_text())):   # copied sheets
            add_sheet(st, PATHS["scraped"] / ref.split("?")[0])
    for el in soup.find_all(True):
        if el.get("style"):
            scan_decls(st, el.name, el["style"])
        for attr, role in LEGACY_COLORS.items():
            if el.get(attr) and (attr in ("bgcolor", "color") or el.name == "body"):
                value = el[attr].strip()
                add_color(st, normalize_hex(("#" if re.fullmatch(r"[0-9a-fA-F]{6}", value) else "") + value), role)
        if el.name == "font" and el.get("face"):
            st["families"].update(extract_font_families(el["face"], chosen=True))


def tone(hexval):  # (HSL lightness, saturation), both 0..1
    rgb = [int(hexval[i:i + 2], 16) for i in (1, 3, 5)]
    hi, lo = max(rgb), min(rgb)
    return (hi + lo) / 510, (hi - lo) / (255 - abs(hi + lo - 255) or 1)


def is_neutral(hexval):  # greys, near-white, near-black
    light, sat = tone(hexval)
    return sat < 0.1 or light > 0.94 or light < 0.08


def role_guess(hexval, roles):
    use, (light, sat) = roles.most_common(1)[0][0], tone(hexval)
    return {"background": "light surface / section background" if light > 0.7 else "brand band / button fill",
            "link": "link / accent", "border": "border / divider",
            "text": "accent text / headings / highlights" if sat > 0.4 else "body / secondary text"}[use]


def modernization(soup, css):
    """[(points, check, passed, detail)]: the layout signals double as the score."""
    css, html = css.lower(), str(soup).lower()
    has = lambda pattern: bool(re.search(pattern, css))  # noqa: E731
    yn = lambda flag: "yes" if flag else "no"  # noqa: E731
    flex, grid = has(r"display\s*:\s*(inline-)?flex"), has(r"display\s*:\s*(inline-)?grid")
    floats = has(r"float\s*:")
    media = len(re.findall(r"@media(?!\s+print\b)", css))   # print styles don't adapt to screens
    tables = sum(1 for t in soup.find_all("table") if t.find(["table", "img"]) and not t.find("th"))
    legacy = len(soup.find_all(["font", "center", "marquee"])) + len(soup.find_all(attrs={"bgcolor": True}))
    semantic = sorted({t.name for t in soup.find_all(["header", "nav", "main", "section", "article", "footer"])})
    return [(20, "Media queries", bool(media), f"{media} found" if media else "absent"),   # the viewport is its own check
            (20, "Flexbox or grid layout", flex or grid, f"flex {yn(flex)}, grid {yn(grid)}, floats {yn(floats)}"),
            (15, "No tables for layout", not tables, f"{tables} layout tables"),
            (10, "Semantic HTML5 elements", bool(semantic), ", ".join(semantic) or "none"),
            (15, "No legacy markup (<font>, <center>, bgcolor)", not legacy, f"{legacy} found"),
            (10, "CSS custom properties", has(r"var\(--|--[\w-]+\s*:"), ""),
            (5, "Web fonts", "@font-face" in css or bool(re.search(r"fonts\.googleapis|use\.typekit", html)), ""),
            (5, "Modern CSS effects", has(r"transition|animation|border-radius|box-shadow|transform"), "")]


# ---------------------------------------------------------------- pages + output

def meta(soup, *names):
    tags = soup.find_all("meta", content=True)
    return next((one_line(m["content"]) for n in names for m in tags
                 if (m.get("name") or m.get("property") or "").lower() == n and one_line(m["content"])), "")


def drop_banners(soup, hidden=None):
    """The page without cookie banners and <noscript> (the app shows neither): no H1s, colours or text from them.
    hidden: the selectors the render hid (measure.IS_BANNER); without a render, the same rule as far as the HTML tells."""
    if hidden is not None:
        drop = []
        for sel in hidden:
            try:
                drop.append(soup.select_one(sel))
            except Exception:           # a selector soupsieve reads differently: that banner's text stays
                pass
    else:
        total = len(soup.get_text()) or 1
        found = [el for el in soup.select(CONSENT) if el.name not in ("a", "label", "input", "button", "select", "textarea")]   # not "Cookie-Einstellungen"

        def answers(b):                 # as IS_BANNER: a short answer label, not a link to another page
            text, href = (b.get("value") or b.get_text()).strip(), b.get("href") if b.name == "a" else None
            return len(text) <= CONSENT_LABEL_MAX and re.search(CONSENT_ANSWER, text, re.I) and not (href is not None and not re.match(CONSENT_ANSWER_LINK, href, re.I | re.S))
        drop = [el for el in found if el.get("role") in ("dialog", "alertdialog") or el.get("aria-modal") == "true"
                or re.search(r"position\s*:\s*(fixed|sticky)", el.get("style", ""), re.I)]
        drop += small(soup, [el for el in found if any(answers(b) for b in el.select(CONSENT_CONTROLS))   # not a form's consent field (a page-wide <form> is no form)
                             and not any(len(w.get_text()) < total / 2 for w in el.find_parents(["form", "footer"]))])
    for el in [e for e in drop if e] + soup.find_all("noscript"):   # a consent dialog may hold most of the page's text (vendor lists)
        if not el.decomposed:
            el.decompose()
    return soup


def wrap_loose_text(soup):
    """Text sitting between blocks (legacy <td>: heading, text, <p>, text) becomes paragraphs in
    its place, so the page text keeps the page's order."""
    blocks = BLOCK_CHILDREN + ["ul", "ol", "script", "style"]
    for el in soup.find_all(True):
        kids = [k for k in el.children if not isinstance(k, Comment)]
        if not any(getattr(k, "name", None) in blocks for k in kids):
            continue
        run = []
        for k in kids + [None]:
            if k is not None and not (k.name and (k.name in blocks or k.find(BLOCK_CHILDREN))):
                run.append(k)
                continue
            if run and re.search(r"\w", "".join(x.get_text() if x.name else str(x) for x in run)):
                p = soup.new_tag("p")
                run[0].insert_before(p)
                for x in run:
                    p.append(x.extract())
            run = []


BR = "\u2028"   # a <br> in the page text: one is a space, two in a row end a paragraph


def process_page(soup, slug, sitemap, st, url=""):
    """The page's text and facts; takes the soup apart, so pass a copy."""
    for br in soup.find_all("br"):
        br.replace_with(BR)
    wrap_loose_text(soup)
    footer = next((f for f in soup.find_all("footer") if not f.find_parent(["article", "section"])), None) \
        or next(iter(small(soup, soup.select("[id*=footer], [class*=footer]"))), None)
    for link in (l for l in soup.find_all("link", href=True) if "stylesheet" in [r.lower() for r in l.get("rel", [])]):   # REL="StyleSheet" too
        add_sheet(st, PATHS["scraped"] / link["href"].split("?")[0])
    page = {"slug": slug, "title": (one_line(soup.title.get_text()) if soup.title else "") or slug,
            "description": meta(soup, "description", "og:description"), "site_name": meta(soup, "og:site_name"),
            "nav": extract_nav(soup, sitemap), "footer": one_line(footer.get_text(" ")) if footer else "",
            "contact": contact_info(soup), "logo": find_logo(soup, st, url)}
    scan_html_styles(st, soup)
    for el in soup.find_all(["script", "style", "noscript", "template"]):
        el.decompose()
    for el in nav_elements(soup) + ([footer] if footer else []):
        if not el.decomposed:
            el.decompose()  # nav/footer go into the facts, not the page text
    page["sections"] = extract_sections(soup)
    return page


def render_section(sec, n):
    out = [f"## {escape_md(sec['heading'] or '') or f'Section {n}'}", ""]
    for kind, data in sec["blocks"]:
        if kind not in ("image", "link") and out[-1] != "":
            out.append("")                 # after "- Image:" / "- Link:" lines: a paragraph is not part of that list
        if kind == "subheading":
            out += [f"### {escape_md(data)}", ""]
        elif kind == "p":
            out += [escape_md(data), ""]
        elif kind == "list":
            out += [f"- {escape_md(i)}" for i in data] + [""]
        else:
            out.append(f"- Image: `{code(data[0])}` — alt: \"{escape_md(data[1])}\"" if kind == "image"
                       else f"- Link: {escape_md(data[0])} → `{code(data[1])}`")
    return out + [""]


def write_md(path, lines):
    write_text(path, re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n")


def facts(page, st, soup, css, url):
    """The measured side of a page's report: what it is, how modern, and its look."""
    checks = modernization(soup, css)
    resolve_vars(st)
    hrefs = [a.get("href", "") for a in soup.find_all(["a", "area"], href=True)]
    ranked = st["colors"].most_common()
    color = lambda h, n: {"hex": h, "count": n, "role": role_guess(h, st["roles"][h])}  # noqa: E731
    return {
        "url": url, "title": page["title"], "description": page["description"],
        "modernization": sum(points for points, _, ok, _ in checks if ok),
        "modernization_checks": [{"check": name, "points": points, "passed": ok, "detail": detail}
                                 for points, name, ok, detail in checks],
        **measure.readability(page["sections"]),
        "words": len(re.findall(r"\w+", soup.get_text(" "))),
        "headings": len(soup.find_all(HEADINGS)),
        "images": len([i for i in soup.find_all("img") if img_src(i) and not decorative(i)]),
        "links": len(hrefs),
        "downloads": len([h for h in hrefs if re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip)(\?|$)", h, re.I)]),
        "colors": [color(h, n) for h, n in ranked if not is_neutral(h)][:6],
        "neutrals": [color(h, n) for h, n in ranked if is_neutral(h)][:4],
        "fonts": [f for f, _ in st["families"].most_common(4)],
        "sizes": [f for f, _ in st["font-size"].most_common(6)],
        "logo": {"src": page["logo"][0], "alt": page["logo"][1]} if page["logo"] else None,
        "nav": [{"text": t, "href": h} for t, h in page["nav"][0]],
        "footer": truncate(page["footer"], 400),
        "contact": {k.lower(): v for k, v in page["contact"].items() if v},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0].rstrip(":"))
    ap.add_argument("--project-dir", required=True, help="the project: reads scraped/, writes content/<slug>.md and .json")
    ap.add_argument("--pages", help="comma-separated slugs (default: every page in sitemap.json)")
    ap.add_argument("--verify", help="comma-separated slugs: check that their reports are complete")
    args = ap.parse_args()
    out = Path(args.project_dir)
    scraped = out / "scraped"
    if args.verify:
        bad = 0
        for slug in [re.sub(r"\.html$", "", s.strip()) for s in args.verify.split(",") if s.strip()]:
            problems = ["not a page report: --verify checks reports/<slug>.json of a copied page"] if slug.startswith("_") \
                else measure.verify(out, slug)
            bad += bool(problems)
            print(f"reports/{slug}.json: " + ("complete" if not problems else plural(len(problems), "problem")))
            for line in problems:
                print("  - " + line)
        sys.exit(1 if bad else 0)
    PATHS.update(scraped=scraped, out=out)
    sitemap = read_dict(scraped / "sitemap.json")
    entries = {Path(p["filename"]).stem: p for p in sitemap_pages(sitemap)}   # a shared copy: its own address wins
    on_disk = {p.stem.lower(): p.stem for p in scraped.glob("*.html")}
    wanted = [slug.lower() for slug in entries] or list(on_disk)
    if args.pages:
        wanted = [re.sub(r"\.html$", "", s.strip().strip("/").lower()) for s in args.pages.split(",") if s.strip()]
        missing = [w for w in wanted if w not in on_disk]
        if missing:
            print(f"WARNING: no copied page for: {', '.join(missing)}", file=sys.stderr)
    slugs = list(dict.fromkeys(on_disk[w] for w in wanted if w in on_disk))
    if not slugs:
        sys.exit(f"ERROR: no pages to analyse in {scraped}")
    (out / "content").mkdir(parents=True, exist_ok=True)
    for slug in slugs:                   # no old screenshot passes for this run's
        for kind in ("desktop", "phone"):
            (out / "content" / f"{slug}.{kind}.jpg").unlink(missing_ok=True)
    try:
        home = sitemap.get("home") or next((e.get("url") for e in entries.values() if e.get("url")), "")
        rendered = measure.render({slug: scraped / f"{slug}.html" for slug in slugs}, out / "content", local_ok=bool(home) and private(home, unsure=False))
    except Exception as e:  # no browser: the HTML facts still help
        print(f"WARNING: could not render the pages ({e}); measured only from the HTML", file=sys.stderr)
        rendered = {}
    for slug, kinds in rendered.items():
        for kind, r in kinds.items():
            if "error" in r:
                print(f"WARNING: {slug} could not be rendered at {kind} width ({r['error']}): no screenshot, its measured checks are missing", file=sys.stderr)
    titles = [one_line(p.get("title", "")) for p in entries.values()]   # as measure compares them (&nbsp; too)
    for slug in slugs:
        st = new_stats()
        kinds = [k for k in rendered.get(slug, {}).values() if "error" not in k]   # the banners the render hid, else the HTML rule
        hidden = sorted({sel for k in kinds for sel in k.get("banners", [])}) if kinds else None
        soup = drop_banners(as_browser(BeautifulSoup(restore_media(read_text(scraped / f"{slug}.html")), "lxml")), hidden)   # left-out media as the page has them
        css = [t.get_text() for t in soup.find_all("style")]
        page = process_page(copy.copy(soup), slug, sitemap, st, entries.get(slug, {}).get("url", ""))
        css += list(st["sheets"].values())
        lines = [f"# {escape_md(page['title'])}", "", f"Description: {escape_md(page['description']) or '(none)'}", ""]
        write_md(out / "content" / f"{slug}.md",
                 lines + [ln for n, sec in enumerate(page["sections"], 1) for ln in render_section(sec, n)])
        f = facts(page, st, soup, "\n".join(css), entries.get(slug, {}).get("url", ""))
        r = rendered.get(slug, {})
        f["network"] = entries.get(slug, {}).get("network")
        f["copied"] = entries.get(slug, {}).get("copied")
        f["screens"] = {k: f"content/{slug}.{k}.jpg" for k in ("desktop", "phone") if (out / "content" / f"{slug}.{k}.jpg").exists()}
        f["rendered"] = r
        f["checks"] = measure.checks(soup, f, r, f["network"], titles)
        write_json(out / "content" / f"{slug}.json", f)
    print(f"Measured {plural(len(slugs), 'page')} into {out / 'content'}/: {', '.join(slugs)}")


if __name__ == "__main__":
    main()

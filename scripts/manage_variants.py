#!/usr/bin/env python3
"""
The designs of a Waib project, and its export.

  DS = a look (tokens.css + styleguide.html)        ds1, ds2 … / a copy to compare: ds1.1
  V  = a layout in that look, built page by page    v1, v2 …   / a copy to compare: v1.1

Layout:   variants/<ds>/{tokens.css, styleguide.html, <v>/{<slug>.html…, styles.css, main.js}}
Original: original/<slug>.html, edited copies of the scraped pages (the "Original" design)
Media:    what a copy left out (<waib-media> placeholders), downloaded on request into scraped/assets/
State:    variants.json {"design_systems": […], "chosen": {"ds", "variant"}} — the ★ design
          the website uses ({"ds": "original"} for the edited original).

A page's slug is its scraped file name without .html (scraped/sitemap.json maps
URL -> file). Library for app.py, and a CLI for the agent.
"""

import argparse
import functools
import re
import shutil
import sys
import time
from html import escape, unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402
import site_index  # noqa: E402
from common import (  # noqa: E402
    ATTR_RE, HOST, MEDIA_MAX, TAG_RE, UA, ascii_fold, asset_filename, base_host, canonical, clean_label, fresh, in_markup, markup_findall, media_in,
    megabytes, now, plural, private, project_lock, read_dict, read_json, read_text, restore_media, same_page, sitemap_pages, strip_scripts, trusted,
    user_hosts_file, write_json, write_text,
)

ORIGINAL = "original"


# --- State ---------------------------------------------------------------------

def load(project):
    data = read_dict(Path(project) / "variants.json")
    data.setdefault("design_systems", [])
    data.setdefault("chosen", None)
    for ds in data["design_systems"]:
        ds.setdefault("variants", [])
    return data


def save(project, data):
    """Persist variants.json, entries in version order so a copy (ds1.1, v1.1)
    sits next to its parent."""
    version = lambda x: [int(n) for n in re.findall(r"\d+", x["id"])]  # noqa: E731
    data["design_systems"].sort(key=version)
    for ds in data["design_systems"]:
        ds["variants"].sort(key=version)
    write_json(Path(project) / "variants.json", data)


def html_pages(folder):
    return sorted(f.stem for f in Path(folder).glob("*.html") if f.stem != "styleguide") if Path(folder).is_dir() else []


def sitemap_of(project, sitemap=None):
    """scraped/sitemap.json (or the one already read), {} when there is none or it's damaged."""
    return sitemap if isinstance(sitemap, dict) else read_dict(Path(project) / "scraped" / "sitemap.json")


def copied(project, sitemap=None):
    """URL -> slug of every copied page (a shared copy's own address last, so inverting the map keeps it)."""
    return {canonical(p["url"]): Path(p["filename"]).stem for p in sitemap_pages(sitemap_of(project, sitemap))}


def hosts(project, sitemap=None):
    """The other sites a public site's copies load from: allowed (well known, the site itself, or allowed
    by the user), and the ones to ask about. The app lets a copy load only from the allowed."""
    sitemap = sitemap_of(project, sitemap)
    found = sitemap.get("origins") if not sitemap.get("local") else None
    if not isinstance(found, list):
        return {"allowed": [], "ask": []}   # a local site (or no list of sites): not limited
    allow, block = user_answers()
    site = base_host(urlparse(str(sitemap.get("home") or "")).netloc)
    found = [h for h in found if isinstance(h, str)]
    own = lambda h: bool(site) and (base_host(h) == site or base_host(h).endswith("." + site))  # noqa: E731  (cdn.site.de)
    ok = [h for h in found if trusted(h) or own(h) or h in allow]
    return {"allowed": ok, "ask": [h for h in found if h not in ok and h not in block]}


def user_answers():
    """(allowed, blocked) sites from the user's own list, kept for every project (common.user_hosts_file)."""
    data = read_dict(user_hosts_file())
    return tuple({h for h in data.get(k) or [] if isinstance(h, str)} for k in ("allow", "block"))


def answer_hosts(project, allow=(), block=()):
    """The user's answer for sites a copy loads from, kept for every project (host names only)."""
    allow, block = ({h.lower() for h in hs if isinstance(h, str) and re.fullmatch(HOST, h, re.I)} for hs in (allow, block))
    a, b = user_answers()
    if not (allow - a or block - b or allow & b or block & a):   # nothing new: the user's file stays as it is
        return hosts(project)
    write_json(user_hosts_file(), {"allow": sorted((a | allow) - block), "block": sorted((b | block) - allow)})
    return hosts(project)


def retry(project, urls):
    """Pages asked for again: no longer "couldn't be copied" (the next copy says how it went)."""
    with project_lock(project):
        sitemap = sitemap_of(project)
        failed = sitemap.get("failed")
        if isinstance(failed, dict) and any(canonical(u) in urls for u in failed):
            sitemap["failed"] = {u: why for u, why in failed.items() if canonical(u) not in urls}
            write_json(Path(project) / "scraped" / "sitemap.json", sitemap)


def state(project):
    """What the app shows: designs with the pages built in each, the copied
    pages with their scores and reports, and the project's own settings."""
    project = Path(project)
    data = load(project)
    ds_ids = [ds["id"] for ds in data["design_systems"]]
    data["next_ds"] = next_id("ds", ds_ids)   # the ids the app shows before they exist (one source: next_id)
    for ds in data["design_systems"]:
        v_ids = [v["id"] for v in ds["variants"]]
        ds["copy"], ds["next"] = next_id("ds", ds_ids, ds["id"]), next_id("v", v_ids)
        ds["styleguide"] = (project / ds["dir"] / "styleguide.html").is_file()
        for v in ds["variants"]:
            v["pages"], v["copy"] = html_pages(project / v["dir"]), next_id("v", v_ids, v["id"])
    sitemap = sitemap_of(project)
    pages, by_url = {}, copied(project, sitemap)
    left = media(project, list(by_url.values()))
    for slug in by_url.values():
        facts = read_dict(project / "content" / f"{slug}.json")
        report = read_json(project / "reports" / f"{slug}.json")
        judged = report.get("checks", {}) if isinstance(report, dict) and isinstance(report.get("checks"), dict) else {}
        wanted = {m["src"]: m["bytes"] for m in left.get(slug, []) if m["kind"] != "audio"}   # what can be downloaded (once each)
        pages[slug] = {"score": measure.score(facts.get("checks") or {}, judged), "report": isinstance(report, dict),
                       "partial": not judged, "media": {"count": len(wanted), "bytes": sum(wanted.values())}}
    overall = read_json(project / "reports" / "_overall.json")
    failed = sitemap.get("failed")
    failed = {canonical(u): str(why) for u, why in failed.items() if canonical(u) not in by_url} if isinstance(failed, dict) else {}
    titles = {canonical(p["url"]): str(p.get("title") or "") for p in sitemap_pages(sitemap)}
    return {**data, "hosts": hosts(project, sitemap), "original": html_pages(project / ORIGINAL), "copied": by_url, "failed": failed, "titles": titles,
            "pages": pages, "overall": {"recommend": overall.get("recommend")} if isinstance(overall, dict) else None,
            "ideas": read_dict(project / "ideas.json"), "settings": settings(project)}


def settings(project):
    """project.json: the user's page, notes, the pages to redesign and those sent for analysis."""
    return read_dict(Path(project) / "project.json")


def locked(fn):
    """fn under the project lock: the app and Claude's commands change a project one at a time."""
    @functools.wraps(fn)
    def run(project, *args, **kwargs):
        with project_lock(project):
            return fn(project, *args, **kwargs)
    return run


def change_settings(project, change):
    """project.json changed under the project lock. change: {key: value} (None removes the key), or a function
    that changes the settings in place. Returns the new settings."""
    with project_lock(project):
        opts = settings(project)
        change(opts) if callable(change) else opts.update(change)
        opts = {k: v for k, v in opts.items() if v is not None}
        write_json(Path(project) / "project.json", opts)
    return opts


def merge(urls, add=(), remove=()):
    """An ordered list of canonical page addresses with some added and some removed."""
    for given in (urls, add, remove):
        if not isinstance(given, (list, tuple)) or not all(isinstance(u, str) and re.match(r"https?://", u) for u in given):
            raise ValueError("expected a list of page addresses")
    gone = {canonical(u) for u in remove}
    return [u for u in dict.fromkeys([canonical(u) for u in [*urls, *add]]) if u not in gone]


def put_on_list(project, urls):
    """Pages the user asked for in the chat go on their Redesign list, as if ticked in the app, and show as
    being analysed until their reports are in. The app says where they came from (from_chat)."""
    added = []

    def change(opts):
        have = opts.get("redesign", [])
        opts["redesign"] = merge(have, urls)
        opts["requested"] = merge(opts.get("requested", []), urls)
        added[:] = [u for u in opts["redesign"] if u not in have]
        if added:
            opts["from_chat"] = {"at": now(), "count": len(added)}
    opts = change_settings(project, change)
    return f"On the Redesign list: {plural(len(added), 'page')} added, {len(opts['redesign'])} in all."


SCRATCH = "https://example.com/"   # the page of a project started from scratch
PLACEHOLDER_PAGE = ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
                    '<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font:17px/1.5 system-ui,sans-serif;color:#334;background:#f5f6f8}}'
                    'main{{max-width:30em;padding:32px;text-align:center}}h1{{font-size:2em;margin:0 0 .4em}}</style></head>'
                    '<body><main><h1>{head}</h1><p>{text}</p></main></body></html>\n')


def scratch_page(project, slug, url, title, text, notes):
    """A page of a project started from scratch: a placeholder copy, listed, with an empty report
    whose only design input is the user's description."""
    project = Path(project)
    sitemap = sitemap_of(project) or {"home": SCRATCH, "origins": [], "pages": []}
    sitemap["pages"] = [p for p in sitemap.get("pages", []) if p.get("url") != url] + [{"url": url, "filename": f"{slug}.html", "title": title}]
    write_json(project / "scraped" / "sitemap.json", sitemap)
    write_text(project / "scraped" / f"{slug}.html", PLACEHOLDER_PAGE.format(title=escape(title), head=escape(title), text=text))
    write_json(project / "content" / f"{slug}.json", {"checks": {}})
    write_text(project / "content" / f"{slug}.md", "")
    write_json(project / "reports" / f"{slug}.json", {
        "summary": "There was no website, so there is no report: nothing to keep or fix. Claude builds your pages from your description, in Designs.",
        "audience": "", "tasks": [], "checks": {}, "inventory": [], "keep": [], "fix": [], "add": [],
        "design_input": [notes] if notes else []})


@locked
def from_scratch(project, notes, renamed_from=None):
    """A project without a website: a placeholder home page and an empty first design system,
    so designing starts at once. The user's description is the brief."""
    project = Path(project)
    scratch_page(project, "index", SCRATCH, "New website", "There was no website to copy, so this placeholder stays as it is. "
                 "Your pages are built in Designs: open Designs to see and change them.", notes)
    if not load(project)["design_systems"]:   # the first design system is there at once: Claude builds into ds1/v1
        add_ds(project, "Design 1")
        add_variant(project, "ds1", "Layout 1")
        write_text(project / "variants" / "ds1" / "styleguide.html", PLACEHOLDER_PAGE.format(
            title="Style guide", head="The look of your new website",
            text="Colours, fonts, buttons and the parts every page uses. Claude fills this in with the first design."))
    change_settings(project, {"url": SCRATCH, "code": None, "notes": notes, "scratch": True, "renamed_from": renamed_from,
                              "redesign": [SCRATCH], "requested": [], "started": now()})


@locked
def add_page(project, title):
    """Another page of a project started from scratch (as its description names them): listed for the redesign."""
    opts = settings(project)
    if not opts.get("scratch"):
        fail("add-page is only for projects started from scratch.")
    title = clean_label(title) or fail("a page needs a title.")
    pages = {p["url"]: p for p in sitemap_pages(sitemap_of(project))}
    base = re.sub(r"[^a-z0-9]+", "-", ascii_fold(title.lower())).strip("-")[:50].strip("-") or "page"
    slug, n = base, 1
    while slug in ("index", "styleguide") or f"{SCRATCH}{slug}/" in pages:   # Café and Cafe: two pages; "styleguide" is the style guide's
        if str(pages.get(f"{SCRATCH}{slug}/", {}).get("title", "")).lower() == title.lower():
            return f"{title} is there already: {SCRATCH}{slug}/ (slug {slug})"
        n += 1
        slug = f"{base}-{n}"
    url = f"{SCRATCH}{slug}/"
    scratch_page(project, slug, url, title, "There was no website to copy, so this placeholder stays as it is. This page is built in Designs when you ask for it.", opts.get("notes", ""))
    change_settings(project, lambda o: o.update(redesign=merge(o.get("redesign", []), [url])))
    return f"Added {title}: {url} (slug {slug})"


def find(items, item_id):
    return next((x for x in items if x["id"] == item_id), None)


def fail(msg):
    raise ValueError(msg)


def need_ds(data, ds_id):
    return find(data["design_systems"], ds_id) or fail(f"DS '{ds_id}' not found.")


def need_variant(ds, v_id):
    return find(ds["variants"], v_id) or fail(f"Variant '{v_id}' not found in {ds['id']}.")


def next_id(prefix, taken, base=None):
    """v1, v2 … or, with a base like 'v1', the next minor: v1.1, v1.2 …"""
    nums = [[int(n) for n in re.findall(r"\d+", t)] for t in taken]   # above the highest one there is (ds1 gone, ds1.1 left: ds2)
    if base:
        digits = re.search(r"\d+", base)
        major = int(digits.group()) if digits else 1
        return f"{prefix}{major}.{max([n[1] for n in nums if len(n) > 1 and n[0] == major] or [0]) + 1}"
    return f"{prefix}{max([n[0] for n in nums if n] or [0]) + 1}"


def copy_dir_files(src, dest):
    """Copy the files directly inside `src` (not sub-folders) into `dest`."""
    for f in Path(src).iterdir():
        if f.is_file():
            shutil.copy2(f, Path(dest) / f.name)


# --- Actions (used by the CLI below and by app.py; errors raise ValueError) ------

@locked
def add_ds(project, label, ds_id=None, source_id=None):
    """New design system. With a source, copy its tokens, style guide and every
    variant with all its pages, so a copy starts from the same pages."""
    project = Path(project)
    data = load(project)
    systems = data["design_systems"]
    source = need_ds(data, source_id) if source_id else None
    ds_id = ds_id or next_id("ds", {d["id"] for d in systems}, base=source_id)
    if not re.fullmatch(r"ds\d+(\.\d+)?", ds_id):
        fail(f"'{ds_id}' is not a DS id like ds2 or ds1.1.")
    if find(systems, ds_id):
        fail(f"DS '{ds_id}' already exists.")

    ds_dir = f"variants/{ds_id}"
    (project / ds_dir).mkdir(parents=True, exist_ok=True)
    entry = {"id": ds_id, "label": label, "dir": ds_dir, "variants": []}
    if source:
        copy_dir_files(project / source["dir"], project / ds_dir)
        for v in source["variants"]:
            v_dir = f"{ds_dir}/{v['id']}"
            shutil.copytree(project / v["dir"], project / v_dir, dirs_exist_ok=True)
            entry["variants"].append({"id": v["id"], "label": v["label"], "dir": v_dir})
    systems.append(entry)
    if not data["chosen"] and entry["variants"]:
        data["chosen"] = {"ds": ds_id, "variant": entry["variants"][0]["id"]}
    save(project, data)
    copied_note = f" (copied from {source['id']}: {', '.join(v['id'] for v in entry['variants']) or 'no variants'})" if source else ""
    return f"Added {ds_id} at {ds_dir}{copied_note}"


@locked
def add_variant(project, ds_id, label, v_id=None, source_id=None):
    """New variant under a DS. With a source, copy that variant's pages as the start."""
    project = Path(project)
    data = load(project)
    ds = need_ds(data, ds_id)
    source = need_variant(ds, source_id) if source_id else None
    v_id = v_id or next_id("v", {v["id"] for v in ds["variants"]}, base=source_id)
    if not re.fullmatch(r"v\d+(\.\d+)?", v_id):
        fail(f"'{v_id}' is not a variant id like v2 or v1.1.")
    if find(ds["variants"], v_id):
        fail(f"Variant '{v_id}' already exists in {ds['id']}.")

    v_dir = f"{ds['dir']}/{v_id}"
    if source:
        shutil.copytree(project / source["dir"], project / v_dir, dirs_exist_ok=True)
    (project / v_dir).mkdir(parents=True, exist_ok=True)
    ds["variants"].append({"id": v_id, "label": label, "dir": v_dir})
    if not data["chosen"]:
        data["chosen"] = {"ds": ds["id"], "variant": v_id}
    save(project, data)
    return f"Added {ds['id']}/{v_id} at {v_dir}" + (f" (copied from {source['id']})" if source else "")


@locked
def duplicate(project, ds_id, v_id=None):
    """Copy a variant (or a whole design system) to try a change next to it: the next free
    sub-number of the same main number (v1.1 -> v1.2, ds1 -> ds1.1). Returns the new id."""
    data = load(project)
    ds = need_ds(data, ds_id)
    if v_id:
        v = need_variant(ds, v_id)
        new = next_id("v", {x["id"] for x in ds["variants"]}, base=v_id)
        add_variant(project, ds_id, v["label"], new, v_id)
        return new
    new = next_id("ds", {d["id"] for d in data["design_systems"]}, base=ds_id)
    add_ds(project, ds["label"], new, ds_id)
    return new


@locked
def choose(project, ds_id, v_id=None):
    """The ★ design: the one the website shows and Export delivers."""
    data = load(project)
    if ds_id == ORIGINAL:
        data["chosen"] = {"ds": ORIGINAL}
    else:
        ds = need_ds(data, ds_id)
        v = need_variant(ds, v_id) if v_id else (ds["variants"] or fail(f"{ds_id} has no variant yet."))[0]
        data["chosen"] = {"ds": ds["id"], "variant": v["id"]}
    save(project, data)
    return "Chose " + "/".join(data["chosen"].values())


@locked
def rename(project, ds_id, v_id, label):
    label = clean_label(label) or fail("a name can't be empty.")
    data = load(project)
    ds = need_ds(data, ds_id)
    (need_variant(ds, v_id) if v_id else ds)["label"] = label
    save(project, data)
    return f"Renamed {v_id or ds_id} to {label}"


@locked
def discard(project, ds_id, v_id=None):
    """Permanently delete a DS or variant. If it was chosen, the first one left is (else the
    edited original, if there is one)."""
    project = Path(project)
    data = load(project)
    ds = need_ds(data, ds_id)
    if v_id:
        items, target = ds["variants"], need_variant(ds, v_id)
    else:
        items, target = data["design_systems"], ds
    items.remove(target)
    chosen = data["chosen"] or {}
    if chosen.get("ds") == ds_id and (not v_id or chosen.get("variant") == v_id):
        first = next(((d, v) for d in data["design_systems"] for v in d["variants"]), None)
        edited = any((project / ORIGINAL).glob("*.html"))
        data["chosen"] = {"ds": first[0]["id"], "variant": first[1]["id"]} if first else {"ds": ORIGINAL} if edited else None
    save(project, data)                 # first: a list that can't be saved deletes nothing
    shutil.rmtree(project / target["dir"], ignore_errors=True)
    return f"Discarded {ds['id']}" + (f"/{v_id}" if v_id else "")


@locked
def original(project, slug):
    """Start editing the original of a page: copy scraped/<slug>.html to
    original/<slug>.html with its asset paths fixed. Keeps an existing copy."""
    project, slug = Path(project), re.sub(r"\.html$", "", slug)   # "kontakt" or "kontakt.html"
    if not re.fullmatch(r"[\w-][\w.-]*", slug):
        fail(f"'{slug}' is not a page slug (the copied file name without .html).")
    src, dest = project / "scraped" / f"{slug}.html", project / ORIGINAL / f"{slug}.html"
    if not src.exists():
        fail(f"{src} not found: copy the page first (scrape_site.py).")
    if dest.exists():
        return f"{dest} already exists — edit it."
    write_text(dest, in_markup(read_text(src), lambda m: re.sub(r"(?<=[\"'(\s=,])assets/", "../scraped/assets/", m)))   # not in the page's text
    data = load(project)
    if not data["chosen"]:              # the first design gets the ★ by itself, the edited original too
        data["chosen"] = {"ds": ORIGINAL}
        save(project, data)
    return f"Created {dest} — edit it."


# --- Media the copies left out -----------------------------------------------------

def media(project, slugs=None):
    """{slug: [{kind, src, bytes, name}]}: the media each copy left out (its placeholders)."""
    project = Path(project)
    bad = [s for s in slugs or [] if not re.fullmatch(r"[\w-][\w.-]*", s)]
    if bad:
        fail(f"not page slugs: {', '.join(bad)}")
    files = [project / "scraped" / f"{s}.html" for s in (slugs if slugs is not None else copied(project).values())]
    return fresh(files, lambda: {f.stem: media_in(read_text(f)) for f in files if f.exists()})


def fetch_media(project, src, progress=None):
    """Download one left-out medium into scraped/assets/ and return its file name (place_media then puts it
    in the pages). progress(bytes so far, total or 0) while it loads."""
    project = Path(project)
    if not re.match(r"https?://", src):
        fail(f"{src} is not a web address.")
    local = bool(sitemap_of(project).get("local"))
    if not local and private(src):
        fail(f"{src} leads to this computer or its network.")
    assets = project / "scraped" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    with (site_index.OPEN_LOCAL if local else site_index.OPEN)(Request(src, headers={"User-Agent": UA}), timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0) if str(r.headers.get("Content-Length") or "").isdigit() else 0
        if total > MEDIA_MAX:
            fail(f"{src} is larger than {MEDIA_MAX // 1_000_000} MB.")
        name = asset_filename(src, r.headers.get("Content-Type", ""))
        part, done = assets / (name + ".part"), 0
        try:
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MEDIA_MAX:
                        fail(f"{src} is larger than {MEDIA_MAX // 1_000_000} MB.")
                    f.write(chunk)
                    if progress:
                        progress(done, total)
            if total and done != total:          # the connection broke off
                fail(f"the download of {src} stopped at {megabytes(done)} of {megabytes(total)}; try again.")
            part.replace(assets / name)
        finally:
            part.unlink(missing_ok=True)
    return name


def place_media(project, src, name):
    """A downloaded medium back where its placeholders were: in the copies and the edited originals."""
    project, stamp, pages = Path(project), str(int(time.time())), []
    with project_lock(project):           # a copy run may be rewriting the same pages
        for folder, prefix in (("scraped", "assets/"), (ORIGINAL, "../scraped/assets/")):
            for f in sorted((project / folder).glob("*.html")):
                html = read_text(f)
                new = restore_media(html, src, prefix + name, stamp) if "<waib-media" in html else html
                if new != html:
                    write_text(f, new)
                    pages.append(f"{folder}/{f.name}")
    return pages


def get_media(project, slugs=None, what=("all",)):
    """Download the left-out media of these pages that match what: 'image', 'video', 'all' or addresses."""
    found, lines, done = media(project, slugs), [], set()
    if slugs:
        lines += [f"{s}: not a copied page" for s in slugs if s not in found]
    for slug, items in found.items():
        for m in items:
            if not ("all" in what or m["kind"] in what or m["src"] in what) or m["src"] in done:
                continue
            done.add(m["src"])
            if m["kind"] == "audio":
                lines.append(f"{slug}: {m['name']} is audio, which a copy never keeps (not needed for the design)")
                continue
            try:
                name = fetch_media(project, m["src"])
                lines.append(f"{slug}: {m['kind']} {m['name']} -> scraped/assets/{name} (in {', '.join(place_media(project, m['src'], name))})")
            except (OSError, ValueError) as e:
                lines.append(f"{slug}: {m['name']} could not be downloaded: {e}")
    return "\n".join(lines) or "Nothing to download: the copies have all media of that kind."


def list_media(project, slugs=None):
    lines = [f"{slug}: {m['kind']} {m['name']} {megabytes(m['bytes']) or 'size unknown'} {m['src']}"
             for slug, items in media(project, slugs).items() for m in {m["src"]: m for m in items}.values()]   # (shown twice on a page: once)
    return "\n".join(lines) or "The copies left nothing out."


# --- Export --------------------------------------------------------------------

def export(project):
    """The ★ design's pages on the Redesign list into export/, ready to upload (pages, css/, js/, assets/). Links to
    exported pages point to them, the rest to the live site. Returns a report; 'Warning:' lines need a look."""
    project = Path(project)
    data, opts = load(project), settings(project)
    chosen = data["chosen"] or fail("no design chosen for the website yet.")
    by_url = copied(project)
    wanted = list(dict.fromkeys(by_url[canonical(u)] for u in opts.get("redesign", []) if canonical(u) in by_url))   # two addresses, one copy: once
    url_of = {slug: url for url, slug in by_url.items()}
    here = str(sitemap_of(project).get("home") or "") if opts.get("code") else None   # a site started from its code runs at a local address
    out = project / "export"
    warnings = []

    if chosen["ds"] == ORIGINAL:     # the edited original where there is one, else the copy as it is
        def source(slug):
            edited = project / ORIGINAL / f"{slug}.html"
            return edited if edited.exists() else project / "scraped" / f"{slug}.html"
        assets, others = project / "scraped" / "assets", []
        name = "the original"
    else:
        ds = need_ds(data, chosen["ds"])
        v = need_variant(ds, chosen["variant"])
        src, assets, name = project / v["dir"], project / "assets", f"{ds['id']}/{v['id']}"
        others = [(project / ds["dir"] / "tokens.css", out / "css" / "tokens.css")]
        others += [(f, out / ("css" if f.suffix == ".css" else "js") / f.name) for f in sorted(src.iterdir())
                   if f.suffix in (".css", ".js")]

        def source(slug):
            return src / f"{slug}.html"
    pages = [s for s in wanted if source(s).exists()]
    if not pages:
        fail(f"none of the pages to redesign is built in {name} yet.")
    if out.exists():                 # only now: a failed export keeps the last one
        shutil.rmtree(out)

    exported = set(pages)
    warnings += [f"{s}.html is not built in {name} yet: not exported" for s in wanted if s not in exported]
    for slug in pages:
        html = read_text(source(slug))
        if chosen["ds"] == ORIGINAL:   # as the app shows it: no scripts, no cookie banners; left-out media from the live site
            html = restore_media(strip_scripts(html))
            html = re.sub(r'\sdata-waib-fetched="\d*"', "", html)
            if here:                   # a site started from its code: its own files by their paths, not the local address
                html = in_markup(html, lambda markup: markup.replace(here, "/"))
            html = hide_banners(html, project, slug)
        dead = []
        html = relink(in_markup(html, export_paths), url_of.get(slug, ""), by_url, url_of, exported, relative=chosen["ds"] == ORIGINAL, dead=dead, here=here)
        warnings += [f"{slug}.html: link {d} has no page in the export" for d in sorted(set(dead))]
        write_text(out / f"{slug}.html", html)
        refs = markup_findall(UP_REF, html)
        warnings += [f"{slug}.html: path {r} has no file in the export" for r in sorted(set(refs))]
    for f, dest in others:
        if not f.exists():
            warnings.append(f"{f.relative_to(project)} is missing")
            continue
        text = re.sub(r"(?:\.\./)+assets/", "../assets/", read_text(f))
        write_text(dest, re.sub(r"(?:\.\./)+tokens\.css", "tokens.css", text))
    if assets.is_dir():
        shutil.copytree(assets, out / "assets", ignore=shutil.ignore_patterns("*.part"))   # (a download under way)
    used = {}                           # assets/<file> the pages and stylesheets point to -> where
    for f in sorted(out.glob("*.html")) + sorted(out.glob("css/*.css")):
        text = read_text(f)
        for a in markup_findall(ASSET_REF, text) if f.suffix == ".html" else ASSET_REF.findall(text):
            used.setdefault(unquote(a), str(f.relative_to(out)))
    if used and not assets.is_dir():
        warnings.append(f"{assets.relative_to(project)}/ is missing — no images were exported.")
    elif used:
        warnings += [f"{where}: assets/{a} has no file in the export" for a, where in sorted(used.items()) if not (out / "assets" / a).is_file()]

    report = [f"Exported {plural(len(pages), 'page')} of {name} to {out}:"] + [f"  {s}.html" for s in pages]
    return "\n".join(report + [f"Warning: {w}" for w in warnings])


REF = r"""((?:\b(?:href|src)\s*=\s*["']?|url\(\s*["']?|@import\s+["']))(?:\./)?"""   # where a path starts


def export_paths(markup):
    """A page's paths as export/ lays the files out (used in its tags and <style>, not its text)."""
    markup = re.sub(r"(?:\.\./)+(?:scraped/)?assets/", "assets/", markup)
    markup = re.sub(r"(?:\.\./)+tokens\.css", "css/tokens.css", markup)
    markup = re.sub(REF + r"([\w.-]+\.css)\b", r"\1css/\2", markup, flags=re.I)
    return re.sub(REF + r"([\w.-]+\.js)\b", r"\1js/\2", markup, flags=re.I)


def hide_banners(html, project, slug):
    """The original as the app shows it: the cookie banners the render found hidden and their scroll
    lock undone (without scripts nobody could close them)."""
    rendered = read_dict(Path(project) / "content" / f"{slug}.json").get("rendered")
    rendered = rendered if isinstance(rendered, dict) else {}
    found = sorted({s for r in rendered.values() if isinstance(r, dict) for s in r.get("banners") or [] if isinstance(s, str) and "<" not in s})
    if not found:
        return html
    pinned = any(isinstance(r, dict) and r.get("pinned") for r in rendered.values())   # the banner fixed the page in place (as measure's UNLOCK undoes)
    style = ("<style>" + ", ".join(found) + " { display: none !important; } html, body { overflow-y: visible !important; }"
             + (" html, body { position: static !important; top: auto !important; height: auto !important; width: auto !important; }" if pinned else "") + "</style>")
    return re.sub(r"</head\s*>", lambda m: style + m.group(0), html, count=1, flags=re.I) if re.search(r"</head\s*>", html, re.I) else style + html


ASSET_REF = re.compile(r"""(?:^|(?<=["'(=,\s]))(?:\.\./)*assets/([^"'\s)>?#,]+)""")   # an asset address (not "assets/" in a word)
UP_REF = re.compile(r"""(?:\b(?:href|src|srcset|poster)\s*=\s*["']?|url\(\s*["']?|@import\s+["'])(\.\./[^"'\s)>]*)""", re.I)   # a path out of the export


def relink(html, page_url, by_url, url_of, exported, relative, dead=None, here=None):
    """Links to exported pages -> '<slug>.html'; others -> the live site. A design
    links its own built pages as '<slug>.html' and all others absolutely; the
    original's links are relative to its live address. dead: collects a design's
    links to pages that are neither exported nor known (they would lead nowhere).
    here: the local address of a site started from its code: its links become the site's own paths."""
    def fix(href):                      # the new address, or None to keep the attribute as written
        new = address(href)
        return "/" + new[len(here):] if here and new and new.startswith(here) else new

    def address(href):
        if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")) or re.match(r"(?:css|js|assets)/", href):
            return None
        if not relative and not re.match(r"https?://", href):
            built = re.fullmatch(r"(?:\./)?([\w.-]+)\.html(#.*)?", href)   # a page of this design that isn't exported
            if built and built.group(1) not in exported and built.group(1) in url_of:
                return url_of[built.group(1)] + (built.group(2) or "")
            if built and built.group(1) not in exported and dead is not None:
                dead.append(href)
            return None
        absolute = urljoin(page_url, href) if page_url else href
        slug = by_same.get(same_page(absolute.split("#")[0]))
        if slug in exported:
            return f"{slug}.html" + ("#" + absolute.split("#", 1)[1] if "#" in absolute else "")
        return absolute if re.match(r"https?://", absolute) else None

    def attr(a):                        # href="…", href='…' or href=… (old sites write all three); other attributes as they are
        if a.group(2).lower() != "href" or not a.group(3):
            return a.group(0)
        new = fix(unescape(a.group(3).strip("\"'")))
        return a.group(0) if new is None else f'{a.group(1)}href="{escape(new)}"'

    by_same = {same_page(u): slug for u, slug in by_url.items()}   # www. or not, /a or /a/
    return TAG_RE.sub(lambda m: ATTR_RE.sub(attr, m.group(0)) if re.match(r"<(a|area)\b", m.group(0), re.I) else m.group(0), html)   # links only (not a canonical <link>), not href="…" in the text


# --- CLI -------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Manage the designs of a Waib project")
    parser.add_argument("--project-dir", required=True, help="Waib project directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-ds", help="Add a design system (variants/<id>/)")
    p.add_argument("--label", required=True)
    p.set_defaults(run=lambda a: add_ds(a.project_dir, a.label))

    p = sub.add_parser("add-variant", help="Add a variant (variants/<ds>/<id>/)")
    p.add_argument("--ds", required=True)
    p.add_argument("--label", required=True)
    p.set_defaults(run=lambda a: add_variant(a.project_dir, a.ds, a.label))

    p = sub.add_parser("add-page", help="Add a page to a project started from scratch")
    p.add_argument("--title", required=True)
    p.set_defaults(run=lambda a: add_page(a.project_dir, a.title))

    p = sub.add_parser("list", help="Put pages the user asked for in the chat on their Redesign list")
    p.add_argument("--add", nargs="+", required=True, metavar="URL")
    p.set_defaults(run=lambda a: put_on_list(a.project_dir, a.add))

    p = sub.add_parser("choose", help="Choose the design for the website (★)")
    p.add_argument("--ds", required=True, help="a DS id, or 'original'")
    p.add_argument("--variant")
    p.set_defaults(run=lambda a: choose(a.project_dir, a.ds, a.variant))

    # (copies to compare, discarding and renaming are the user's, in the app)
    p = sub.add_parser("original", help="Copy a scraped page to original/<slug>.html for editing")
    p.add_argument("--page", required=True, help="the page's slug")
    p.set_defaults(run=lambda a: original(a.project_dir, a.page))

    p = sub.add_parser("media", help="List the media the copies left out (videos, audio, large images), or download them")
    p.add_argument("--page", action="append", help="a page's slug (repeat for more; default: every copied page)")
    p.add_argument("--get", nargs="+", metavar="WHAT", help="download: image, video, all, or the addresses listed")
    p.set_defaults(run=lambda a: get_media(a.project_dir, a.page, a.get) if a.get else list_media(a.project_dir, a.page))

    p = sub.add_parser("export", help="Write the chosen design's pages to export/")
    p.set_defaults(run=lambda a: export(a.project_dir))

    args = parser.parse_args()
    try:
        print(args.run(args))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

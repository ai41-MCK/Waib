"""
The evidence for a page report: the saved copy rendered like a desktop browser
(1280 px) and a phone (390 px, with phone scaling), screenshots of both, and
the verdicts of every check a script can decide. The agent judges the rest and
writes the report; verify() checks that the report is complete.

The checks, their questions and weights live in skills/waib/checklist.json.
"""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from common import (
    CONSENT, CONSENT_ANSWER, CONSENT_ANSWER_LINK, CONSENT_CONTROLS, CONSENT_LABEL_MAX, LINK_DECOR, as_browser, contact, decorative, fill, fresh, guard_public, megabytes,
    one_line, plural, read_json, read_text, viewport_meta,
)

CHECKLIST = Path(__file__).resolve().parent.parent / "skills" / "waib" / "checklist.json"
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
SHOT_HEIGHT = 3  # screens per screenshot

# SELECTOR_OF and AS_THE_APP_SHOWS (with IS_BANNER and UNLOCK) run in the GUI too (app.py fills them into app.html),
# so a selector or a banner means the same in a report and in the app.
# The shortest `a > b > c` path that matches exactly this element, anchored at an id when there is one.
SELECTOR_OF = r"""el => { const d = el.ownerDocument, parts = [];
  for (; el && el.nodeType === 1 && el !== d.documentElement; el = el.parentElement) {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id) && d.querySelectorAll('#' + el.id).length === 1) { parts.unshift('#' + el.id); }   // old sites repeat ids
    else { let p = el.tagName.toLowerCase(); const c = [...el.classList].filter(x => /^-?[A-Za-z_][\w-]*$/.test(x)).slice(0, 2);
      if (c.length) p += '.' + c.join('.');
      const same = el.parentElement ? [...el.parentElement.children].filter(x => x.tagName === el.tagName) : [];
      if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      parts.unshift(p); }
    if (d.querySelectorAll(parts.join(' > ')).length === 1) break; }
  return parts.join(' > ') || 'html'; }"""
# A cookie banner (CONSENT): fixed, sticky or a dialog at any size (its vendor list may be most of the text), else a small
# box with consent buttons; never a page-wide wrapper or a privacy page's section (analyze.drop_banners: the same rule).
IS_BANNER = r"""el => { const d = el.ownerDocument, total = d.body ? d.body.textContent.length || 1 : 1;
  if (el === d.body || el === d.documentElement || el.matches('a, label, input, button, select, textarea')) return false;   // "Cookie-Einstellungen"
  const pos = d.defaultView.getComputedStyle(el).position, answer = new RegExp(__ANSWER__, 'i'), wrap = el.parentElement && el.parentElement.closest('form, footer');
  const answers = b => { const t = (b.value || b.textContent || '').trim();   // a short label, not a link to another page
    return t.length <= __MAX__ && answer.test(t) && !(b.matches('a[href]') && !answerLink(b.getAttribute('href'))); };
  const answerLink = href => new RegExp(__ANSWER_LINK__, 'is').test(href);   // "#", "javascript:", "?cookies=ok": an answer, not "#necessary"
  return pos === 'fixed' || pos === 'sticky' || /^(alert)?dialog$/.test(el.getAttribute('role') || '') || el.getAttribute('aria-modal') === 'true'
    || el.textContent.length < total / 2 && [...el.querySelectorAll(__CONTROLS__)].some(answers)
       && !(wrap && wrap.textContent.length < total / 2);   // not a form's consent field (a page-wide <form> of old ASP.NET sites is no form)
}"""
IS_BANNER = fill(IS_BANNER, {"__ANSWER__": json.dumps(CONSENT_ANSWER), "__CONTROLS__": json.dumps(CONSENT_CONTROLS), "__MAX__": str(CONSENT_LABEL_MAX),
                             "__ANSWER_LINK__": json.dumps(CONSENT_ANSWER_LINK)})

# A consent banner often locks the page (html.sp-message-open body {overflow: hidden; position: fixed}) until
# it is answered: with the banner hidden, the page scrolls again.
UNLOCK = r"""(d => { for (const el of [d.documentElement, d.body]) { if (!el) continue; const s = d.defaultView.getComputedStyle(el);
  if (s.overflowY === 'hidden') el.style.setProperty('overflow-y', 'visible', 'important');   // only the lock: the page's own layout stays
  if (s.position === 'fixed') for (const [k, v] of [['position', 'static'], ['height', 'auto'], ['width', 'auto'], ['top', 'auto']]) el.style.setProperty(k, v, 'important'); } })"""

MENU = "nav, [role=navigation], [class*=nav i], [id*=nav i], [class*=menu i], [id*=menu i]"   # a menu, also <div id="navigation">

# Runs in the rendered page; returns plain numbers and a few examples with selectors.
PROBE = r"""screen => {
  const MENU = __MENU__;
  const W = innerWidth, H = innerHeight, scale = screen / W;   // a page without a viewport is shrunk to fit the phone
  const visible = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);   // 1 px labels and aria-hidden copies are for machines
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && !el.closest('[aria-hidden=true], waib-media'); };   // (a placeholder of a left-out medium is the app's, not the page's)
  const sel = __SELECTOR_OF__, isBanner = __IS_BANNER__;
  // [r, g, b, alpha] of any CSS colour (also lab(), oklch(), color()), as the browser paints it
  const paint = document.createElement('canvas').getContext('2d', {willReadFrequently: true}), seen = {};
  const rgb = c => { if (!seen[c]) { paint.clearRect(0, 0, 1, 1); paint.fillStyle = '#000'; paint.fillStyle = c; paint.fillRect(0, 0, 1, 1);
      const d = paint.getImageData(0, 0, 1, 1).data; seen[c] = [d[0], d[1], d[2], d[3] / 255]; } return seen[c]; };
  const over = ([r, g, b, a], under) => [r * a + under[0] * (1 - a), g * a + under[1] * (1 - a), b * a + under[2] * (1 - a)].map(Math.round);
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const media = 'img, picture, video, canvas, iframe, svg image';
  const overPicture = (el, owner) => {   // a photo between the text and its solid background (asked only for text that looks unreadable: it scrolls)
    const kept = [];                    // sliders scroll too: put them back, the screenshot shows the page as it loads
    for (let p = el.parentElement; p; p = p.parentElement) kept.push([p, p.scrollLeft, p.scrollTop]);
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    const stack = document.elementsFromPoint(x, y), below = owner ? stack.indexOf(owner) : -1;   // topmost first
    const found = stack.some((u, i) => (below < 0 || i < below) && u !== el && !u.contains(el) && !el.contains(u)
      && (u.matches(media) || getComputedStyle(u).backgroundImage !== 'none'));   // a button with its own colour over a photo: measured
    for (const [p, left, top] of kept) p.scrollTo({left, top, behavior: 'instant'});   // instant: a smooth-scrolling slider too
    return found; };
  let owner = null;                    // the element whose solid background the text is on (null: the page's white)
  const background = el => { const layers = [];   // see-through backgrounds are blended down to the first solid one
    owner = null;
    for (; el; el = el.parentElement) { const s = getComputedStyle(el);
      if (s.backgroundImage !== 'none' || ['::before', '::after'].some(p => { const ps = getComputedStyle(el, p);   // a painted band: unknown
        return ps.content !== 'none' && (ps.backgroundImage !== 'none' || rgb(ps.backgroundColor)[3] > 0); })) return null;
      const c = rgb(s.backgroundColor);
      if (c[3] > 0) layers.push(c); if (c[3] > .99) { owner = el; break; } }
    return layers.reverse().reduce((under, c) => over(c, under), [255, 255, 255]); };
  const texts = [...document.querySelectorAll('body *')].filter(el =>
    [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1) && visible(el));
  const sizes = {}, lowContrast = [], pairs = new Set();
  let overImages = 0, measured = 0;
  const finePrint = 'footer, small, sup, sub, [class*=footnote], [class*=legal], [class*=disclaimer]';   // not the body text
  for (const el of texts) {
    const s = getComputedStyle(el), px = parseFloat(s.fontSize),
      len = [...el.childNodes].filter(n => n.nodeType === 3).reduce((a, n) => a + n.textContent.trim().length, 0);
    const seen = Math.round(px * (scale < 1 / 1.1 ? scale : 1));   // a page shrunk by a few % fits (as mobile.overflow says)
    if (!el.closest(finePrint)) sizes[seen] = (sizes[seen] || 0) + len;
    const bg = background(el); if (!bg) { overImages++; continue; }
    const fg = over(rgb(s.color), bg), l1 = lum(fg), l2 = lum(bg), ratio = (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
    const large = px >= 24 || (px >= 18.66 && parseInt(s.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5) && overPicture(el, owner)) { overImages++; continue; }
    measured++;
    if (ratio < (large ? 3 : 4.5)) { pairs.add(fg.join() + '/' + bg.join());
      lowContrast.push({selector: sel(el), ratio: +ratio.toFixed(2), text: el.textContent.trim().slice(0, 40)}); }
  }
  scrollTo({left: 0, top: 0, behavior: 'instant'});
  const body = Object.entries(sizes).sort((a, b) => b[1] - a[1])[0];
  const targets = [...document.querySelectorAll('a[href], button, input, select, textarea')].filter(visible);
  // WCAG 2.2 target size: under 24 px only counts when another target is closer than 24 px (or the page is shrunk).
  const box = el => { const rs = [el, ...el.querySelectorAll('img, svg')].map(x => x.getBoundingClientRect()).filter(r => r.width && r.height);
    if (!rs.length) return el.getBoundingClientRect();   // an inline link around a picture is as big as the picture
    const l = Math.min(...rs.map(r => r.left)), t = Math.min(...rs.map(r => r.top));
    return {left: l, top: t, width: Math.max(...rs.map(r => r.right)) - l, height: Math.max(...rs.map(r => r.bottom)) - t}; };
  const centre = el => { const r = box(el); return [(r.left + r.width / 2) * scale, (r.top + r.height / 2) * scale]; };
  const spots = targets.slice(0, 400).map(centre);
  // WCAG 2.2 exempts links inside running text
  const blockOf = e => { let b = e;   // the paragraph something sits in, through <b>, <font>, <span> …
    while (b && b.parentElement && getComputedStyle(b).display === 'inline') b = b.parentElement;
    return b; };
  const block = el => el.parentElement && blockOf(el.parentElement);
  const ownText = b => { let n = 0; const walk = document.createTreeWalker(b, NodeFilter.SHOW_TEXT);   // the words on the links' own lines, not the links
    while (walk.nextNode()) { const p = walk.currentNode.parentElement;
      if (!p.closest('a, button') && blockOf(p) === b) n += walk.currentNode.textContent.trim().length; }
    return n; };
  const inText = el => { const b = el.textContent.trim() && !el.querySelector('img') && getComputedStyle(el).display === 'inline' && block(el);   // a text link, not an image
    return !!b && b !== document.body && ownText(b) > 20; };   // a row of links ("Home | About") is no running text
  const small = targets.slice(0, 400).filter((el, i) => { const r = box(el);
    return !inText(el) && (r.width * scale < 24 || r.height * scale < 24) && (scale < .99 ||
      spots.some(([x, y], j) => j !== i && Math.hypot(x - spots[i][0], y - spots[i][1]) < 24)); });
  const consent = __CONSENT__;
  const inBanner = el => { const b = el.closest(consent); return !!b && isBanner(b); };
  const onScreen = el => { const r = el.getBoundingClientRect(); return r.top < H && r.bottom > 0 && r.left < W && r.right > 0; };   // not a closed drawer
  const shows = c => rgb(c)[3] > 0;   // a colour one can see (transparent and rgba(…, 0) are none)
  const looksLikeButton = el => { const cs = getComputedStyle(el);   // a link with its own background or border and padding, not in a menu
    const menu = el.closest(MENU), whole = document.body ? document.body.textContent.length : 1;   // a menu, not a page-wide "has-navbar" wrapper
    return el.matches('a[href]') && !(menu && menu !== document.body && menu !== document.documentElement && menu.textContent.length < whole / 2)
      && parseFloat(cs.paddingLeft) >= 6
      && (shows(cs.backgroundColor) || parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none' && shows(cs.borderTopColor)); };
  const firstScreen = targets.filter(el => onScreen(el) && !inBanner(el) &&
    !el.matches('[aria-expanded], [aria-controls], select') &&      // menu and language toggles aren't actions
    !(/toggle|hamburger|burger/i.test(String(el.className) + ' ' + el.id)
      || (!el.matches('a[href]') && /menu|nav/i.test(String(el.className) + ' ' + el.id))) &&   // a menu button; a link styled as a button counts
    (el.matches('a[href^="tel:"], button, input, textarea') || /(^|[\s_-])(btn|button|cta)([\s_-]|$)/i.test(el.className) || looksLikeButton(el)));
  scrollTo({left: 9999, top: 0, behavior: 'instant'});   // instant: html {scroll-behavior: smooth} would still be at 0
  const ox = getComputedStyle(document.documentElement).overflowX, bx = document.body ? getComputedStyle(document.body).overflowX : 'visible';
  const clipped = ['hidden', 'clip'].includes(ox) || (ox === 'visible' && ['hidden', 'clip'].includes(bx));   // the user can't scroll it, a script can
  const sideways = scrollX > 0 && !clipped;   // a page with overflow-x: hidden is wider but can't scroll
  scrollTo({left: 0, top: 0, behavior: 'instant'});
  const navs = [...document.querySelectorAll('nav, [role=navigation], #nav, #navigation, #menu, #links, .nav, .menu')].filter(visible);   // (not MENU: a page-wide "has-navbar" wrapper is no menu)
  const wideNav = navs.filter(n => { const r = n.getBoundingClientRect(); return r.left < W && r.right > W + 1; });
  const imgs = [...document.images].filter(visible);
  return {width: W, scrollWidth: (document.scrollingElement || document.documentElement).scrollWidth, sideways, scale: +scale.toFixed(3),
    bodyFont: body ? +body[0] : null, lowContrast: lowContrast.slice(0, 8), lowContrastCount: lowContrast.length, lowContrastPairs: pairs.size,
    contrastMeasured: measured, overImages,
    tapTargets: targets.length, smallTargets: small.length, smallExamples: small.slice(0, 5).map(sel),
    firstScreenActions: firstScreen.map(el => (el.getAttribute('href') || el.tagName.toLowerCase()).slice(0, 60)),
    navWider: wideNav.map(sel),
    broken: imgs.filter(i => !i.naturalWidth).map(sel),
    upscaled: imgs.filter(i => i.naturalWidth && i.getBoundingClientRect().width > i.naturalWidth * 1.15).map(i =>
      ({selector: sel(i), natural: i.naturalWidth, shown: Math.round(i.getBoundingClientRect().width)}))};
}"""
PROBE = fill(PROBE, {"__CONSENT__": json.dumps(CONSENT), "__SELECTOR_OF__": SELECTOR_OF, "__IS_BANNER__": IS_BANNER, "__MENU__": json.dumps(MENU)})


def checklist():
    """skills/waib/checklist.json, read again only when it changes (the app scores on every update)."""
    return fresh([CHECKLIST], lambda: read_json(CHECKLIST))


# The copy as the app shows it (the GUI runs this too): cookie banners hidden, the outermost of each, and no
# <noscript> (the app runs pages with scripting on, only blocked). Hidden, not removed: the report's selectors
# must match the copy as saved; data-waib-hidden lets the GUI show one again that a finding points at.
AS_THE_APP_SHOWS = r"""d => {
  d = d || document;   // (Playwright passes null)
  const isBanner = __IS_BANNER__, selectorOf = __SELECTOR_OF__, all = [...d.querySelectorAll(__CONSENT__)].filter(isBanner);
  const banners = all.filter(el => !all.some(o => o !== el && o.contains(el)));
  for (const el of banners) { el.dataset.waibHidden = ''; el.style.setProperty('display', 'none', 'important'); }
  const fixed = !!banners.length && [d.documentElement, d.body].some(el => el && d.defaultView.getComputedStyle(el).position === 'fixed');
  if (banners.length) __UNLOCK__(d);
  for (const el of d.querySelectorAll('noscript')) el.style.setProperty('display', 'none', 'important');
  return {banners: banners.map(selectorOf), fixed};   // analyze drops exactly these from the page text; fixed: the banner pinned the page
}"""
AS_THE_APP_SHOWS = fill(AS_THE_APP_SHOWS, {"__CONSENT__": json.dumps(CONSENT), "__IS_BANNER__": IS_BANNER, "__SELECTOR_OF__": SELECTOR_OF, "__UNLOCK__": UNLOCK})


def render(pages, content, local_ok=False):
    """Render each copy at desktop and phone width: screenshots into content/, probe results back.
    pages: {slug: path of the copy}; local_ok: the site itself is on this computer or network.
    Returns {slug: {"desktop": {...}, "phone": {...}}}."""
    from playwright.sync_api import sync_playwright  # only needed here
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Without the page's scripts, as the app shows the copy (no banners, widgets or animations mid-way).
        contexts = {"desktop": browser.new_context(viewport=DESKTOP, java_script_enabled=False),
                    "phone": browser.new_context(viewport=PHONE, is_mobile=True, has_touch=True, java_script_enabled=False)}
        # A copy opened from disk may load only files of its own folder (a hostile page could name ~/.ssh/…).
        roots = {Path(p).resolve().parent for p in pages.values()}

        def local_files(route):
            try:
                target = Path(unquote(urlparse(route.request.url).path)).resolve()
            except (ValueError, OSError):
                return route.abort()
            return route.continue_() if any(r == target or r in target.parents for r in roots) else route.abort()
        for ctx in contexts.values():
            ctx.route(lambda url: url.startswith("file:"), local_files)
            if not local_ok:             # a public site's copy loads nothing from this computer or network
                guard_public(ctx, hops=True)
        for slug, path in pages.items():
            out[slug] = {}
            for kind, ctx in contexts.items():
                page = ctx.new_page()
                try:
                    page.goto(Path(path).resolve().as_uri(), wait_until="domcontentloaded", timeout=30000)
                    try:                        # images from the live site: a dead host must not stop the screenshot
                        page.wait_for_load_state("load", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    hidden = page.evaluate(AS_THE_APP_SHOWS)
                    out[slug][kind] = {**page.evaluate(PROBE, (PHONE if kind == "phone" else DESKTOP)["width"]),
                                       "banners": hidden["banners"], "pinned": hidden["fixed"]}
                    # The page's own layout size: a phone page without a viewport is laid out at
                    # 980 px and shown shrunk, so the screenshot shows all of it, as the phone does.
                    w, h, full = page.evaluate("[innerWidth, innerHeight, (document.scrollingElement || document.documentElement).scrollHeight]")
                    page.screenshot(path=str(Path(content) / f"{slug}.{kind}.jpg"), type="jpeg", quality=70,
                                    full_page=True, clip={"x": 0, "y": 0, "width": w, "height": min(full, h * SHOT_HEIGHT)})
                except Exception as e:  # a page that won't render still gets its HTML facts
                    out[slug][kind] = {"error": str(e)[:200]}
                finally:
                    page.close()
        browser.close()
    return out


def result(verdict, evidence, selector=None):
    return {"verdict": verdict, "evidence": evidence, **({"selector": selector} if selector else {})}


def checks(soup, facts, rendered, network, titles):
    """The verdicts of the measured checks: {id: {verdict, evidence, selector?}}."""
    d, ph = rendered.get("desktop") or {"error": "not rendered"}, rendered.get("phone") or {"error": "not rendered"}
    c = {}
    h1 = [h for h in soup.find_all("h1") if h.get("aria-hidden") != "true" and not h.find_parent(attrs={"aria-hidden": "true"})]
    named = [h for h in h1 if h.get_text(strip=True)]
    c["structure.h1"] = result("pass" if len(h1) == 1 and named else "fail",
                               f"{len(h1)} H1" + (", empty" if h1 and not named else "") + (f": {named[0].get_text(' ', strip=True)[:60]}" if named else ""))
    levels = [int(h.name[1]) for h in soup.find_all(re.compile(r"^h[1-6]$")) if not h.find_parent(["nav", "aside", "footer"])]   # the content's outline
    skips = [f"h{a} → h{b}" for a, b in zip(levels, levels[1:]) if b > a + 1]
    c["structure.outline"] = result("warn" if skips or (levels and levels[0] != 1) else "pass",
                                    ", ".join(skips[:4]) or (f"starts with h{levels[0]}" if levels and levels[0] != 1 else "headings in order"))
    found = contact(soup)                # the same as the page facts (analyze.contact_info)
    tel, mail, phone_text, mail_text = found["tel"], found["mailto"], found["phones"], found["emails"]
    missing = [what for what, has, shown in (("phone", tel, phone_text), ("email", mail, mail_text)) if shown and not has]
    c["action.contact"] = result("warn" if missing else "pass" if tel or mail else "fail",
                                 ("not clickable: " + ", ".join(missing)) if missing else
                                 " and ".join(w for w, has in (("tel:", tel), ("mailto:", mail)) if has) + " link present" if tel or mail
                                 else "no phone number or email on the page")
    vague = [a.get_text(strip=True) for a in soup.find_all("a") if one_line(a.get_text()).strip(LINK_DECOR).lower() in   # also "Mehr »"
             {"hier", "mehr", "weiter", "click here", "here", "more", "read more", "mehr erfahren", "link"}]
    c["nav.link_text"] = result("warn" if vague else "pass", ", ".join(f"“{v}”" for v in vague[:5]) or "link texts say where they go")
    vp = viewport_meta(soup)
    fits = bool(vp and "device-width" in vp.get("content", ""))
    c["mobile.viewport"] = result("pass" if fits else "fail",
                                  f"viewport: {vp.get('content')}" + ("; a fixed width: check whether phones get a separate mobile site"
                                                                         if re.search(r"width\s*=\s*\d", vp.get("content", "")) else "")
                                  if vp else "no viewport meta tag: phones show a shrunk desktop page")
    if "error" not in ph:
        acts, wide = ph.get("firstScreenActions", []), ph.get("navWider", [])
        c["action.first_screen"] = result("pass" if acts else "fail", ", ".join(acts[:3]) or "no phone link, button or form in the first phone screen")
        c["nav.mobile"] = result("fail" if wide else "pass", "navigation wider than the phone screen" if wide else "navigation fits",
                                 wide[0] if wide else None)
        over = ph["scrollWidth"] > ph["width"] + 1 and ph.get("sideways", True)
        shrunk = ph["scale"] < 0.99
        promised = ph["scale"] < 1 / 1.1 and fits   # says it fits the phone, then is laid out much wider (a few px: an ad slot the site's scripts size)
        c["mobile.overflow"] = result("fail" if over or promised else "pass", f"page is {ph['scrollWidth']} px wide on a {PHONE['width']} px wide phone" + (f" (laid out at {ph['width']} px and shrunk)" if shrunk else "")
                                      if over else f"laid out {ph['width']} px wide although its viewport says device-width; the phone shrinks it to {round(ph['scale'] * 100)} %"
                                      if promised else f"no sideways scrolling, but the page is laid out {ph['width']} px wide and shrunk to fit"
                                      if shrunk else "fits the screen")
        font = ph.get("bodyFont")
        if font:
            note = f" (the page is shrunk to {round(ph['scale'] * 100)} %)" if ph["scale"] < 1 / 1.1 else ""   # a few % less counts as fitting
            c["mobile.font"] = result("fail" if font < 14 else "warn" if font < 16 else "pass", f"body text appears at {font} px{note}")
        n = ph.get("smallTargets", 0)
        c["mobile.tap"] = result("fail" if n >= 10 else "warn" if n >= 3 else "pass", f"{n} of {ph.get('tapTargets', 0)} links, buttons and form fields smaller than 24 px",
                                 (ph.get("smallExamples") or [None])[0])
    if "error" not in d:
        n, ex = d.get("lowContrastCount", 0), d.get("lowContrast", [])
        pairs = d.get("lowContrastPairs", n)         # one colour choice used on many links is one problem
        skipped = d.get("overImages", 0)
        note = f"; {skipped} over images or painted bands not measured" if skipped else ""
        c["a11y.contrast"] = result("n/a", f"no text measured: all {skipped} texts are over images") if not d.get("contrastMeasured", 1) and skipped else \
            result("fail" if pairs >= 3 else "warn" if n else "pass",
                   f"{plural(n, 'text element')} in {plural(pairs, 'colour pair')} below WCAG AA" + (f", e.g. “{ex[0]['text']}” at {ex[0]['ratio']}:1" if ex else "") + note,
                   ex[0]["selector"] if ex else None)
    no_alt = [i for i in soup.find_all("img") if i.get("alt") is None and not decorative(i)]
    c["a11y.alt"] = result("warn" if no_alt else "pass", f"{plural(len(no_alt), 'image')} without alt text" if no_alt else "every image has alt text")
    html = soup.find("html")
    c["a11y.lang"] = result("pass" if html and html.get("lang") else "fail", f"lang=\"{html.get('lang')}\"" if html and html.get("lang") else "no lang attribute")
    m = facts["modernization"]
    c["tech.layout"] = result("pass" if m >= 70 else "warn" if m >= 40 else "fail",
                              f"modernization score {m}/100" + "".join(f"; missing: {', '.join(gaps)}" for gaps in [[x["check"] for x in facts["modernization_checks"] if not x["passed"]]] if gaps))
    if network:
        mb, req = network.get("bytes", 0) / 1e6, network.get("requests", 0)
        c["tech.weight"] = result("fail" if mb > 5 else "warn" if mb > 2 or req > 80 else "pass", (megabytes(network.get("bytes", 0)) or "0 KB") + f" in {plural(req, 'request')}")
        s = network.get("load_ms", 0) / 1000
        c["tech.speed"] = result("fail", f"not finished loading after {s:.0f} s") if network.get("load_incomplete") else \
            result("fail" if s > 5 else "warn" if s > 2.5 else "pass", f"loaded in {s:.1f} s" if s >= 0.1 else f"loaded in {s * 1000:.0f} ms")
    final = (network or {}).get("final_url") or facts["url"]          # where the address led
    c["tech.https"] = result("pass" if final.startswith("https:") else "fail", final.split(":")[0])
    title = one_line(soup.title.get_text(" ")) if soup.title else ""
    same = [t for t in titles if t == title]
    c["seo.title"] = result("warn" if not title or len(title) < 15 or len(title) > 65 or len(same) > 1 else "pass",
                            "no title" if not title else f"“{title}”" + (f", also used on {plural(len(same) - 1, 'other page')}" if len(same) > 1 else ""))
    c["seo.description"] = result("pass" if facts["description"] else "warn", facts["description"][:80] or "no meta description")
    hrefs = " ".join((a.get_text(" ") + " " + a.get("alt", "") + " " + a.get("href", "")).lower() for a in soup.find_all(["a", "area"]))   # image maps too
    legal = [n for n, words in (("imprint", ("impressum", "imprint")), ("privacy", ("datenschutz", "privacy"))) if any(w in hrefs for w in words)]
    english = (html.get("lang") or "").lower().startswith("en") if html else False   # English sites have no imprint
    c["trust.legal"] = result("pass" if len(legal) == 2 or (english and "privacy" in legal) else "warn", "links to " + (" and ".join(legal) or "neither imprint nor privacy"))
    return c


def readability(sections):
    """Paragraph and sentence lengths of the page text (to help judge content.readable)."""
    # Text of a few words is a table cell or a label, not a paragraph; "- " starts a list line split with <br>.
    paras = [b[1] for s in sections for b in s["blocks"] if b[0] == "p" and len(b[1].split()) >= 8]
    words = [len(p.split()) for p in paras]
    sentences = [len(x.split()) for p in paras for x in re.split(r"(?<=[.!?])\s+|\s+[-–•]\s+", p) if x.strip()]
    return {"paragraphs": len(paras), "longest_paragraph_words": max(words, default=0),
            "average_sentence_words": round(sum(sentences) / len(sentences), 1) if sentences else 0}


def score(measured, judged):
    """0–100 from all verdicts, by the checklist's scoring rule: weighted, then lowered for each failed must-have."""
    cl = checklist()
    weights = {c["id"]: c["weight"] for c in cl["checks"]}
    value, must_fail = cl["scoring"]["verdicts"], cl["scoring"]["must_fail"]
    got = total = musts = 0
    for cid, r in {**measured, **judged}.items():
        v = r.get("verdict") if isinstance(r, dict) else None
        if isinstance(v, str) and v in value and cid in weights:
            got += weights[cid] * value[v]
            total += weights[cid]
            musts += v == "fail" and weights[cid] == cl["scoring"]["must_weight"]
    return round(100 * got / total * must_fail ** musts) if total else None


def text(value):
    """A report's text field, stripped; '' when it is not a text."""
    return value.strip() if isinstance(value, str) else ""


def verify(project, slug):
    """Problems that keep a page report from being complete: a list of strings (empty = fine)."""
    project = Path(project)
    report = read_json(project / "reports" / f"{slug}.json")
    if not isinstance(report, dict):
        return [f"reports/{slug}.json is missing or not valid JSON"]
    copy = project / "scraped" / f"{slug}.html"
    soup = as_browser(BeautifulSoup(read_text(copy), "lxml")) if copy.exists() else None
    problems = [] if soup is not None else [f"scraped/{slug}.html is missing, so the selectors can't be checked"]
    for field, kind in (("summary", str), ("audience", str), ("tasks", list), ("checks", dict), ("inventory", list),
                        ("keep", list), ("fix", list), ("add", list), ("design_input", list)):
        if not isinstance(report.get(field), kind):
            problems.append(f"'{field}' is missing" + (f" or not {({list: 'a list', dict: 'an object', str: 'a text'})[kind]}" if field in report else ""))
        elif not (report[field].strip() if kind is str else report[field]) and field not in ("keep", "fix", "add"):   # a good page may have nothing to fix
            problems.append(f"'{field}' is empty")
        elif field in ("tasks", "keep", "add", "design_input") and not all(text(x) for x in report[field]):
            problems.append(f"'{field}': every entry must be a text")
    lists = {k: report[k] if isinstance(report.get(k), list) else [] for k in ("fix", "inventory")}
    checks = checklist()["checks"]
    judged, ids = [c["id"] for c in checks if c["kind"] == "judged"], {c["id"] for c in checks}
    given = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    for cid in judged:
        r = given.get(cid)
        if not isinstance(r, dict) or r.get("verdict") not in ("pass", "warn", "fail", "n/a"):
            got = r.get("verdict") if isinstance(r, dict) else None
            problems.append(f"check {cid}: " + (f"verdict {got!r} is not one of" if got else "no verdict:") + " pass, warn, fail, n/a")
        elif r["verdict"] in ("warn", "fail") and len(text(r.get("evidence"))) < 3:
            problems.append(f"check {cid}: a {r['verdict']} needs evidence (a quote, number or observation)")
    for cid in given:
        if cid not in judged:
            problems.append(f"check {cid}: not a judged check of the checklist" + (" (it is measured)" if cid in ids else ""))
    for i, f in enumerate(lists["fix"]):
        f = f if isinstance(f, dict) else {}
        wrong = [w for w, bad in (("text", not text(f.get("text"))), ("severity (high, medium, low)", f.get("severity") not in ("high", "medium", "low")),
                                  (f"check ({f.get('check')!r} is not a check id)" if f.get("check") else "check",
                                   not isinstance(f.get("check"), str) or f.get("check") not in ids)) if bad]
        if wrong:
            problems.append(f"fix #{i + 1}: needs " + ", ".join(wrong))
    for i, b in enumerate(lists["inventory"]):
        if not isinstance(b, dict) or not text(b.get("block")) or b.get("action") not in ("keep", "rewrite", "merge", "drop"):
            problems.append(f"inventory #{i + 1}: needs block and action (keep, rewrite, merge, drop)")
    selectors = [("check " + k, v.get("selector")) for k, v in given.items() if isinstance(v, dict)]
    selectors += [(f"fix #{i + 1}", f.get("selector")) for i, f in enumerate(lists["fix"]) if isinstance(f, dict)]
    selectors += [(f"inventory #{i + 1}", b.get("selector")) for i, b in enumerate(lists["inventory"]) if isinstance(b, dict)]
    for where, s in selectors:
        if s and not isinstance(s, str) or isinstance(s, str) and re.search(r":-soup-|:contains\(", s):
            problems.append(f"{where}: selector {s!r} is not a CSS selector a browser understands")
        elif s and soup is not None:
            try:
                n = len(soup.select(s))
            except Exception:  # an invalid selector
                n = -1
            if n != 1:
                problems.append(f"{where}: selector {s!r} matches {n if n >= 0 else 'no valid'} element(s), not exactly one")
    return problems

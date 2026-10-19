#!/usr/bin/env python3
"""
Waib: a local web GUI for one website-redesign project, wired to the Claude Code sessions that work on it.

  app.py --project-dir <out> [--url <page> | --code <folder or repository>] [--no-open]

Starts the app for the project if it isn't running (in the background, so it outlives the session that
started it), adds the calling session to it, prints the link and returns. --url fills in the Start screen.

The app serves the GUI (templates/app.html) at / and the project's files beside it, so the GUI can look
inside the pages it shows (same origin). It reads the site's pages itself (site_index.py) and runs the
decisions that need no design work. Requests for Claude go to the inbox socket of the session the user
picked; Claude Code exports that socket (and its key) only to processes the session starts, which is why
each session joins by running this script. Without a running session the GUI copies the message instead.
"""

import argparse
import hmac
import json
import os
import posixpath
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manage_variants as mv  # noqa: E402
import measure  # noqa: E402
from common import (  # noqa: E402
    BUTTON, HOST, LABEL_MAX, MEDIA_STYLE, QUIET, TRUSTED_FILE, canonical, claude_dir, clean_label, fill, fresh, is_same_domain, now, one_line, read_dict, truncate, user_hosts_file, with_scheme, write_json,
)
from site_index import AROUND_PICKS, SECTION, SiteIndex  # noqa: E402

APP_HTML = Path(__file__).resolve().parent.parent / "templates" / "app.html"
CODE_REV = str(max(f.stat().st_mtime_ns for f in Path(__file__).resolve().parent.glob("*.py")))  # a running app with older code is replaced
MAX_BODY = 1_000_000
IDLE = 600  # with no page open and no running session, the app stops after 10 minutes
MAX_START = 40  # pages chosen on the Start screen
HOST_PORT = re.compile(HOST)
PLACEHOLDER = "waib-new"  # the project folder before a URL is known; renamed on Start
# Files whose changes don't change a page on screen (the GUI gets them through /api/state).
REV_SKIP = {"scraped", "content", "reports", "export", "code", "variants.json", "ideas.json", "pages.json", "project.json"}   # code: a site's own code (node_modules, builds)
# Pages run same-origin inside the GUI frame (that is what lets the GUI annotate
# them), so a script in a page could drive the GUI. Designs therefore only run
# their own scripts (from variants/; external widgets, analytics and the site's
# copied scripts are blocked), and nothing else served here runs scripts: copies
# of the live site are rendered snapshots, and an export may hold such a copy.
PAGE_CSP = "script-src 'unsafe-inline' http://127.0.0.1:{port}/variants/ http://localhost:{port}/variants/; object-src 'none'"
COPY_CSP = "script-src 'none'; object-src 'none'"
# A copy of a public site loads only from this app, the site, well-known sites and the ones the user allowed
# (Handler.loads): nothing from this computer or network, however the address is written. A local site's
# copies are not limited.
FRAMES_CSP = "; frame-src 'self' {hosts}"
LOADS_CSP = "; default-src 'self' data: blob: {hosts}; style-src 'self' 'unsafe-inline' data: {hosts}; form-action 'none'"
# back in the app as soon as the new link is opened in another tab (app.html stores the token there)
RESTARTED = b"""<!doctype html><meta charset="utf-8"><title>Waib</title>
<p style="font: 16px/1.5 system-ui, sans-serif; max-width: 34em; margin: 4em auto">This link doesn't open the app (the app was restarted, or the link
is incomplete). Ask Claude to open the app, then open the link Claude gives you. This tab follows by itself.</p>
<script>
history.replaceState(null, '', '/' + location.hash);   // an old token out of the address bar
const key = 'waib-token:' + location.port, go = t => location.replace('/?k=' + encodeURIComponent(t) + location.hash);
addEventListener('storage', e => { if (e.key === key && e.newValue) go(e.newValue); });
const t = localStorage.getItem(key);
if (t && sessionStorage.getItem(key) !== t) { sessionStorage.setItem(key, t); go(t); }   // once: it may be the old one
</script>"""
GUI_CSP = "frame-ancestors 'none'"  # no other site may frame the GUI
LOCK = threading.Lock()  # one change to the project at a time
DOWNLOADS = threading.Lock()  # media the user asked for: one download at a time
APPS = {"claude-desktop": "Claude Desktop", "cli": "Claude Code CLI", "claude-vscode": "VS Code"}


def revision(project, skip=REV_SKIP):
    """Changes whenever a file the GUI may be showing changes (or disappears); skip: names at the top left out."""
    latest, count = 0, 0
    for root, dirs, files in os.walk(project):
        top = root == str(project)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (top and d in skip)]
        for f in files:
            if f.startswith(".") or (top and f in skip):
                continue
            try:
                latest = max(latest, os.stat(os.path.join(root, f)).st_mtime_ns)
                count += 1
            except OSError:
                pass
    return f"{latest}-{count}"


# --- Agent sessions ---------------------------------------------------------------

def send_to_claude(inbox, key, text):
    """Post `text` to a session's inbox. Wire format: JSON lines, an optional
    auth line, then one user message."""
    lines = [{"type": "auth", "token": key}] if key else []
    lines.append({"type": "user", "message": {"role": "user", "content": text}})
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(inbox)
            s.sendall("".join(json.dumps(line) + "\n" for line in lines).encode())
        return True
    except OSError:
        return False


def alive(pid, inbox):
    """The session is still there: its inbox exists and its process runs."""
    if not inbox or not os.path.exists(inbox):
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (ValueError, TypeError, OSError):
        pass  # no pid to check, or not ours to signal: the inbox file decides
    return True


def title_of(session_id):
    """What the session is called in the agent: its title, else the first thing
    the user typed (as the resume list shows it). Read from the session's log."""
    title = first = ""
    if re.fullmatch(r"[\w-]+", session_id or ""):
        for log in (claude_dir() / "projects").glob(f"*/{session_id}.jsonl"):
            with open(log, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"custom-title"' not in line and (first or '"type":"user"' not in line):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") == "custom-title":
                        title = entry.get("customTitle") or title
                    elif entry.get("type") == "user" and not first:
                        text = (entry.get("message") or {}).get("content")
                        # plain prompts only: not tool results, command wrappers or a continuation summary
                        if isinstance(text, str) and not text.startswith(("<", "This session is being continued")):
                            first = text
    return truncate(one_line(title or first), 80)


def this_session():
    """The agent session running this script, from the environment it
    passed down and the process table; None outside one."""
    env = os.environ
    inbox = env.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not inbox:
        return None
    entry = env.get("CLAUDE_CODE_ENTRYPOINT", "")
    version = re.search(r"(\d+)-(\d+)-(\d+)", env.get("AI_AGENT", ""))
    pid = env.get("CLAUDE_PID") or Path(inbox).stem
    info = {"id": env.get("CLAUDE_CODE_SESSION_ID") or inbox, "app": APPS.get(entry, entry or "Claude Code"),
            "version": ".".join(version.groups()) if version else "", "pid": pid, "inbox": inbox,
            "key": env.get("CLAUDE_CODE_MESSAGING_TOKEN", ""), "started": "", "folder": ""}
    try:
        started = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True, timeout=2).stdout
        info["started"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.strptime(started.strip(), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        if Path(f"/proc/{pid}/cwd").exists():
            info["folder"] = os.readlink(f"/proc/{pid}/cwd")
        else:
            out = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True,
                                 text=True, timeout=2).stdout
            info["folder"] = next((x[1:] for x in out.splitlines() if x.startswith("n")), "")
    except (OSError, subprocess.SubprocessError):
        pass
    return info


class Sessions:
    """Every session that opened the app for this project. The list (no keys) is
    kept in .sessions.json; inbox keys only live in this process, so after a
    restart of the app a session counts as closed until it opens the app again."""
    FIELDS = ("id", "title", "app", "version", "pid", "inbox", "started", "folder")

    def __init__(self, project):
        self.file = project / ".sessions.json"
        data = read_dict(self.file)
        self.list = [s for s in data.get("sessions", []) if isinstance(s, dict) and s.get("id")]
        self.active = data.get("active")
        self.keys = {}  # session id -> inbox key

    def find(self, session_id):
        return next((s for s in self.list if s["id"] == session_id), None)

    def running(self, s):
        return bool(s) and s["id"] in self.keys and alive(s.get("pid"), s.get("inbox"))

    def join(self, info):
        """A session opened the app. It gets the requests unless the current choice still runs."""
        s = self.find(info["id"])
        if not s:
            s = {"id": info["id"]}
            self.list.append(s)
        s.update({k: str(info.get(k) or "") for k in self.FIELDS if k != "title"})
        s["title"] = title_of(s["id"]) or s.get("title", "")
        self.keys[s["id"]] = str(info.get("key") or "")
        if not self.running(self.find(self.active)):
            self.active = s["id"]
        self.save()

    def choose(self, session_id):
        if not self.running(self.find(session_id)):
            raise ValueError("that session is not running")
        self.active = session_id
        self.save()

    def send(self, text):
        s = self.find(self.active)
        return self.running(s) and send_to_claude(s["inbox"], self.keys[s["id"]], text)

    def view(self, titles=None):
        """The sessions for the GUI; titles: {id: title} read fresh from the logs."""
        for s in self.list:
            s["title"] = (titles or {}).get(s["id"]) or s.get("title", "")
        # running: alive and joined to this app; alive only: it runs, but has to join again (the app restarted)
        return [{**{k: s.get(k, "") for k in self.FIELDS if k != "inbox"}, "running": self.running(s),
                 "alive": alive(s.get("pid"), s.get("inbox"))} for s in self.list]

    def save(self):
        write_json(self.file, {"active": self.active, "sessions": self.list})


# --- The project ------------------------------------------------------------------

def code_name(code):
    """A project started from code is named after its repository or folder: github.com/me/shop/tree/main -> shop."""
    parts = [p for p in re.split(r"[/:]", re.sub(r"^(https?://|git@|ssh://)", "", code.strip())) if p and p != "~"]
    if parts and re.match(r"(www\.)?(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)$", parts[0], re.I):
        parts = parts[:3]               # host, owner, repository (not /tree/<branch>)
    name = re.sub(r"[^\w.-]", "-", re.sub(r"\.git$", "", parts[-1] if parts else ""))
    return name.strip("-.") or "site"


def rename_placeholder(app, name):
    """A project started without a URL lives in waib-new until Start: give it the site's name (or "scratch")."""
    project = app["project"]
    target = project.parent / f"waib-{name}"
    if project.name != PLACEHOLDER or not name or target.exists():
        return False
    project.rename(target)
    app["project"] = target
    app["sessions"].file = target / ".sessions.json"
    app["index"].file = target / "pages.json"
    return True


def download(app, srcs):
    """Download left-out media the user asked for, one after the other, and put each in the pages.
    app["media"][src] shows the progress in the app, or why it failed."""
    with DOWNLOADS:
        for src in srcs:
            job = app["media"].get(src)
            if not job or job.get("error"):
                continue
            try:
                name = mv.fetch_media(app["project"], src, lambda done, total, job=job: job.update(done=done, total=total or job["total"]))
                with LOCK:
                    mv.place_media(app["project"], src, name)
                app["media"].pop(src, None)
            except Exception as e:     # (also http.client's errors): the job always ends, removed or failed
                job["error"] = one_line(str(e) or type(e).__name__)[:200]


def page_list(index, urls):
    return [{"url": u, "label": index.label(u)} for u in urls]


# --- The web app -----------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    """The GUI at /, the project's files and a small JSON API. Only the app's own page may call the API: Host and
    Origin must be this server (no DNS rebinding) and POSTs carry the token. Sessions join with the app's key."""

    def __init__(self, request, client_address, server):
        super().__init__(request, client_address, server, directory=str(server.app["project"]))

    def end_headers(self):
        path = posixpath.normpath(unquote(self.path.split("?")[0].split("#")[0]))   # as served: /variants/../x is no design (no script CSP)
        self.send_header("Cache-Control", "no-store")  # always show the latest edit
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "grant", False):
            self.send_header("Set-Cookie", f"{self.cookie()}={self.server.app['view']}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Security-Policy", GUI_CSP + self.loads(frames=True) if self.path.split("?")[0] == "/"
                         else PAGE_CSP.format(port=self.server.server_port) if path.startswith("/variants/") else COPY_CSP + self.loads())
        super().end_headers()

    def loads(self, frames=False):
        """Where a copy may load from (see LOADS_CSP): the app, the site, well-known sites and the ones the user
        allowed (mv.hosts). With frames: what the app's page may show in its frame (FRAMES_CSP: a copy can't
        move the frame on to an address here). Read again when sitemap.json, the user's list or trusted_hosts.txt changes."""
        project = self.server.app["project"]
        f = project / "scraped" / "sitemap.json"

        def build():
            sitemap = mv.sitemap_of(project)   # (a damaged file: no limit, but the app keeps working)
            ok = isinstance(sitemap.get("origins"), list) and not sitemap.get("local")
            hosts = [h for h in mv.hosts(project, sitemap)["allowed"] if HOST_PORT.fullmatch(h)] if ok else []
            try:
                site = (urlparse(str(sitemap.get("home") or "")).netloc or "").lower().removeprefix("www.")   # the Live view
            except ValueError:
                site = ""
            live = [h for h in (site, "www." + site) if site and HOST_PORT.fullmatch(h)]
            return {"csp": LOADS_CSP.format(hosts=" ".join(hosts)) if ok else "",
                    "frames": FRAMES_CSP.format(hosts=" ".join(dict.fromkeys(live + hosts))) if ok else ""}
        return fresh([f, user_hosts_file(), TRUSTED_FILE], build)["frames" if frames else "csp"]

    def log_message(self, *args):
        pass

    def list_directory(self, path):     # no folder listings
        self.send_error(404)

    def guess_type(self, path):
        kind = super().guess_type(path)
        return kind + "; charset=utf-8" if kind in ("text/html", "text/css", "application/javascript", "application/json") else kind

    def trusted(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or urlparse(origin).netloc in hosts)

    def cookie(self):                   # per port: cookies are shared by every port of a host
        return f"waib-{self.server.server_port}"

    def admitted(self, q, api):
        """Anyone on this Mac can reach the port. The app's link carries the token (?k=); the
        GUI keeps it in the browser (localStorage) and sends it (X-Token); this script sends the key (X-Key).
        For pages and files the browser also gets a cookie with a second secret (`view`):
        cookies go to every port of 127.0.0.1, so it opens files only, never the API."""
        app = self.server.app
        same = lambda a, b: secrets.compare_digest(str(a or "").encode(), b.encode())   # noqa: E731  (bytes: any odd character is just "no")
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            jar = {}
        by_token = same(q.get("k"), app["token"]) or same(self.headers.get("X-Token"), app["token"])
        by_cookie = self.cookie() in jar and same(jar[self.cookie()].value, app["view"])
        self.grant = by_token or by_cookie    # gets the cookie with this answer (never a stranger)
        return by_token or same(self.headers.get("X-Key"), app["key"]) or (by_cookie and not api)

    def hidden(self):                   # dotfiles (.app.json holds the join key) and any ".." step
        return any(part.startswith(".") for part in unquote(self.path.split("?")[0]).split("/"))

    def reply(self, data, status=200, kind="application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        app = self.server.app
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path == "/api/ping" and self.trusted():   # "are you this project's app?": proven with the key, without sending it
            nonce = str(q.get("nonce", ""))[:64]
            return self.reply({"pid": os.getpid(), "proof": hmac.new(app["key"].encode(), nonce.encode(), "sha256").hexdigest()})
        if not self.trusted() or not self.admitted(q, path.startswith("/api/")):
            if path == "/" and self.trusted():   # an old tab reloaded after a restart
                return self.reply(RESTARTED, 403, kind="text/html")
            return self.send_error(403, explain="Open the app with the link app.py printed (it carries the key).")
        index = app["index"]
        if path == "/":
            page = fill(APP_HTML.read_text(encoding="utf-8"), {
                "'__PROJECT__'": json.dumps(str(app["project"]), ensure_ascii=False).replace("<", "\\u003c"), "__SELECTOR_OF__": measure.SELECTOR_OF,
                "__AS_THE_APP_SHOWS__": measure.AS_THE_APP_SHOWS,
                "__PLACEHOLDER__": json.dumps({"style": MEDIA_STYLE, "quiet": QUIET, "button": BUTTON}),
                "__LIMITS__": json.dumps({"start": MAX_START, "around": AROUND_PICKS, "label": LABEL_MAX, "desktop": measure.DESKTOP["width"], "phone": measure.PHONE["width"]})})
            return self.reply(page.encode(), kind="text/html")
        if self.hidden():
            return self.send_error(404)
        if path == "/api/state":
            app["seen"] = time.time()
            project = app["project"]
            try:
                # the logs can be long: read the titles before taking the lock
                titles = {s["id"]: title_of(s["id"]) for s in list(app["sessions"].list)} if "titles" in q else None
                with LOCK:
                    sessions = app["sessions"].view(titles)
                    state = mv.state(project)
                with index.lock:
                    for url, title in state.pop("titles").items():   # copied pages know their titles (a copy made again, its new one)
                        index.add(url, title, copied=True)
                opts = state["settings"]
                index.names = opts.get("names") if isinstance(opts.get("names"), dict) else {}
                urls = [u for u in opts.get("redesign", []) + opts.get("requested", []) + [opts.get("url")] + list(state["copied"])
                        if isinstance(u, str) and u]   # (every copied page: its report names it, on the list or not)
            except (ValueError, TypeError, AttributeError, KeyError, OSError) as e:   # e.g. a report still being written
                return self.reply({"error": f"{type(e).__name__}: {e}"}, 500)
            return self.reply({**state, "labels": {canonical(u): index.label(canonical(u)) for u in urls}, "discovery": index.status(),
                               "rev": revision(project), "docs": revision(project / "reports", ()) + revision(project / "content", ()),
                               "copies": revision(project / "scraped", ("assets",)),   # (a medium downloaded into a copy)
                               "project": str(project), "pid": os.getpid(),
                               "home": str(Path.home()), "sessions": sessions, "active": app["sessions"].active,
                               "downloads": {src: dict(job) for src, job in list(app["media"].items())}})
        if path == "/api/checklist":
            return self.reply(measure.checklist())
        try:
            if path == "/api/tree":
                return self.reply(index.tree(q.get("path", "")))
            if path == "/api/under":       # a folder's pages, its subfolders' too (the Redesign column's box for all of them)
                return self.reply({"urls": index.under(q.get("path", ""))})
            if path == "/api/search":
                return self.reply({"results": index.search(q.get("q", ""))})
            if path == "/api/options":
                o = index.options()
                section = o["section"] and {**o["section"], "pages": page_list(index, o["section"]["pages"])}
                return self.reply({"around": page_list(index, o["around"]), "section": section, "most": SECTION})
        except (ValueError, TypeError, AttributeError, KeyError, OSError) as e:   # a page the index can't handle: say so, don't drop the line
            return self.reply({"error": f"{type(e).__name__}: {e}"}, 500)
        super().do_GET()

    def do_HEAD(self):
        if not self.trusted() or not self.admitted({}, self.path.startswith("/api/")):
            return self.send_error(403)
        if self.hidden():
            return self.send_error(404)
        super().do_HEAD()

    def do_POST(self):
        app = self.server.app
        self.grant = False                 # no cookie on answers to POSTs
        given = self.headers.get("X-Key" if self.path == "/api/join" else "X-Token") or ""
        allowed = secrets.compare_digest(given.encode(), (app["key"] if self.path == "/api/join" else app["token"]).encode())
        if not self.trusted() or not allowed:
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            project, ds, variant = app["project"], body.get("ds"), body.get("variant")
            with LOCK:
                if self.path in ("/api/discover", "/api/code", "/api/start") and mv.settings(project).get("started"):   # the site was chosen: its index stays
                    raise ValueError("this project has already started")
                if self.path == "/api/discover":
                    url = str(body.get("url") or "").strip()
                    if not re.match(r"(https?://)?([\w-]+(\.[\w-]+)+|localhost\b|\[[\da-f:]+\])", url, re.I):   # a host with a dot, or a local one
                        error = "Enter the address of a web page, e.g. example.com/about."
                        app["index"].forget(error)
                        mv.change_settings(project, {"url": ""})   # no other tab looks the old one up again
                        raise ValueError(error)
                    result = app["index"].discover(url)
                    if result:                   # the address of the Start screen (as --url sets it); one the user typed replaces the site's code
                        mv.change_settings(project, {"url": app["index"].asked, **({"code": None} if body.get("typed") else {})})
                elif self.path == "/api/code":   # the site's code instead of an address: Claude starts it here and hands over its local address
                    source = str(body.get("source") or "").strip()
                    if not source or len(source) > 500:
                        raise ValueError("Enter a URL, folder path or Git repository.")
                    if re.match(r"\.\.?(/|$)", source):   # relative to what? The app and Claude may run in different folders
                        raise ValueError("Enter the folder's full path, starting with / or ~.")
                    if re.match(r"[~/]", source) and not Path(os.path.expanduser(source)).is_dir():
                        raise ValueError(f"There is no folder at {source}.")
                    if app["index"].busy:      # an address being looked up would fill the list with its site
                        raise ValueError("Still reading the previous address. Try again in a moment.")
                    app["index"].forget("")       # nothing of an earlier site while this one starts
                    mv.change_settings(project, {"code": source, "url": ""})
                    result = source
                elif self.path == "/api/explore":
                    result = app["index"].explore(str(body.get("path") or ""))
                elif self.path == "/api/start" and body.get("scratch"):   # no site: a blank page with an empty report, ready to design
                    renamed = rename_placeholder(app, "scratch")
                    mv.from_scratch(app["project"], str(body.get("notes") or "").strip(), renamed and PLACEHOLDER)
                    result = str(app["project"])
                elif self.path == "/api/start":
                    pages = mv.merge(body.get("pages") or [])
                    if not pages or len(pages) > MAX_START:
                        raise ValueError(f"choose between 1 and {MAX_START} pages")
                    url = canonical(str(body.get("url") or pages[0]))
                    site = app["index"].home or app["index"].start   # (a failed lookup keeps the user's page)
                    if not site or any(not is_same_domain(u, site) for u in pages + [url]):   # (a new lookup clears home)
                        raise ValueError("the pages must be from the site that was looked up")
                    code = mv.settings(project).get("code")   # a site started from its code is named after its folder or repository
                    name = code_name(code) if code else (urlparse(url).hostname or "").removeprefix("www.")
                    renamed = rename_placeholder(app, name)
                    mv.change_settings(app["project"], {"url": url, "notes": str(body.get("notes") or "").strip(), "renamed_from": PLACEHOLDER if renamed else None,
                                                        "redesign": pages, "requested": [], "started": now()})   # requested: the GUI sends the first page
                    result = str(app["project"])
                elif self.path == "/api/pages":
                    asked = body.get("request") or []   # a page sent to be analysed goes on the list too (the user can take it off again)

                    def change(opts):
                        opts["redesign"] = mv.merge(opts.get("redesign", []), (body.get("add") or []) + asked, body.get("remove") or [])
                        before = canonical(str(body.get("before") or ""))
                        if body.get("add") and before in opts["redesign"]:   # Undo: back before the page that followed it
                            url = canonical(body["add"][0])
                            opts["redesign"].remove(url)
                            opts["redesign"].insert(opts["redesign"].index(before), url)
                        opts["requested"] = mv.merge(opts.get("requested", []), asked)
                    result = len(mv.change_settings(project, change)["redesign"])
                    mv.retry(project, [canonical(str(u)) for u in asked])
                elif self.path == "/api/asked":   # sent to Claude: shown as being worked on in every tab, {key: null} when done
                    def change(opts):
                        asked = opts.get("asked") if isinstance(opts.get("asked"), dict) else {}
                        for key, what in (body.get("asked") or {}).items():
                            if what is None:
                                asked.pop(key, None)
                            else:
                                asked[key] = {**(what if isinstance(what, dict) else {}), "at": now()}
                        opts["asked"] = asked
                    result = len(mv.change_settings(project, change)["asked"])
                elif self.path == "/api/hosts":   # the user's answer: let the copies load from these sites, or keep them blocked
                    names = lambda k: [str(h) for h in body.get(k) or [] if isinstance(h, str)]  # noqa: E731
                    result = mv.answer_hosts(project, names("allow"), names("block"))
                elif self.path == "/api/duplicate":
                    result = mv.duplicate(project, ds, variant)
                elif self.path == "/api/choose":
                    result = mv.choose(project, ds, variant)
                elif self.path == "/api/rename":     # a design, or a page (url): the user's own name
                    if body.get("url"):
                        url, label = canonical(str(body["url"])), clean_label(body.get("label"))
                        names = lambda o: {**{u: n for u, n in (o.get("names") or {}).items() if u != url}, **({url: label} if label else {})}  # noqa: E731  (empty: the title again)
                        app["index"].names = mv.change_settings(project, lambda o: o.update(names=names(o)))["names"]
                        result = label
                    else:
                        result = mv.rename(project, ds, variant, body.get("label"))
                elif self.path == "/api/discard":
                    result = mv.discard(project, ds, variant)
                elif self.path == "/api/export":
                    result = mv.export(project)
                elif self.path == "/api/media":   # download what a copy left out: one (src) or all of a page but its audio
                    slug, src = str(body.get("page") or ""), body.get("src")
                    found = [m for m in mv.media(project, [slug]).get(slug, []) if m["kind"] != "audio" and (not src or m["src"] == src)]
                    if not found:
                        raise ValueError("nothing to download there")
                    srcs = list(dict.fromkeys(m["src"] for m in found))
                    for m in found:
                        if m["src"] not in app["media"] or app["media"][m["src"]].get("error"):   # (again after a failure)
                            app["media"][m["src"]] = {"page": slug, "done": 0, "total": m["bytes"], "error": ""}
                    threading.Thread(target=download, args=(app, srcs), daemon=True).start()
                    result = len(srcs)
                elif self.path == "/api/join":
                    result = app["sessions"].join(body)
                elif self.path == "/api/session":
                    result = app["sessions"].choose(body.get("id"))
                elif self.path == "/api/claude":
                    text = str(body.get("text") or "").strip()
                    if not text:
                        raise ValueError("empty message")
                    return self.reply({"sent": app["sessions"].send(text)})
                else:
                    return self.send_error(404)
            self.reply({"result": result})
        except (ValueError, TypeError, AttributeError, KeyError, OSError, RecursionError) as e:
            self.reply({"error": str(e)}, 400)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # else: the browser dropped a request it no longer needs
            super().handle_error(request, client_address)


def port_answers(port):
    """Whether something already listens on this port."""
    try:
        socket.create_connection(("127.0.0.1", port), 0.2).close()
        return True
    except OSError:
        return False


def serve(project, record, port=None):
    """The app itself (runs in the background, started by main())."""
    # Same port per project (bookmarks, open tabs and saved notes keep working): the
    # one it ran on before, else one derived from the folder; the next free one if taken.
    preferred = 8700 + zlib.crc32(str(project).encode()) % 100
    for port in ([port] if port else []) + list(range(preferred, preferred + 50)):
        if port_answers(port):   # macOS lets us bind next to a server on *:port, so ask first whether one answers
            continue
        try:
            server = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"Error: no free port between {preferred} and {preferred + 49}.")
    # new secrets for every run: one that leaked (a tab still asking a stopped app's port) dies with the run
    server.app = {"project": project, "token": secrets.token_urlsafe(24), "view": secrets.token_urlsafe(24),
                  "key": secrets.token_urlsafe(24), "sessions": Sessions(project), "index": SiteIndex(project),
                  "seen": time.time(), "media": {}}   # media: downloads the user asked for (src -> progress)
    # the key lets a session join: only this user may read it
    write_json(record, {"pid": os.getpid(), "port": port, "key": server.app["key"], "token": server.app["token"], "rev": CODE_REV}, 0o600)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def stop_when_idle():
        while True:
            time.sleep(30)
            s = server.app["sessions"]
            if time.time() - server.app["seen"] > IDLE and not any(s.running(x) for x in s.list):
                return server.shutdown()
    threading.Thread(target=stop_when_idle, daemon=True).start()
    server.serve_forever()   # the record stays: the next start keeps its port (a dead pid marks it stopped)


def running_app(project, record):
    """The app already running for this project, if it confirms it is ours and current."""
    rec = read_dict(record)
    if not rec.get("port"):
        return None
    nonce = secrets.token_hex(16)   # whoever answers on the port must prove it holds the key (the key is never sent)
    try:
        with urlopen(f"http://127.0.0.1:{rec['port']}/api/ping?nonce={nonce}", timeout=10) as r:   # a busy app (an export) may take a moment
            info = json.load(r)
    except (OSError, ValueError):
        return None
    proof = hmac.new(str(rec.get("key")).encode(), nonce.encode(), "sha256").hexdigest()
    if not isinstance(info, dict) or not hmac.compare_digest(str(info.get("proof")).encode(), proof.encode()) or info.get("pid") != rec.get("pid"):
        return None
    if rec.get("rev") != CODE_REV:  # older code: replace it (its sessions join again when they reopen it)
        os.kill(rec["pid"], signal.SIGTERM)
        for _ in range(30):              # until it has let go of its port (the new one takes it again)
            time.sleep(0.1)
            try:
                os.kill(rec["pid"], 0)
            except OSError:
                break
        return None
    return rec


def start_app(project, record, port=None):
    args = ["--port", str(port)] if port else []
    with open(project / ".app.log", "a") as log:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--project-dir", str(project), "--serve", *args],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        time.sleep(0.1)
        rec = read_dict(record)
        if rec.get("pid") == proc.pid:
            return rec
        if proc.poll() is not None:
            break
    sys.exit(f"Error: the app did not start; see {project / '.app.log'}")


def main():
    parser = argparse.ArgumentParser(description="Open the Waib app for one project")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--url", help="The page to redesign: fills in the Start screen of a new project")
    parser.add_argument("--code", help="The folder or Git repository of the site's code (a new project): the Start screen waits for its --url")
    parser.add_argument("--no-open", action="store_true", help="Don't open the browser")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)   # (--serve: the port it ran on before)
    args = parser.parse_args()
    project = Path(args.project_dir).resolve()
    if not project.is_dir():
        sys.exit(f"Error: {project} is not a directory.")
    record = project / ".app.json"
    if args.serve:
        return serve(project, record, args.port)
    opts = mv.settings(project)
    if (args.code or args.url) and not opts.get("started"):   # as the Start screen's field does
        mv.change_settings(project, {**({"code": args.code.strip(), "url": ""} if args.code else {}), **({"url": canonical(with_scheme(args.url))} if args.url else {})})
    if project.name == PLACEHOLDER and not opts.get("started") and not args.url:   # Start renames the folder: an old $OUT starts afresh
        moved = sorted((d for d in project.parent.glob("waib-*") if d.is_dir() and d != project and mv.settings(d).get("renamed_from") == PLACEHOLDER),
                       key=lambda d: (d / "project.json").stat().st_mtime, reverse=True)
        if moved:
            print(f"Note: Start renamed an earlier project to {moved[0].name}. To continue that one, run this with --project-dir {moved[0]}", file=sys.stderr)

    before = read_dict(record)
    rec = running_app(project, record)
    started = rec is None
    if started:
        rec = start_app(project, record, before.get("port"))
    url = f"http://127.0.0.1:{rec['port']}/"
    me = this_session()
    if me:
        req = Request(url + "api/join", data=json.dumps(me).encode(),
                      headers={"X-Key": rec["key"], "Content-Type": "application/json"})
        try:
            urlopen(req, timeout=15).close()
        except OSError as e:            # busy (a long export): the link still works, the user picks the session in the app
            print(f"Note: could not link this session yet ({e}); choose it in the app.", file=sys.stderr)
            me = None
    linked = "linked to this Claude Code session" if me \
        else "no Claude Code session found: requests will be copied for pasting"
    link = f"{url}?k={rec['token']}"    # the GUI keeps the token; the browser also gets a files-only cookie
    print(f"App: {link}  ({linked})", flush=True)
    if started and not args.no_open:
        webbrowser.open(link)


if __name__ == "__main__":
    main()

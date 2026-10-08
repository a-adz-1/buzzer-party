"""Admin web UI for Buzzer Party: name players and build quizzes from a phone.

Runs only while the game is in Admin mode. Every API call is handed to the
game thread (game.tasks) so nothing touches game state from the HTTP thread.
"""
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import buzzer_party as bp

WEB_DIR = os.path.join(bp.HERE, "web")
FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}\.json$")
MAX_QUESTIONS = 500


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- pack files

def pack_path(file):
    if not isinstance(file, str) or not FILE_RE.match(file):
        raise ApiError("bad quiz file name")
    return os.path.join(bp.PACK_DIR, file)


def list_pack_files():
    out = []
    for path in sorted(os.listdir(bp.PACK_DIR)) if os.path.isdir(bp.PACK_DIR) else []:
        if not FILE_RE.match(path):
            continue
        try:
            with open(os.path.join(bp.PACK_DIR, path), encoding="utf-8") as f:
                d = json.load(f)
            out.append({"file": path, "name": d.get("name", path), "description": d.get("description", ""),
                        "kids": bool(d.get("kids")), "count": len(d.get("questions", []))})
        except Exception as e:
            out.append({"file": path, "name": path, "description": f"(unreadable: {e})", "kids": False, "count": 0})
    return out


def clean_str(v, limit, field, required=True):
    v = " ".join(str(v or "").split())
    if required and not v:
        raise ApiError(f"{field} can't be empty")
    if len(v) > limit:
        raise ApiError(f"{field} is too long (max {limit} characters)")
    return v


def validate_pack(d):
    if not isinstance(d, dict):
        raise ApiError("bad request")
    qs = d.get("questions") or []
    if not isinstance(qs, list) or len(qs) > MAX_QUESTIONS:
        raise ApiError("questions must be a list (max %d)" % MAX_QUESTIONS)
    out_q = []
    for n, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            raise ApiError(f"question {n} is malformed")
        answers = q.get("answers")
        if not isinstance(answers, list) or len(answers) != 4:
            raise ApiError(f"question {n} needs exactly 4 answers")
        item = {"cat": clean_str(q.get("cat"), 30, f"Question {n} category", required=False),
                "q": clean_str(q.get("q"), 200, f"Question {n}"),
                "answers": [clean_str(a, 80, f"Question {n} answer {i + 1}") for i, a in enumerate(answers)]}
        if len({a.lower() for a in item["answers"]}) < 4:
            raise ApiError(f"question {n} has duplicate answers")
        if not item["cat"]:
            del item["cat"]
        out_q.append(item)
    return {"name": clean_str(d.get("name"), 60, "Quiz name"),
            "description": clean_str(d.get("description"), 200, "Description", required=False),
            "kids": bool(d.get("kids")),
            "questions": out_q}


def new_file_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "quiz"
    file, n = f"{slug}.json", 2
    while os.path.exists(os.path.join(bp.PACK_DIR, file)):
        file, n = f"{slug}-{n}.json", n + 1
    return file


# --------------------------------------------------------------------------- server

class WebUI:
    def __init__(self, game, port=8080):
        self.game = game
        self.port = port
        self.httpd = None
        self.thread = None
        self.seen = {}  # client ip -> last seen

    @property
    def clients(self):
        now = time.time()
        return sum(1 for t in self.seen.values() if now - t < 10)

    def start(self):
        if self.httpd is None:
            handler = type("Handler", (Handler,), {"ui": self})
            ThreadingHTTPServer.allow_reuse_address = True
            self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
            self.httpd.daemon_threads = True
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
        return f"http://{lan_ip()}:{self.port}"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self.seen = {}

    def on_game_thread(self, fn, timeout=3.0):
        """Run fn on the game thread and wait for its result."""
        box, done = {}, threading.Event()

        def task():
            try:
                box["ok"] = fn()
            except Exception as e:
                box["err"] = e
            done.set()

        self.game.tasks.put(task)
        if not done.wait(timeout):
            raise ApiError("game is busy, try again", 503)
        if "err" in box:
            raise box["err"]
        return box["ok"]

    # ----- API (all run on the game thread)
    def api_state(self):
        g = self.game
        return {"slots": g.slots(), "packs": list_pack_files(), "state": g.state}

    def api_name(self, body):
        label = clean_str(body.get("label"), 40, "Slot")
        name = " ".join(str(body.get("name") or "").split())[:bp.MAX_NAME]
        self.game.set_slot_name(label, name)
        return {"label": label, "name": name}

    def api_get_pack(self, file):
        path = pack_path(file)
        if not os.path.exists(path):
            raise ApiError("quiz not found", 404)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["file"] = file
        return d

    def api_save_pack(self, body):
        pack = validate_pack(body)
        file = body.get("file") or new_file_name(pack["name"])
        path = pack_path(file)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pack, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        self.game.reload_packs()
        return {"file": file, "count": len(pack["questions"])}

    def api_delete_pack(self, body):
        path = pack_path(body.get("file"))
        if not os.path.exists(path):
            raise ApiError("quiz not found", 404)
        os.remove(path)
        self.game.reload_packs()
        return {"deleted": body.get("file")}


class Handler(BaseHTTPRequestHandler):
    ui = None  # set per server

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ApiError("request too large", 413)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError("invalid JSON")

    def handle_api(self, fn):
        self.ui.seen[self.client_address[0]] = time.time()
        try:
            self.send_json(fn())
        except ApiError as e:
            self.send_json({"error": str(e)}, e.code)
        except Exception as e:
            self.send_json({"error": f"server error: {e}"}, 500)

    def do_GET(self):
        url = urlparse(self.path)
        ui = self.ui
        if url.path == "/api/state":
            return self.handle_api(lambda: ui.on_game_thread(ui.api_state))
        if url.path == "/api/pack":
            file = parse_qs(url.query).get("file", [""])[0]
            return self.handle_api(lambda: ui.on_game_thread(lambda: ui.api_get_pack(file)))
        if url.path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        ui = self.ui
        routes = {"/api/name": ui.api_name, "/api/pack": ui.api_save_pack, "/api/pack/delete": ui.api_delete_pack}
        fn = routes.get(urlparse(self.path).path)
        if not fn:
            self.send_error(404)
            return

        def run():
            body = self.body()
            return ui.on_game_thread(lambda: fn(body))
        self.handle_api(run)

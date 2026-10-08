#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Adam (a-adz-1) - Buzzer Party, https://github.com/a-adz-1/buzzer-party
"""Buzzer Party - a party quiz for PlayStation Buzz! buzzers on SteamOS / Linux.

Inputs: real Buzz buzzers (wired or wireless dongle, up to 2 sets = 8 players),
any gamepad (incl. the Steam Deck itself), and the keyboard for testing.
LEDs on the buzzers are driven through /dev/hidraw* when permissions allow.
"""
import argparse
import array
import glob
import json
import math
import os
import queue
import random

import pygame

W, H = 1280, 720
HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.join(HERE, "questions")
CONFIG_PATH = os.path.join(HERE, "config.json")
NAMES_PATH = os.path.join(HERE, "names.json")
MAX_NAME = 12

COL = {
    "blue": (45, 115, 255),
    "orange": (255, 140, 30),
    "green": (40, 200, 80),
    "yellow": (255, 210, 25),
    "red": (230, 40, 50),
}
ANSWER_COLOURS = ["blue", "orange", "green", "yellow"]  # top-to-bottom, as on the buzzer
# Per-buzzer button order in the HID report (buttons 0-4 = buzzer 1, 5-9 = buzzer 2 ...).
DEFAULT_BUZZ_ORDER = ["red", "yellow", "green", "orange", "blue"]
# Xbox-style layout (what Steam Input presents): A, B, X, Y, LB, RB
PAD_MAP = {0: "green", 1: "orange", 2: "blue", 3: "yellow", 4: "red", 5: "red"}
PLAYER_TINTS = [
    (255, 90, 160), (70, 210, 255), (170, 240, 70), (190, 120, 255),
    (255, 170, 60), (60, 230, 180), (255, 240, 110), (255, 120, 110),
]
WHITE = (255, 255, 255)
DIM = (150, 140, 190)
PANEL = (35, 25, 70)

DEFAULT_CONFIG = {
    "buzz_button_order": DEFAULT_BUZZ_ORDER,
    "question_time": 15,
    "kids_question_time": 25,
    "fastest_finger_every": 5,
    "web_port": 8080,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("config.json ignored:", e)
    return cfg


# --------------------------------------------------------------------------- packs

def load_packs():
    packs = []
    for path in sorted(glob.glob(os.path.join(PACK_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            qs = [q for q in data.get("questions", [])
                  if isinstance(q.get("answers"), list) and len(q["answers"]) == 4 and q.get("q")]
            if qs:
                packs.append({
                    "file": os.path.basename(path),
                    "name": data.get("name", os.path.basename(path)),
                    "description": data.get("description", ""),
                    "kids": bool(data.get("kids", False)),
                    "questions": qs,
                })
        except Exception as e:
            print(f"Skipping {path}: {e}")
    return packs


def load_names():
    try:
        with open(NAMES_PATH, encoding="utf-8") as f:
            return {str(k): str(v)[:MAX_NAME] for k, v in json.load(f).items()}
    except Exception:
        return {}


def save_names(names):
    try:
        with open(NAMES_PATH, "w", encoding="utf-8") as f:
            json.dump(names, f, indent=2)
    except OSError as e:
        print("Could not save names:", e)


def prepare_question(q):
    """First answer in the file is correct; shuffle for display."""
    order = list(range(4))
    random.shuffle(order)
    answers = [q["answers"][i] for i in order]
    return {"q": q["q"], "cat": q.get("cat", ""), "answers": answers, "correct": order.index(0)}


# --------------------------------------------------------------------------- sound

class Sounds:
    def __init__(self):
        self.ok = False
        try:
            pygame.mixer.init(44100, -16, 2, 512)
            self.ok = True
        except Exception as e:
            print("No audio:", e)
            return
        self.s = {
            "join": self.seq([([523], .08), ([784], .14)]),
            "lock": self.seq([([880], .06)], vol=.2),
            "tick": self.seq([([1200], .025)], vol=.12),
            "go": self.seq([([392, 523], .12)], vol=.25),
            "correct": self.seq([([523], .09), ([659], .09), ([784], .22)]),
            "wrong": self.seq([([140, 147], .45)], vol=.3),
            "buzz": self.seq([([220, 330], .25)], vol=.3),
            "move": self.seq([([660], .04)], vol=.15),
            "fanfare": self.seq([([523], .12), ([523], .12), ([523], .12), ([698, 880], .5)]),
        }

    @staticmethod
    def seq(parts, vol=.3, sr=44100):
        buf = array.array("h")
        for freqs, dur in parts:
            n = int(sr * dur)
            for i in range(n):
                t = i / sr
                env = min(1.0, i / 150) * (1 - i / n) ** 1.2
                s = 0.0
                for f in freqs:
                    s += 0.6 * (1 if (t * f) % 1 < .5 else -1) + 0.4 * math.sin(2 * math.pi * f * t)
                v = int(max(-1, min(1, s / len(freqs) * env * vol)) * 32767)
                buf.append(v)
                buf.append(v)
        return pygame.mixer.Sound(buffer=buf.tobytes())

    def play(self, name):
        if self.ok and name in self.s:
            self.s[name].play()


# --------------------------------------------------------------------------- buzzer LEDs

class BuzzLeds:
    """Writes LED reports to Buzz dongles via hidraw: 00 00 L1 L2 L3 L4 00 00."""

    def __init__(self):
        self.fds = []
        self.state = {}
        self.scan()

    def scan(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds, self.state = [], {}
        paths = glob.glob("/sys/class/hidraw/hidraw*")
        paths.sort(key=lambda p: int(p.rsplit("hidraw", 1)[1] or 0))
        for p in paths:
            try:
                with open(os.path.join(p, "device", "uevent")) as f:
                    ue = f.read().lower()
            except OSError:
                continue
            if "buzz" in ue or "0000054c:00000002" in ue or "0000054c:00001000" in ue:
                try:
                    self.fds.append(os.open("/dev/" + os.path.basename(p), os.O_WRONLY | os.O_NONBLOCK))
                except OSError as e:
                    print(f"Buzz LEDs unavailable on {p} ({e}); see README for the udev rule")

    @property
    def available(self):
        return bool(self.fds)

    def set(self, dev, leds):
        if dev >= len(self.fds):
            return
        t = tuple(bool(x) for x in leds[:4])
        if self.state.get(dev) == t:
            return
        self.state[dev] = t
        try:
            os.write(self.fds[dev], bytes([0, 0] + [0xFF if x else 0 for x in t] + [0, 0]))
        except OSError:
            pass

    def off(self):
        for d in range(len(self.fds)):
            self.set(d, [0, 0, 0, 0])


# --------------------------------------------------------------------------- input

KEYMAP = {}
for _row, _keys in enumerate(["12345", "qwert", "asdfg", "zxcvb"]):
    for _k, _c in zip(_keys, ["blue", "orange", "green", "yellow", "red"]):
        KEYMAP[getattr(pygame, "K_" + _k)] = (_row, _c)


class Inputs:
    """Turns pygame events into (source, colour) presses or system actions."""

    def __init__(self, buzz_order):
        self.order = buzz_order
        self.buzz = {}   # instance_id -> Joystick
        self.pads = {}   # instance_id -> Joystick

    def buzz_ordinal(self, iid):
        ids = sorted(self.buzz)
        return ids.index(iid) if iid in ids else 0

    def label(self, src):
        kind = src[0]
        if kind == "buzz":
            return f"BUZZER {src[2] + 1}" + (f" / SET {self.buzz_ordinal(src[1]) + 1}" if len(self.buzz) > 1 else "")
        if kind == "pad":
            ids = sorted(self.pads)
            return f"GAMEPAD {ids.index(src[1]) + 1 if src[1] in ids else 1}"
        return f"KEYS {src[1] + 1}"

    def handle(self, ev):
        """Returns ("press", src, colour) | ("sys", action) | ("devices",) | None"""
        if ev.type == pygame.JOYDEVICEADDED:
            js = pygame.joystick.Joystick(ev.device_index)
            name = js.get_name().lower()
            if "buzz" in name or (js.get_numbuttons() == 20 and js.get_numaxes() == 0):
                self.buzz[js.get_instance_id()] = js
            else:
                self.pads[js.get_instance_id()] = js
            print(f"Device added: {js.get_name()} ({js.get_numbuttons()} buttons)")
            return ("devices",)
        if ev.type == pygame.JOYDEVICEREMOVED:
            self.buzz.pop(ev.instance_id, None)
            self.pads.pop(ev.instance_id, None)
            return ("devices",)
        if ev.type == pygame.JOYBUTTONDOWN:
            if ev.instance_id in self.buzz:
                if ev.button < 20:
                    sub, b = divmod(ev.button, 5)
                    return ("press", ("buzz", ev.instance_id, sub), self.order[b])
            elif ev.instance_id in self.pads:
                if ev.button in PAD_MAP:
                    return ("press", ("pad", ev.instance_id, 0), PAD_MAP[ev.button])
                if ev.button in (6, 7):  # back / start
                    return ("sys", "back" if ev.button == 6 else "start")
            return None
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_ESCAPE:
                return ("sys", "back")
            if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                return ("sys", "start")
            if ev.key == pygame.K_F11:
                return ("sys", "fullscreen")
            if ev.key in KEYMAP:
                row, c = KEYMAP[ev.key]
                return ("press", ("kb", row, 0), c)
        if ev.type == pygame.QUIT:
            return ("sys", "quit")
        return None


# --------------------------------------------------------------------------- drawing helpers

_fonts = {}


def font(size):
    if size not in _fonts:
        _fonts[size] = pygame.font.Font(None, size)
    return _fonts[size]


def text(surf, s, size, pos, colour=WHITE, anchor="center", shadow=True):
    f = font(size)
    img = f.render(str(s), True, colour)
    r = img.get_rect(**{anchor: pos})
    if shadow:
        sh = f.render(str(s), True, (10, 5, 25))
        surf.blit(sh, r.move(2, 3))
    surf.blit(img, r)
    return r


def wrap(s, size, width):
    f = font(size)
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.size(trial)[0] <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_wrapped(s, width, max_lines, sizes=(64, 56, 48, 42, 36, 30)):
    for size in sizes:
        lines = wrap(s, size, width)
        if len(lines) <= max_lines:
            return size, lines
    return sizes[-1], wrap(s, sizes[-1], width)


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def rrect(surf, colour, rect, radius=18, width=0):
    pygame.draw.rect(surf, colour, rect, width, border_radius=radius)


def buzzer_dot(surf, colour, center, r):
    pygame.draw.circle(surf, shade(COL[colour], .55), (center[0], center[1] + 3), r)
    pygame.draw.circle(surf, COL[colour], center, r)
    pygame.draw.circle(surf, shade(COL[colour], 1.35), (center[0] - r // 3, center[1] - r // 3), max(2, r // 3))


# --------------------------------------------------------------------------- game

class Player:
    def __init__(self, src, idx, label, name=None):
        self.src, self.idx, self.label = src, idx, label
        self.name = name or f"P{idx + 1}"
        self.tint = PLAYER_TINTS[idx % len(PLAYER_TINTS)]
        self.score = 0
        self.shown = 0.0
        self.delta = 0


class Game:
    def __init__(self, cfg, sounds=None, leds=None, inputs=None):
        self.cfg = cfg
        self.snd = sounds or Sounds()
        self.leds = leds or BuzzLeds()
        self.inputs = inputs or Inputs(cfg["buzz_button_order"])
        self.packs = load_packs()
        self.names = load_names()
        self.tasks = queue.Queue()   # callables queued by the web server, run on the game thread
        self.web = None              # WebUI, only running in admin mode
        self.web_url = None
        self.qr_matrix = None
        self.qr_cache = {}
        self.menu_i = 0
        self.pick_i = 0
        self.admin_flash = {}        # slot label -> time last pressed
        self.pack_i = 0
        self.lengths = [10, 15, 20, 30]
        self.length_i = 0
        self.players = []
        self.t = 0.0
        self.last_press = ""
        self.quit = False
        self.particles = []
        self.bg = self.make_bg()
        self.blobs = [(random.uniform(0, W), random.uniform(0, H), random.uniform(120, 260),
                       random.uniform(.05, .2), random.uniform(0, 6.28)) for _ in range(7)]
        self.set_state("menu")

    # ----- web server hooks (run on the game thread via self.tasks)
    def run_tasks(self):
        while True:
            try:
                fn = self.tasks.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as e:
                print("web task failed:", e)

    def slots(self):
        """Every input slot we know about, with its remembered name."""
        labels = []
        for iid in sorted(self.inputs.buzz):
            for sub in range(4):
                labels.append(("buzz", self.inputs.label(("buzz", iid, sub))))
        for iid in sorted(self.inputs.pads):
            labels.append(("pad", self.inputs.label(("pad", iid, 0))))
        for row in range(4):
            labels.append(("keys", f"KEYS {row + 1}"))
        seen = {l for _, l in labels}
        labels += [("saved", l) for l in sorted(self.names) if l not in seen]
        joined = {p.label: p for p in self.players}
        return [{"label": l, "kind": k, "name": self.names.get(l, ""),
                 "joined": l in joined,
                 "flash": self.t - self.admin_flash.get(l, -99) < 1.5}
                for k, l in labels]

    def set_slot_name(self, label, name):
        name = " ".join(str(name).split())[:MAX_NAME]
        if name:
            self.names[label] = name
        else:
            self.names.pop(label, None)
        save_names(self.names)
        for p in self.players:
            if p.label == label:
                p.name = name or f"P{p.idx + 1}"

    def reload_packs(self):
        current = self.pack["file"] if self.pack else None
        self.packs = load_packs()
        files = [p["file"] for p in self.packs]
        self.pack_i = files.index(current) if current in files else 0
        self.pick_i = min(self.pick_i, max(0, len(self.packs) - 1))

    # ----- admin mode / web UI
    def enter_admin(self):
        self.set_state("admin")
        if self.web is None:
            try:
                import webui
                self.web = webui.WebUI(self, int(self.cfg.get("web_port", 8080)))
            except Exception as e:
                self.web_error = str(e)
                print("Web UI failed:", e)
                return
        try:
            self.web_url = self.web.start()
            self.web_error = None
        except OSError as e:
            self.web_url, self.web_error = None, f"Could not start web server: {e}"
            return
        self.qr_matrix = None
        try:
            import qrcode
            q = qrcode.QRCode(border=2, box_size=1)
            q.add_data(self.web_url)
            q.make(fit=True)
            self.qr_matrix = q.get_matrix()
        except Exception as e:
            print("QR code unavailable:", e)

    def leave_admin(self):
        if self.web:
            self.web.stop()
        self.reload_packs()
        self.set_state("menu")

    def qr_surface(self, size):
        if not self.qr_matrix:
            return None
        if size not in self.qr_cache:
            m = self.qr_matrix
            n = len(m)
            img = pygame.Surface((n, n))
            img.fill(WHITE)
            for y, row in enumerate(m):
                for x, v in enumerate(row):
                    if v:
                        img.set_at((x, y), (0, 0, 0))
            k = max(1, size // n)
            self.qr_cache = {size: pygame.transform.scale(img, (n * k, n * k))}
        return self.qr_cache[size]

    # ----- helpers
    def make_bg(self):
        bg = pygame.Surface((W, H))
        for y in range(H):
            k = y / H
            c = (int(40 + 30 * k), int(10 + 10 * k), int(90 - 40 * k))
            pygame.draw.line(bg, c, (0, y), (W, y))
        return bg

    def player_for(self, src):
        for p in self.players:
            if p.src == src:
                return p
        return None

    @property
    def pack(self):
        return self.packs[self.pack_i] if self.packs else None

    def set_state(self, s):
        self.state, self.st = s, 0.0

    # ----- states
    def to_lobby(self):
        for p in self.players:
            p.score, p.shown, p.delta = 0, 0.0, 0
        self.set_state("lobby")

    def start_game(self):
        if not self.players or not self.pack:
            return
        pool = list(self.pack["questions"])
        random.shuffle(pool)
        n = min(self.lengths[self.length_i], len(pool))
        self.questions = [prepare_question(q) for q in pool[:n]]
        every = max(2, int(self.cfg["fastest_finger_every"]))
        for i, q in enumerate(self.questions):
            q["fastest"] = (i + 1) % every == 0
        self.kids = self.pack["kids"]
        self.qtime = float(self.cfg["kids_question_time" if self.kids else "question_time"])
        self.qi = -1
        for p in self.players:
            p.score, p.shown, p.delta = 0, 0.0, 0
        self.snd.play("fanfare")
        self.next_question()

    def next_question(self):
        self.qi += 1
        if self.qi >= len(self.questions):
            self.snd.play("fanfare")
            self.set_state("final")
            return
        self.q = self.questions[self.qi]
        self.answers = {}      # player idx -> (choice, time_taken)
        self.locked_out = set()
        self.winner = None     # fastest finger winner idx
        for p in self.players:
            p.delta = 0
        self.read_time = max(1.5, min(4.0, len(self.q["q"]) / 22))
        self.set_state("splash" if self.q["fastest"] else "read")

    def begin_answers(self):
        self.snd.play("go")
        self.last_tick = int(self.qtime)
        self.set_state("answer")

    def reveal(self):
        q = self.q
        any_right = False
        if q["fastest"]:
            right = [(t, i) for i, (c, t) in self.answers.items() if c == q["correct"]]
            if right:
                t, i = min(right)
                self.winner = i
                self.players[i].delta = 1000
            for i, (c, t) in self.answers.items():
                if c != q["correct"] and not self.kids:
                    self.players[i].delta = -250
        else:
            for i, (c, t) in self.answers.items():
                if c == q["correct"]:
                    remaining = max(0.0, self.qtime - t) / self.qtime
                    self.players[i].delta = int(round((200 + 800 * remaining) / 10) * 10)
        for p in self.players:
            p.score += p.delta
            any_right |= p.delta > 0
        self.snd.play("correct" if any_right else "wrong")
        if any_right:
            self.burst(W // 2, 300, 40)
        self.set_state("reveal")

    def after_reveal(self):
        last = self.qi + 1 >= len(self.questions)
        if not last and (self.qi + 1) % 5 == 0 and len(self.players) > 1:
            self.set_state("scores")
        else:
            self.next_question()

    # ----- input
    def on_press(self, src, colour):
        p = self.player_for(src)
        self.last_press = f"{self.inputs.label(src)} - {colour.upper()}"
        s = self.state
        if s == "menu":
            if colour in ("blue", "yellow"):
                self.menu_i = (self.menu_i + (-1 if colour == "blue" else 1)) % 3
                self.snd.play("move")
            elif colour in ("green", "red"):
                self.menu_select()
            return
        if s == "pick":
            if colour in ("blue", "yellow") and self.packs:
                self.pick_i = (self.pick_i + (-1 if colour == "blue" else 1)) % len(self.packs)
                self.snd.play("move")
            elif colour in ("green", "red") and self.packs:
                self.pack_i = self.pick_i
                self.snd.play("join")
                self.to_lobby()
            elif colour == "orange":
                self.set_state("menu")
            return
        if s == "admin":
            self.admin_flash[self.inputs.label(src)] = self.t
            if colour == "green":
                self.leave_admin()
            return
        if s == "lobby":
            if colour == "red" and p is None and len(self.players) < 8:
                label = self.inputs.label(src)
                self.players.append(Player(src, len(self.players), label, self.names.get(label)))
                self.snd.play("join")
                self.burst(W // 2, 560, 15)
            elif p is not None and self.packs:
                if colour == "blue":
                    self.pick_i = self.pack_i
                    self.set_state("pick")
                    self.snd.play("move")
                elif colour == "orange":
                    self.length_i = (self.length_i + 1) % len(self.lengths)
                    self.snd.play("move")
                elif colour == "green":
                    self.start_game()
            return
        if p is None:
            return
        if s == "answer" and colour in ANSWER_COLOURS and p.idx not in self.answers:
            self.answers[p.idx] = (ANSWER_COLOURS.index(colour), self.st)
            self.snd.play("lock")
            if len(self.answers) == len(self.players):
                self.reveal()
            elif self.q["fastest"] and ANSWER_COLOURS.index(colour) == self.q["correct"]:
                self.reveal()  # first correct answer ends a fastest-finger question
        elif s == "answer" and colour == "red":
            self.snd.play("buzz")
        elif s in ("reveal", "scores") and colour == "red" and self.st > 1.5:
            self.on_system("start")
        elif s == "final" and colour == "green" and self.st > 2:
            self.to_lobby()

    def menu_select(self):
        self.snd.play("join")
        if self.menu_i == 0:
            self.reload_packs()
            self.pick_i = self.pack_i
            self.set_state("pick")
        elif self.menu_i == 1:
            self.enter_admin()
        else:
            self.quit = True

    def on_system(self, action):
        s = self.state
        if action == "quit":
            self.quit = True
        elif action == "back":
            if s == "menu":
                self.quit = True
            elif s == "admin":
                self.leave_admin()
            elif s == "pick":
                self.set_state("menu")
            elif s == "lobby":
                self.players = []
                self.set_state("menu")
            else:
                self.to_lobby()
        elif action == "start":
            if s == "menu":
                self.menu_select()
            elif s == "pick" and self.packs:
                self.pack_i = self.pick_i
                self.to_lobby()
            elif s == "admin":
                self.leave_admin()
            elif s == "lobby":
                self.start_game()
            elif s == "reveal" and self.st > 1.0:
                self.after_reveal()
            elif s == "scores" and self.st > 1.0:
                self.next_question()
            elif s == "final" and self.st > 2:
                self.to_lobby()
            elif s in ("read", "splash"):
                self.st = 99

    # ----- update
    def update(self, dt):
        self.run_tasks()
        self.t += dt
        self.st += dt
        s = self.state
        if s == "splash" and self.st > 2.2:
            self.set_state("read")
        elif s == "read" and self.st > self.read_time:
            self.begin_answers()
        elif s == "answer":
            left = self.qtime - self.st
            if 0 < left <= 5 and int(left) < self.last_tick:
                self.last_tick = int(left)
                self.snd.play("tick")
            if left <= 0:
                self.reveal()
        elif s == "reveal" and self.st > 5.0:
            self.after_reveal()
        elif s == "scores" and self.st > 6.0:
            self.next_question()
        elif s == "final" and int(self.st * 4) != int((self.st - dt) * 4):
            self.burst(random.randint(100, W - 100), random.randint(80, 300), 12)
        for p in self.players:
            p.shown += (p.score - p.shown) * min(1, dt * 5)
        for pt in self.particles:
            pt[0] += pt[2] * dt
            pt[1] += pt[3] * dt
            pt[3] += 600 * dt
            pt[4] -= dt
        self.particles = [pt for pt in self.particles if pt[4] > 0]
        self.update_leds()

    def led_on(self, p):
        s = self.state
        if s in ("lobby", "pick"):
            return True
        if s == "answer":
            return p.idx not in self.answers
        if s == "reveal":
            return p.delta > 0 and int(self.st * 6) % 2 == 0
        if s == "final":
            top = max(x.score for x in self.players)
            return p.score == top and int(self.st * 5) % 2 == 0
        if s == "scores":
            return p.score == max(x.score for x in self.players)
        return False

    def update_leds(self):
        if not self.leds.available:
            return
        want = {}
        for p in self.players:
            if p.src[0] == "buzz":
                dev = self.inputs.buzz_ordinal(p.src[1])
                want.setdefault(dev, [0, 0, 0, 0])[p.src[2]] = self.led_on(p)
        if self.state in ("menu", "admin"):  # all buzzers chase in the menus
            for iid in self.inputs.buzz:
                dev = self.inputs.buzz_ordinal(iid)
                want[dev] = [int(self.t * 4) % 4 == i for i in range(4)]
                if self.state == "admin":  # light the buzzer that was just pressed
                    for sub in range(4):
                        if self.t - self.admin_flash.get(self.inputs.label(("buzz", iid, sub)), -99) < 1.5:
                            want[dev] = [i == sub for i in range(4)]
        if self.state == "lobby":  # gently pulse unjoined buzzers to invite a press
            pulse = int(self.t * 2) % 2 == 0
            for iid in self.inputs.buzz:
                dev = self.inputs.buzz_ordinal(iid)
                leds = want.setdefault(dev, [0, 0, 0, 0])
                for sub in range(4):
                    if not self.player_for(("buzz", iid, sub)):
                        leds[sub] = pulse
        for dev in range(len(self.leds.fds)):
            self.leds.set(dev, want.get(dev, [0, 0, 0, 0]))

    def burst(self, x, y, n):
        for _ in range(n):
            a = random.uniform(0, math.tau)
            v = random.uniform(150, 520)
            self.particles.append([x, y, math.cos(a) * v, math.sin(a) * v - 250,
                                   random.uniform(.8, 1.8), random.choice(list(COL.values())),
                                   random.randint(4, 9)])

    # ----- draw
    def draw(self, surf):
        surf.blit(self.bg, (0, 0))
        glow = pygame.Surface((W, H), pygame.SRCALPHA)
        for i, (bx, by, r, sp, ph) in enumerate(self.blobs):
            x = bx + math.sin(self.t * sp + ph) * 120
            y = by + math.cos(self.t * sp * 1.3 + ph) * 80
            c = list(COL.values())[i % 5]
            pygame.draw.circle(glow, (*c, 22), (int(x), int(y)), int(r))
        surf.blit(glow, (0, 0))
        getattr(self, "draw_" + self.state)(surf)
        for x, y, vx, vy, life, c, sz in self.particles:
            pygame.draw.rect(surf, c, (int(x), int(y), sz, sz))

    def draw_header(self, surf, right=""):
        tag = "FASTEST FINGER" if self.q.get("fastest") else "POINT BUILDER"
        tag_col = COL["red"] if self.q.get("fastest") else COL["blue"]
        r = pygame.Rect(30, 18, font(30).size(tag)[0] + 30, 40)
        rrect(surf, tag_col, r, 20)
        text(surf, tag, 30, r.center, shadow=False)
        text(surf, f"QUESTION {self.qi + 1} / {len(self.questions)}", 32, (W // 2, 38), DIM)
        if self.q.get("cat"):
            text(surf, self.q["cat"].upper(), 30, (W - 30, 38), DIM, anchor="midright")

    def draw_question_text(self, surf, y=95, h=170):
        size, lines = fit_wrapped(self.q["q"], W - 160, 3)
        lh = font(size).get_linesize()
        top = y + (h - lh * len(lines)) // 2
        for i, line in enumerate(lines):
            text(surf, line, size, (W // 2, top + lh * i + lh // 2))

    def draw_answers(self, surf, reveal=False):
        x, w, hgt, gap, top = 140, W - 280, 66, 12, 280
        for i, (colour, ans) in enumerate(zip(ANSWER_COLOURS, self.q["answers"])):
            r = pygame.Rect(x, top + i * (hgt + gap), w, hgt)
            correct = i == self.q["correct"]
            if reveal and not correct:
                rrect(surf, (40, 30, 70), r, 33)
                fg = (110, 100, 140)
            elif reveal and correct:
                grow = r.inflate(16 + 6 * math.sin(self.st * 8), 10)
                rrect(surf, WHITE, grow, 38)
                rrect(surf, COL[colour], r, 33)
                fg = WHITE
            else:
                rrect(surf, shade(COL[colour], .45), r.move(0, 5), 33)
                rrect(surf, (25, 18, 55), r, 33)
                rrect(surf, COL[colour], r, 33, 4)
                fg = WHITE
            if reveal and correct:
                pygame.draw.circle(surf, WHITE, (r.x + 40, r.centery), 26)
            buzzer_dot(surf, colour, (r.x + 40, r.centery), 22 if not (reveal and not correct) else 16)
            size, _ = fit_wrapped(ans, w - 120, 1, sizes=(50, 44, 38, 32, 28))
            text(surf, ans, size, (r.x + 85, r.centery), fg, anchor="midleft")
            if reveal:  # who picked this
                pickers = [self.players[pi] for pi, (c, _) in self.answers.items() if c == i]
                for k, pl in enumerate(pickers):
                    cx = r.right - 30 - k * 46
                    pygame.draw.circle(surf, pl.tint, (cx, r.centery), 19)
                    text(surf, pl.name, 24, (cx, r.centery + 1), (20, 10, 40), shadow=False)

    def draw_players(self, surf, mode="score"):
        n = max(1, len(self.players))
        gap = 12
        pw = min(220, (W - 60 - gap * (n - 1)) // n)
        total = n * pw + (n - 1) * gap
        x0 = (W - total) // 2
        y = H - 112
        for p in self.players:
            r = pygame.Rect(x0 + p.idx * (pw + gap), y, pw, 96)
            active = mode == "answer" and p.idx not in self.answers
            lift = int(4 * math.sin(self.t * 6 + p.idx)) if active else 0
            r.y -= lift
            rrect(surf, shade(p.tint, .35), r.move(0, 5), 16)
            rrect(surf, PANEL, r, 16)
            rrect(surf, p.tint, r, 16, 4)
            score_s = f"{int(round(p.shown))}"
            room = pw - 34 - font(42).size(score_s)[0]
            nsize = 34
            while nsize > 20 and font(nsize).size(p.name)[0] > room:
                nsize -= 2
            text(surf, p.name, nsize, (r.x + 16, r.y + 26), p.tint, anchor="midleft")
            text(surf, score_s, 42, (r.right - 14, r.y + 28), anchor="midright")
            status_y = r.y + 70
            if mode == "answer":
                if p.idx in self.answers:
                    pill = pygame.Rect(0, 0, 110, 32)
                    pill.center = (r.centerx, status_y)
                    rrect(surf, WHITE, pill, 16)
                    text(surf, "LOCKED IN", 24, pill.center, (30, 20, 60), shadow=False)
                else:
                    dots = "." * (1 + int(self.t * 3) % 3)
                    text(surf, "thinking" + dots, 26, (r.centerx, status_y), DIM)
            elif mode == "reveal":
                if p.delta > 0:
                    lab = f"+{p.delta}" + ("  FASTEST!" if self.winner == p.idx else "")
                    text(surf, lab, 34, (r.centerx, status_y), COL["green"])
                elif p.delta < 0:
                    text(surf, f"{p.delta}", 34, (r.centerx, status_y), COL["red"])
                elif p.idx in self.answers:
                    text(surf, "wrong", 28, (r.centerx, status_y), DIM)
                else:
                    text(surf, "too slow!", 28, (r.centerx, status_y), DIM)
            else:
                text(surf, p.label, 22, (r.centerx, status_y), DIM)

    def draw_title(self, surf, size=150, y=110):
        bob = math.sin(self.t * 2.2) * 6
        w1, w2, gap = font(size).size("BUZZER")[0], font(size).size("PARTY")[0], size // 5
        x0 = (W - (w1 + gap + w2)) // 2
        text(surf, "BUZZER", size, (x0, y + bob), COL["yellow"], anchor="midleft")
        text(surf, "PARTY", size, (x0 + w1 + gap, y - bob), COL["red"], anchor="midleft")

    def draw_legend(self, surf, items, y):
        widths = [30 + font(30).size(lab)[0] for _, lab in items]
        lx = (W - sum(widths) - 40 * (len(items) - 1)) // 2 + 15
        for (c, lab), w in zip(items, widths):
            buzzer_dot(surf, c, (lx, y), 15)
            text(surf, lab, 30, (lx + 24, y + 1), anchor="midleft")
            lx += w + 40

    def device_status(self):
        nb = len(self.inputs.buzz)
        status = (f"{nb} Buzz set{'s' if nb != 1 else ''} detected" if nb
                  else "No Buzz buzzers detected - gamepads & keyboard work too")
        status += "  ·  LEDs " + ("on" if self.leds.available else "off")
        if self.last_press:
            status += "  ·  last press: " + self.last_press
        return status

    def draw_menu(self, surf):
        self.draw_title(surf, 170, 150)
        text(surf, "the quiz for your buzzers", 36, (W // 2, 235), DIM)
        options = [("PLAY", "pick a quiz and get buzzing"),
                   ("ADMIN MODE", "name players & build quizzes from your phone"),
                   ("QUIT", "")]
        for i, (lab, sub) in enumerate(options):
            r = pygame.Rect(0, 0, 560, 78)
            r.center = (W // 2, 330 + i * 96)
            sel = i == self.menu_i
            if sel:
                r = r.inflate(20 + 6 * math.sin(self.t * 5), 8)
                rrect(surf, shade(COL["yellow"], .5), r.move(0, 6), 39)
                rrect(surf, COL["yellow"], r, 39)
                fg = (40, 20, 70)
            else:
                rrect(surf, PANEL, r, 39)
                rrect(surf, (90, 70, 150), r, 39, 3)
                fg = WHITE
            text(surf, lab, 52 if sub else 44, (r.centerx, r.centery - (10 if sub else 0)), fg, shadow=not sel)
            if sub:
                text(surf, sub, 26, (r.centerx, r.centery + 22), fg if sel else DIM, shadow=False)
        self.draw_legend(surf, [("blue", "up"), ("yellow", "down"), ("green", "select")], 640)
        text(surf, self.device_status(), 24, (W // 2, H - 22), DIM, shadow=False)

    def draw_pick(self, surf):
        text(surf, "CHOOSE A QUIZ", 80, (W // 2, 70), COL["yellow"])
        if not self.packs:
            text(surf, "No quizzes yet - make one in Admin mode!", 44, (W // 2, 330), COL["red"])
            self.draw_legend(surf, [("orange", "back")], 640)
            return
        n = len(self.packs)
        visible = 4
        first = max(0, min(self.pick_i - 1, n - visible))
        for row, i in enumerate(range(first, min(n, first + visible))):
            pack = self.packs[i]
            sel = i == self.pick_i
            r = pygame.Rect(140, 130 + row * 116, W - 280, 100)
            if sel:
                rrect(surf, WHITE, r.inflate(14 + 4 * math.sin(self.t * 6), 12), 28)
            rrect(surf, PANEL if not sel else (55, 35, 110), r, 24)
            rrect(surf, COL["blue"] if not sel else COL["yellow"], r, 24, 3)
            text(surf, pack["name"], 50, (r.x + 30, r.y + 34), COL["yellow"] if sel else WHITE, anchor="midleft")
            desc = wrap(pack["description"], 28, r.w - 260)
            if desc:
                text(surf, desc[0] + ("..." if len(desc) > 1 else ""), 28, (r.x + 30, r.y + 72), DIM, anchor="midleft")
            text(surf, f"{len(pack['questions'])} Qs", 40, (r.right - 30, r.y + 34), anchor="midright")
            if pack["kids"]:
                pill = pygame.Rect(0, 0, 90, 30)
                pill.midright = (r.right - 30, r.y + 72)
                rrect(surf, COL["green"], pill, 15)
                text(surf, "KIDS", 26, pill.center, shadow=False)
        if first > 0:
            text(surf, "more above", 26, (W // 2, 120), DIM)
        if first + visible < n:
            text(surf, "more below", 26, (W // 2, 600), DIM)
        self.draw_legend(surf, [("blue", "up"), ("yellow", "down"), ("green", "choose"), ("orange", "back")], 640)
        text(surf, f"quiz {self.pick_i + 1} of {n}", 26, (W // 2, H - 22), DIM, shadow=False)

    def draw_admin(self, surf):
        text(surf, "ADMIN MODE", 80, (W // 2, 60), COL["yellow"])
        if not self.web_url:
            err = getattr(self, "web_error", None) or "Starting web server..."
            for i, line in enumerate(wrap(err, 36, W - 200)):
                text(surf, line, 36, (W // 2, 300 + i * 40), COL["red"])
            self.draw_legend(surf, [("green", "back to menu")], 660)
            return
        # QR + instructions on the left
        qr = self.qr_surface(300)
        panel = pygame.Rect(60, 120, 470, 470)
        rrect(surf, PANEL, panel, 24)
        if qr:
            qr_rect = qr.get_rect(center=(panel.centerx, panel.y + 190))
            rrect(surf, WHITE, qr_rect.inflate(20, 20), 12)
            surf.blit(qr, qr_rect)
            text(surf, "Scan with your phone", 34, (panel.centerx, panel.bottom - 90))
        else:
            text(surf, "Open this on your phone:", 36, (panel.centerx, panel.y + 160))
        size = 36
        while size > 22 and font(size).size(self.web_url)[0] > panel.w - 30:
            size -= 2
        text(surf, self.web_url, size, (panel.centerx, panel.bottom - 50), COL["yellow"])
        text(surf, "same Wi-Fi as this machine", 24, (panel.centerx, panel.bottom - 20), DIM, shadow=False)

        # live slot list on the right
        x = 580
        text(surf, "PLAYER NAMES", 40, (x, 140), WHITE, anchor="midleft")
        text(surf, "press a button to see which buzzer is which", 26, (x, 172), DIM, anchor="midleft", shadow=False)
        rows = [sl for sl in self.slots() if sl["kind"] in ("buzz", "pad") or sl["name"] or sl["flash"]][:9]
        if not rows:
            text(surf, "No buzzers or pads connected yet", 30, (x, 220), DIM, anchor="midleft")
        for i, sl in enumerate(rows):
            r = pygame.Rect(x, 196 + i * 44, 640, 38)
            if sl["flash"]:
                rrect(surf, COL["yellow"], r, 12)
                fg = (40, 20, 70)
            else:
                rrect(surf, PANEL, r, 12)
                fg = WHITE
            text(surf, sl["label"], 26, (r.x + 14, r.centery), fg if sl["flash"] else DIM, anchor="midleft", shadow=False)
            text(surf, sl["name"] or "-", 32, (r.right - 14, r.centery), fg, anchor="midright", shadow=False)
        conn = self.web.clients if self.web else 0
        text(surf, f"{len(self.packs)} quizzes  ·  {conn} phone{'s' if conn != 1 else ''} connected",
             26, (x, 600), DIM, anchor="midleft", shadow=False)
        self.draw_legend(surf, [("green", "done - back to menu")], 660)

    def draw_lobby(self, surf):
        self.draw_title(surf, 110, 70)
        if not self.packs:
            text(surf, "No question packs found", 44, (W // 2, 330), COL["red"])
            return
        pack = self.pack
        box = pygame.Rect(W // 2 - 360, 140, 720, 150)
        rrect(surf, PANEL, box, 24)
        rrect(surf, COL["blue"], box, 24, 3)
        text(surf, pack["name"], 56, (box.centerx, box.y + 40), COL["yellow"])
        for i, line in enumerate(wrap(pack["description"], 30, box.w - 60)[:2]):
            text(surf, line, 30, (box.centerx, box.y + 80 + i * 26), DIM)
        n = min(self.lengths[self.length_i], len(pack["questions"]))
        text(surf, f"{n} questions" + ("  ·  KIDS MODE" if pack["kids"] else ""),
             28, (box.centerx, box.bottom - 18), WHITE)

        self.draw_legend(surf, [("red", "join"), ("orange", "length"), ("blue", "change quiz"),
                                ("green", "START")], 330)
        if self.players:
            self.draw_players(surf, "lobby")
            text(surf, f"{len(self.players)} player{'s' if len(self.players) != 1 else ''} ready"
                 + ("  ·  more can press RED to join" if len(self.players) < 8 else ""),
                 40, (W // 2, 450), WHITE)
        else:
            pulse = 0.6 + 0.4 * math.sin(self.t * 4)
            text(surf, "Press RED on your buzzer to join!", 60, (W // 2, 470), shade(COL["red"], .7 + .3 * pulse))
        text(surf, self.device_status(), 24, (W // 2, 380), DIM, shadow=False)

    def draw_splash(self, surf):
        k = min(1, self.st / .35)
        size = int(40 + 100 * k)
        wob = math.sin(self.st * 14) * 4
        text(surf, "FASTEST", size, (W // 2, 250 + wob), COL["red"])
        text(surf, "FINGER!", size, (W // 2, 370 - wob), COL["yellow"])
        rule = ("First right answer wins 1000!" if self.kids
                else "First right answer wins 1000 · wrong answers lose 250")
        text(surf, rule, 40, (W // 2, 470), WHITE)
        self.draw_players(surf)

    def draw_read(self, surf):
        self.draw_header(surf)
        self.draw_question_text(surf, 140, 260)
        bw = int((W - 400) * min(1, self.st / self.read_time))
        rrect(surf, (60, 45, 110), (200, 470, W - 400, 10), 5)
        rrect(surf, DIM, (200, 470, bw, 10), 5)
        self.draw_players(surf)

    def draw_answer(self, surf):
        self.draw_header(surf)
        self.draw_question_text(surf)
        left = max(0.0, self.qtime - self.st)
        frac = left / self.qtime
        bar = pygame.Rect(140, 78, W - 280, 14)
        rrect(surf, (50, 35, 95), bar, 7)
        c = COL["green"] if frac > .5 else COL["yellow"] if frac > .25 else COL["red"]
        rrect(surf, c, (bar.x, bar.y, int(bar.w * frac), bar.h), 7)
        text(surf, f"{math.ceil(left)}", 40, (bar.right + 40, bar.centery), c)
        self.draw_answers(surf)
        self.draw_players(surf, "answer")

    def draw_reveal(self, surf):
        self.draw_header(surf)
        self.draw_question_text(surf)
        self.draw_answers(surf, reveal=True)
        self.draw_players(surf, "reveal")

    def draw_scores(self, surf):
        text(surf, "SCORES SO FAR", 80, (W // 2, 80), COL["yellow"])
        self.draw_table(surf, 160)
        text(surf, f"{len(self.questions) - self.qi - 1} questions to go  ·  press RED to carry on",
             30, (W // 2, H - 40), DIM)

    def draw_table(self, surf, top):
        ranked = sorted(self.players, key=lambda p: -p.score)
        best = max(1, ranked[0].score)
        rowh = min(80, (H - top - 90) // max(1, len(ranked)))
        k = min(1, self.st / 1.2)
        for rank, p in enumerate(ranked):
            y = top + rank * rowh
            text(surf, f"{rank + 1}", 44, (200, y + rowh // 2), DIM)
            text(surf, p.name, 44, (240, y + rowh // 2), p.tint, anchor="midleft")
            bw = int((W - 600) * max(0, p.score) / best * k)
            rrect(surf, shade(p.tint, .5), (330, y + 10, max(10, bw), rowh - 20), 12)
            rrect(surf, p.tint, (330, y + 6, max(10, bw), rowh - 20), 12)
            text(surf, f"{int(p.score * k)}", 42, (340 + max(10, bw), y + rowh // 2), anchor="midleft")

    def draw_final(self, surf):
        top = max(p.score for p in self.players)
        winners = [p for p in self.players if p.score == top]
        title = "WE HAVE A WINNER!" if len(winners) == 1 else "IT'S A DRAW!"
        if len(self.players) == 1:
            title = "GAME OVER!"
        text(surf, title, 90, (W // 2, 70 + math.sin(self.st * 3) * 5), COL["yellow"])
        names = " & ".join(p.name for p in winners)
        text(surf, f"{names}  ·  {top} points", 54, (W // 2, 145), winners[0].tint)
        self.draw_table(surf, 200)
        if self.st > 2:
            text(surf, "GREEN to play again  ·  ESC for the lobby", 34, (W // 2, H - 40), DIM)


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Buzzer Party - a quiz for Buzz! buzzers")
    ap.add_argument("--window", action="store_true", help="run in a window instead of fullscreen")
    args = ap.parse_args()

    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    pygame.init()
    pygame.joystick.init()
    pygame.display.set_caption("Buzzer Party")
    flags = pygame.SCALED | (0 if args.window else pygame.FULLSCREEN)
    screen = pygame.display.set_mode((W, H), flags)
    pygame.mouse.set_visible(args.window)

    game = Game(load_config())
    clock = pygame.time.Clock()
    try:
        while not game.quit:
            dt = min(clock.tick(60) / 1000, 0.05)
            for ev in pygame.event.get():
                r = game.inputs.handle(ev)
                if not r:
                    continue
                if r[0] == "press":
                    game.on_press(r[1], r[2])
                elif r[0] == "devices":
                    game.leds.scan()
                elif r[1] == "fullscreen":
                    pygame.display.toggle_fullscreen()
                else:
                    game.on_system(r[1])
            game.update(dt)
            game.draw(screen)
            pygame.display.flip()
    finally:
        game.leds.off()
        if game.web:
            game.web.stop()
        pygame.quit()


if __name__ == "__main__":
    main()

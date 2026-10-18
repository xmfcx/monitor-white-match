#!/usr/bin/env python3
"""A camera matches the white of up to three monitors, live, with a web page that narrates it.

    python3 show_server.py [--config config.json] [--port 8917] [--start] [--save]

GET  /             the page (index.html)
GET  /stream.mjpg  the camera, 1280x720 MJPEG, with the measurement box drawn in
GET  /events       server-sent events that narrate the run
GET  /preview?id=X aim at one target and draw its box (no run), to check a box
POST /start        run once (ignored while a run is in progress)
"""
import argparse
import http.server
import itertools
import json
import os
import queue
import random
import re
import socketserver
import subprocess
import threading
import time
import traceback

import numpy as np

import cie
import config as configlib
import ddc
from camera import Camera

HERE = os.path.dirname(os.path.abspath(__file__))
BOOT = str(time.time())
PROBE_STEPS = 5         # the measured drop per channel, in gain steps
LATTICE = 13            # the solver tries gains 50 down to 38 per channel
JND = 0.002             # about the smallest u'v' difference people see side by side
COUNT = {1: "one", 2: "two", 3: "three"}


# ---------------------------------------------------------------- events

class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []
        self.history = []

    def emit(self, etype, **data):
        ev = {"type": etype, "t": time.time(), **data}
        with self.lock:
            self.history.append(ev)
            for q in self.subs:
                q.put(ev)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            for ev in self.history:
                q.put(dict(ev, replay=True))
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def reset(self):
        with self.lock:
            self.history = []


BUS = Bus()


def dist(a, b):
    return float(np.hypot(a["u"] - b["u"], a["v"] - b["v"]))


def gammastep_temp(cfg_path):
    """The temperature gammastep applies now, or 6500 K without gammastep."""
    try:
        out = subprocess.run(["gammastep", "-c", os.path.expanduser(cfg_path), "-p"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        return 6500
    m = re.search(r"Color temperature: (\d+)K", out.stdout + out.stderr)
    return int(m.group(1)) if m else 6500


# ---------------------------------------------------------------- the show

class Show:
    def __init__(self, cam, cfg, save=False):
        self.cam = cam
        self.cfg = cfg
        self.mons = cfg["monitors"]
        self.card = cfg.get("card")
        self.save = save
        self.cam_bias = None
        self.running = False
        self.gains = {m["id"]: ddc.get_gains(m["bus"]) for m in self.mons}

    def start(self):
        if self.running:
            return False
        self.running = True
        threading.Thread(target=self._run_safe, daemon=True).start()
        return True

    # --- small helpers
    def say(self, text):
        BUS.emit("say", text=text)

    def phase(self, n, title, sub):
        BUS.emit("phase", n=n, title=title, sub=sub)

    def set_gains(self, m, **ch):
        cur = self.gains.setdefault(m["id"], {"r": None, "g": None, "b": None})
        for c in "rgb":
            if c in ch and cur[c] != ch[c]:
                ddc.set_vcp(m["bus"], ddc.GAIN[c], ch[c])
                cur[c] = ch[c]
                BUS.emit("log", text=f"ddcutil --bus {m['bus']} setvcp 0x{ddc.GAIN[c]} {ch[c]}")
        BUS.emit("gains", id=m["id"], **cur)

    def look(self, tgt, label=None):
        self.cam.overlay = None
        BUS.emit("look", id=tgt["id"])
        pan, tilt, zoom = tgt["aim"]
        BUS.emit("log", text=f"v4l2-ctl pan {pan:+g}° tilt {tilt:+g}° zoom {zoom / 100:g}x")
        self.cam.aim(pan, tilt, zoom)
        self.cam.overlay = {"roi": tgt["roi"], "label": label or tgt["name"], "bgr": tgt["bgr"]}

    def read(self, tgt, settle=0.6):
        self.cam.wait_still(min_wait=settle)
        BUS.emit("sample", id=tgt["id"])
        ov = self.cam.overlay
        if ov:
            self.cam.overlay = dict(ov, reading=True)
        r = self.cam.measure(tgt["roi"])
        # More than half the box near the top of the camera range: lower the
        # exposure and read again (needs camera.brightness in the configuration).
        while r["clip"] > 0.5 and self.cam_bias:
            self.cam_bias = max(0, self.cam_bias - 10)
            self.cam.ctl("brightness", self.cam_bias)
            BUS.emit("log", text=f"v4l2-ctl brightness {self.cam_bias} (the screen is too bright for the camera)")
            self.cam.wait_still(min_wait=0.8)
            r = self.cam.measure(tgt["roi"])
        if ov:
            self.cam.overlay = ov
        return r

    def tint_words(self, r):
        if dist(r, self.target) < JND:
            return "on target"
        if r["duv"] < -0.004:
            return "pink"
        if r["duv"] < -0.0015:
            return "a little pink"
        if r["duv"] > 0.0065:
            return "green"
        return "warmer than the target" if r["cct"] < self.target["T"] else "cooler than the target"

    # --- the run
    def _run_safe(self):
        brightness = {}
        start_gains = {k: dict(v) for k, v in self.gains.items()}
        cam_bias = self.cfg["camera"]["brightness"]
        cam_bias_before = self.cam.get_ctl("brightness") if cam_bias is not None else None
        self.cam_bias = cam_bias
        try:
            if cam_bias is not None:
                self.cam.ctl("brightness", cam_bias)
            for m in self.mons:
                brightness[m["bus"]] = ddc.get_vcp(m["bus"], ddc.BRIGHTNESS)
            self._run(brightness)
            if self.save:
                for m in self.mons:
                    ddc.save(m["bus"])
                BUS.emit("log", text="ddcutil scs (saved in each monitor)")
        except Exception as e:
            traceback.print_exc()
            BUS.emit("error", text=f"The run stopped: {e}. The earlier gains are back.")
            for m in self.mons:
                try:
                    self.set_gains(m, **start_gains[m["id"]])
                except Exception:
                    traceback.print_exc()
        finally:
            for bus, val in brightness.items():
                try:
                    ddc.set_vcp(bus, ddc.BRIGHTNESS, val)
                except Exception:
                    traceback.print_exc()
            self.cam.overlay = None
            try:
                self.cam.auto_white_balance()
                if cam_bias_before is not None:
                    self.cam.ctl("brightness", cam_bias_before)
            except Exception:
                traceback.print_exc()
            self.running = False

    def _wake(self):
        """Two eyes open on the page; the real camera looks where they look."""
        cam = self.cam
        home = self.cfg["camera"]["home"]
        n = len(self.mons)
        cam.overlay = None
        BUS.emit("wake", step="start")
        t0 = time.time()
        cam.aim(*home, min_wait=0.5)
        time.sleep(max(0.0, 1.8 - (time.time() - t0)))
        BUS.emit("eye", open=9, dur=1.8)
        time.sleep(2.4)
        BUS.emit("eye", open=50, dur=1.5)
        time.sleep(1.9)
        BUS.emit("whisper", text="Hello.")
        time.sleep(1.6)
        BUS.emit("eye", blink=True)
        time.sleep(0.9)
        BUS.emit("whisper", text="I can see one screen." if n == 1 else f"I can see {COUNT[n]} screens.")
        time.sleep(0.4)
        looks = [("center", home[0])]
        if n > 1:
            looks = [("left", self.mons[0]["aim"][0]), ("right", self.mons[-1]["aim"][0])] + looks
        for look, pan in looks:
            BUS.emit("eye", look=look)
            cam.aim(pan, home[1], home[2], min_wait=0.9)
            time.sleep(0.5)
        BUS.emit("whisper", text="Its white is off." if n == 1 else "Their whites do not match.")
        time.sleep(2.2)
        BUS.emit("eye", open=31, dur=0.9, focus=True)
        time.sleep(0.5)
        BUS.emit("whisper", text="Let me fix that.")
        time.sleep(2.4)
        BUS.emit("wake", step="end")
        time.sleep(1.6)

    def _run(self, brightness):
        BUS.reset()
        cam, cfg, mons = self.cam, self.cfg, self.mons
        n = len(mons)
        T = gammastep_temp(cfg["gammastep_config"]) if cfg["target"] == "gammastep" else int(cfg["target"])
        tu, tv = cie.daylight_uv(T)
        self.target = {"u": tu, "v": tv, "T": T}
        BUS.emit("init", monitors=monitor_info(mons), target=self.target, jnd=JND, gains=self.gains,
                 planck=[[t, *map(float, cie.planck_uv(t))] for t in range(3000, 10001, 100)],
                 daylight=[[t, *cie.daylight_uv(t)] for t in range(4000, 10001, 100)])
        cam.fix_white_balance(cfg["camera"]["white_balance_k"])
        if cfg["white_background_cmd"]:
            subprocess.run(cfg["white_background_cmd"], shell=True, capture_output=True)
        self._wake()

        # 1. factory
        self.phase(1, "Back to factory settings",
                   "Every color gain goes back to 50, the way the monitors shipped.")
        self.say("Resetting the monitor to its factory white." if n == 1
                 else f"Resetting all {COUNT[n]} monitors to their factory white.")
        for m in mons:
            self.set_gains(m, r=50, g=50, b=50)
        time.sleep(2.5)

        # 2. look
        self.phase(2, "Looking at each monitor",
                   "The camera sits where your head is and reads the white of each screen.")
        start = {}
        for m in mons:
            self.say(f"Looking at the {m['name'].lower()}.")
            self.look(m)
            r = self.read(m)
            start[m["id"]] = r
            BUS.emit("meas", id=m["id"], kind="start", **r)
            self.say(f"{m['name']}: {r['cct']} K, {self.tint_words(r)}.")
            time.sleep(1.4)
        if self.card:
            self.say(f"Now the {self.card['name'].lower()}, for reference.")
            self.look(self.card)
            r = self.read(self.card)
            BUS.emit("meas", id="card", kind="ref", **r)
            self.say(f"The card reads {r['cct']} K. The target is daylight at {T} K.")
            time.sleep(1.6)
        start_spread = max((dist(start[a], start[b]) for a, b in itertools.combinations(start, 2)),
                           default=dist(next(iter(start.values())), self.target))
        BUS.emit("spread", value=start_spread, stage="start")
        self.say(f"The whites are {start_spread:.4f} apart. People see about {JND}.")
        time.sleep(2.5)

        # 3. test the controls
        self.phase(3, "Testing the color controls",
                   "Each monitor gets red, green, and blue turned down, one at a time.")
        jac, base = {}, {}
        for m in mons:
            self.say(f"Testing the {m['name'].lower()}.")
            self.look(m, f"{m['name']}: testing")
            for c, word, tint in (("r", "red", "cyan"), ("g", "green", "magenta"), ("b", "blue", "yellow")):
                self.say(f"{word.capitalize()} off: white turns {tint}.")
                self.set_gains(m, **{c: 0})
                time.sleep(1.5)
                self.set_gains(m, **{c: 50})
                time.sleep(0.5)
            self.say(f"Now small steps of {PROBE_STEPS}, measured.")
            b0 = self.read(m, settle=1.0)
            base[m["id"]] = b0
            BUS.emit("meas", id=m["id"], kind="base", **b0)
            rows = []
            for c in "rgb":
                self.set_gains(m, **{c: 50 - PROBE_STEPS})
                time.sleep(0.6)
                r = self.read(m)
                BUS.emit("meas", id=m["id"], kind="probe", ch=c, **r)
                rows.append([(r["u"] - b0["u"]) / PROBE_STEPS, (r["v"] - b0["v"]) / PROBE_STEPS])
                self.set_gains(m, **{c: 50})
            jac[m["id"]] = rows
            BUS.emit("jac", id=m["id"], base=b0, J=rows)
            time.sleep(0.8)

        # 4. solve: a linear model predicts the white of every gain setting
        self.phase(4, "Solving",
                   "A linear model predicts the white for every gain setting and picks the one closest to the target.")
        tgt = np.array([tu, tv])
        solution = {}
        for m in mons:
            J = np.array(jac[m["id"]])
            b0 = np.array([base[m["id"]]["u"], base[m["id"]]["v"]])
            combos = list(itertools.product(range(LATTICE), repeat=3))
            random.Random(7).shuffle(combos)            # the page reveals them in this order
            pts, best, steps = [], None, []
            for i, k in enumerate(combos):
                p = b0 + np.array(k) @ J
                cost = float(np.sum((p - tgt) ** 2) + 2e-7 * sum(k))   # small tie-break toward brighter
                pts.append([round(float(p[0]), 5), round(float(p[1]), 5)])
                if best is None or cost < best[0]:
                    best = (cost, k, p)
                    steps.append({"i": i, "gains": [50 - x for x in k],
                                  "err": round(float(np.hypot(*(p - tgt))), 5)})
            g = [50 - x for x in best[1]]
            solution[m["id"]] = g
            self.say(f"Trying {len(combos):,} gain settings for the {m['name'].lower()}.")
            BUS.emit("cloud", id=m["id"], points=pts, steps=steps, J=jac[m["id"]],
                     best={"u": float(best[2][0]), "v": float(best[2][1]), "gains": g})
            time.sleep(3.4)
            self.say(f"{m['name']}: red {g[0]}, green {g[1]}, blue {g[2]}.")
            time.sleep(1.2)

        # 5. match: apply, measure, then a greedy search over the neighbors
        self.phase(5, "Matching", "The camera checks each monitor and fixes the last small error.")
        self.say("Setting the new gains.")
        for m in mons:
            r, g, b = solution[m["id"]]
            self.set_gains(m, r=r, g=g, b=b)
        time.sleep(1.2)
        final = {}
        for m in mons:
            self.say(f"Checking the {m['name'].lower()}.")
            self.look(m)
            r = self.read(m, settle=1.0)
            BUS.emit("meas", id=m["id"], kind="verify", **r)
            best = (dist(r, self.target), tuple(solution[m["id"]]), r)
            if best[0] > 0.0010:
                self.say(f"{best[0]:.4f} off. Trying the neighbors.")
                J = np.array(jac[m["id"]])
                tried = {best[1]}
                for _ in range(6):
                    if best[0] <= 0.0008:
                        break
                    cur = np.array(best[1])
                    here = np.array([best[2]["u"], best[2]["v"]])
                    cands = []
                    for d in itertools.product((-1, 0, 1), repeat=3):
                        c = tuple(int(x) for x in cur + np.array(d))
                        if c in tried or max(c) > 50 or min(c) < 30:
                            continue
                        p = here + (cur - np.array(c)) @ J
                        cands.append((float(np.hypot(*(p - tgt))), c))
                    if not cands:
                        break
                    c = min(cands)[1]
                    tried.add(c)
                    self.set_gains(m, r=c[0], g=c[1], b=c[2])
                    time.sleep(0.5)
                    r2 = self.read(m)
                    BUS.emit("meas", id=m["id"], kind="try", **r2)
                    e2 = dist(r2, self.target)
                    if e2 < best[0]:
                        best = (e2, c, r2)
                g = best[1]
                self.set_gains(m, r=g[0], g=g[1], b=g[2])
                BUS.emit("meas", id=m["id"], kind="verify", **best[2])
            final[m["id"]] = best[2]
            self.say(f"{m['name']}: {best[0]:.4f} from the target.")
            time.sleep(1.0)
        spread = max((dist(final[a], final[b]) for a, b in itertools.combinations(final, 2)),
                     default=dist(next(iter(final.values())), self.target))
        BUS.emit("matched", spread=spread, start=start_spread,
                 gains={k: [v["r"], v["g"], v["b"]] for k, v in self.gains.items()})
        self.say(f"Matched. The largest difference left is {spread:.4f}.")
        time.sleep(3.0)

        # 6. bow: the mascot on the page bows with the camera; the other monitors dim
        self.phase(6, "Thank you", "")
        cam.overlay = None
        BUS.emit("bow", step="show")
        self.say("Thank you.")
        home = cfg["camera"]["home"]
        cam.aim(*home, min_wait=0.8)
        time.sleep(1.2)
        side = [m for m in mons if m["id"] != cfg["ui_monitor"]]
        for m in side:
            ddc.set_vcp(m["bus"], ddc.BRIGHTNESS, 0)
        BUS.emit("bow", step="down")
        cam.tilt_now(home[1] - 34)
        time.sleep(2.0)
        BUS.emit("bow", step="up")
        cam.tilt_now(home[1])
        time.sleep(1.0)
        for m in side:
            ddc.set_vcp(m["bus"], ddc.BRIGHTNESS, brightness[m["bus"]])
        time.sleep(0.7)
        BUS.emit("bow", step="nod")
        cam.tilt_now(home[1] - 12)
        time.sleep(1.0)
        BUS.emit("bow", step="up")
        cam.tilt_now(home[1])
        time.sleep(1.2)
        BUS.emit("done", gains={k: [v["r"], v["g"], v["b"]] for k, v in self.gains.items()})


def monitor_info(mons):
    return [{k: m[k] for k in ("id", "name", "short", "model", "color")} for m in mons]


# ---------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/stream.mjpg"):
            self._stream()
        elif self.path.startswith("/events"):
            self._events()
        elif self.path.startswith("/preview"):
            tid = self.path.partition("id=")[2]
            tgt = next((t for t in SHOW.mons + ([SHOW.card] if SHOW.card else []) if t["id"] == tid), None)
            if tgt is None or SHOW.running:
                self._send(409)
                return

            def go():
                CAM.overlay = None
                CAM.aim(*tgt["aim"])
                CAM.overlay = {"roi": tgt["roi"], "label": tgt["name"], "bgr": tgt["bgr"]}
            threading.Thread(target=go, daemon=True).start()
            self._send(204)
        else:
            self._send(404)

    def do_POST(self):
        if self.path == "/start":
            ok = SHOW.start()
            self._send(200 if ok else 409, json.dumps({"started": ok}).encode())
        else:
            self._send(404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        CAM.viewers += 1
        try:
            seq = 0
            while True:
                with CAM.jpeg_cond:
                    CAM.jpeg_cond.wait_for(lambda: CAM.jpeg_seq > seq, timeout=3)
                    seq, jpg = CAM.jpeg_seq, CAM.jpeg
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            CAM.viewers -= 1

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = BUS.subscribe()
        try:
            hello = {"type": "hello", "running": SHOW.running, "boot": BOOT,
                     "monitors": monitor_info(SHOW.mons), "gains": SHOW.gains}
            self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=10)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b'data: {"type": "ping"}\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            BUS.unsubscribe(q)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global CAM, SHOW
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--port", type=int)
    ap.add_argument("--start", action="store_true", help="start one run at once (no page needed)")
    ap.add_argument("--save", action="store_true", help="store the result in each monitor (ddcutil scs)")
    ap.add_argument("--replay", metavar="EVENTS", help="replay a saved event stream, for page work")
    ap.add_argument("--replay-until", type=int, default=10 ** 9)
    args = ap.parse_args()
    cfg = configlib.load(args.config)
    cam_cfg = cfg["camera"]
    CAM = Camera(cam_cfg["device"], cam_cfg["width"], cam_cfg["height"])
    SHOW = Show(CAM, cfg, save=args.save)
    CAM.aim(*cam_cfg["home"], min_wait=0.5)
    if args.replay:
        with open(args.replay) as f:
            evs = [json.loads(line[6:]) for line in f if line.startswith("data: ")]
        BUS.history = [e for e in evs if e["type"] not in ("hello", "ping")][:args.replay_until]
    if args.start:
        SHOW.start()
    port = args.port or cfg["port"]
    print(f"listening on http://127.0.0.1:{port}/", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

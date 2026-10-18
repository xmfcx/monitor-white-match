"""A UVC pan/tilt camera: a frame thread, aiming, waiting for stillness, color readings,
and an optional 1280x720 JPEG stream with the measurement box drawn in."""
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

import cie


CLIP_CODE = 242     # 8-bit camera code at which highlights start to compress


class Camera:
    def __init__(self, device="/dev/video0", width=3840, height=2160):
        self.device = device
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            sys.exit(f"cannot open the camera {device}")
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.jpeg_cond = threading.Condition()
        self.jpeg = None
        self.jpeg_seq = 0
        self.viewers = 0
        self.overlay = None     # {"roi", "label", "bgr", "reading"}
        self.cmd = {}           # last commanded pan/tilt/zoom
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._encode_loop, daemon=True).start()

    # --- controls
    def ctl(self, name, value):
        subprocess.run(["v4l2-ctl", "-d", self.device, "-c", f"{name}={value}"], check=True)

    def get_ctl(self, name):
        out = subprocess.run(["v4l2-ctl", "-d", self.device, "-C", name],
                             capture_output=True, text=True).stdout
        return int(out.split(":")[1])

    def fix_white_balance(self, kelvin):
        self.ctl("white_balance_automatic", 0)
        # The driver drops a write that equals its cached value, and the camera
        # then keeps the gains its auto mode last chose. Write a neighbor first.
        self.ctl("white_balance_temperature", kelvin - 1)
        self.ctl("white_balance_temperature", kelvin)

    def auto_white_balance(self):
        self.ctl("white_balance_automatic", 1)

    # --- frames
    def close(self):
        """Stop the frame thread before the capture goes away (OpenCV aborts otherwise)."""
        self._stop = True
        self._reader.join(timeout=3)
        self.cap.release()

    def _read_loop(self):
        while not self._stop:
            ok, f = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self.cond:
                self.frame = f
                self.seq += 1
                self.cond.notify_all()

    def next_frame(self, after=None):
        with self.cond:
            s = self.seq if after is None else after
            self.cond.wait_for(lambda: self.seq > s, timeout=3)
            return self.seq, self.frame

    def wait_still(self, min_wait=1.0, timeout=12.0, thresh=0.8, need=10):
        """Read frames until the motor and the auto exposure stop moving."""
        t0 = time.time()
        prev = None
        still = 0
        seq = None
        while time.time() - t0 < timeout:
            seq, f = self.next_frame(seq)
            small = cv2.resize(f, (192, 108), interpolation=cv2.INTER_AREA).astype(np.float32)
            if prev is not None:
                d = float(np.abs(small - prev).mean())
                still = still + 1 if (d < thresh and time.time() - t0 > min_wait) else 0
                if still >= need:
                    return True
            prev = small
        return False

    def aim(self, pan, tilt, zoom, min_wait=1.2):
        """Pan and tilt in degrees, zoom in percent. One axis at a time, each to
        rest: pan and tilt share one UVC control, and a tilt write during a pan
        stops the pan where it is."""
        want = {"pan_absolute": int(pan * 3600), "tilt_absolute": int(tilt * 3600),
                "zoom_absolute": int(zoom)}
        moved = False
        for name, value in want.items():
            if self.cmd.get(name) != value:
                self.ctl(name, value)
                self.cmd[name] = value
                time.sleep(0.25)
                self.wait_still(min_wait=min_wait)
                moved = True
        if not moved:
            self.wait_still(min_wait=0.3)

    def tilt_now(self, tilt):
        """Tilt without waiting, for gestures."""
        self.ctl("tilt_absolute", int(tilt * 3600))
        self.cmd["tilt_absolute"] = int(tilt * 3600)

    def measure(self, roi, n=10):
        """Average n fresh frames inside roi (x0, y0, x1, y1 as fractions) in linear
        light, drop the darkest and brightest 10 % of pixels, and return the white."""
        frames = []
        seq = None
        for _ in range(n):
            seq, f = self.next_frame(seq)
            frames.append(f)
        h, w = frames[0].shape[:2]
        x0, y0, x1, y1 = roi
        sl = (slice(int(y0 * h), int(y1 * h)), slice(int(x0 * w), int(x1 * w)))
        acc = None
        bad = None
        for f in frames:
            c = f[sl]
            lin = cie.SRGB_TO_LINEAR[c]
            acc = lin if acc is None else acc + lin
            # The camera compresses its top codes, so a pixel near the top in any
            # channel has the wrong color. Such pixels are left out.
            hot = (c >= CLIP_CODE).any(axis=2)
            bad = hot if bad is None else bad | hot
        clip = float(bad.mean())
        px = (acc / n).reshape(-1, 3)[:, ::-1]          # BGR -> RGB
        ok = ~bad.reshape(-1)
        if ok.mean() >= 0.05:
            px = px[ok]
        y = px @ np.array([0.2126, 0.7152, 0.0722])
        lo, hi = np.percentile(y, [10, 90])
        rgb = px[(y >= lo) & (y <= hi)].mean(axis=0)
        u, v = cie.linear_rgb_to_uv(rgb)
        cct, duv = cie.cct_duv(u, v)
        Y = float(cie.M_SRGB_TO_XYZ[1] @ rgb)
        return {"u": round(float(u), 5), "v": round(float(v), 5), "cct": round(cct),
                "duv": round(duv, 4), "Y": round(Y, 4), "clip": round(clip, 3)}

    def snapshot(self):
        return self.next_frame()[1]

    # --- stream
    def _encode_loop(self):
        last = 0
        while True:
            if not self.viewers:
                time.sleep(0.2)
                continue
            last, f = self.next_frame(last)
            small = cv2.resize(f, (1280, 720), interpolation=cv2.INTER_AREA)
            ov = self.overlay
            if ov:
                draw_box(small, ov)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                with self.jpeg_cond:
                    self.jpeg = buf.tobytes()
                    self.jpeg_seq += 1
                    self.jpeg_cond.notify_all()
            time.sleep(0.06)


def draw_box(img, ov):
    """Corner brackets around the measurement box, with a label."""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = ov["roi"]
    p0 = (int(x0 * W), int(y0 * H))
    p1 = (int(x1 * W), int(y1 * H))
    c = ov["bgr"]
    arm = max(14, int(min(p1[0] - p0[0], p1[1] - p0[1]) * 0.25))
    for (x, y, dx, dy) in ((p0[0], p0[1], 1, 1), (p1[0], p0[1], -1, 1),
                           (p0[0], p1[1], 1, -1), (p1[0], p1[1], -1, -1)):
        cv2.line(img, (x, y), (x + dx * arm, y), c, 4, cv2.LINE_AA)
        cv2.line(img, (x, y), (x, y + dy * arm), c, 4, cv2.LINE_AA)
    if ov.get("reading"):
        layer = img.copy()
        cv2.rectangle(layer, p0, p1, c, -1)
        cv2.addWeighted(layer, 0.18, img, 0.82, 0, img)
    label = ov.get("label")
    if label:
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_DUPLEX, 0.8, 1)
        ty = p0[1] - 12 if p0[1] > th + 24 else p1[1] + th + 14
        cv2.rectangle(img, (p0[0], ty - th - 8), (p0[0] + tw + 16, ty + 8), (43, 34, 28), -1)
        cv2.putText(img, label, (p0[0] + 8, ty), cv2.FONT_HERSHEY_DUPLEX, 0.8, (244, 241, 238), 1, cv2.LINE_AA)

#!/usr/bin/env python3
"""Set up and check the camera targets in config.json.

    python3 measure.py shot NAME PAN TILT ZOOM    aim, wait until still, save captures/NAME.jpg
                                                  and captures/NAME_grid.jpg (a 10 % grid to read boxes from)
    python3 measure.py target ID [ID ...]         measure the configured targets (monitor ids or "card")
    python3 measure.py sweep ID STEP [STEP ...]   aim at ID once, then per STEP apply DDC writes and measure;
                                                  STEP is "bus:code:value+bus:code:value", or "-" for none

Add --config FILE before the command to use another configuration file.
"""
import json
import os
import subprocess
import sys
import time

import cv2

import config as configlib
from camera import Camera, draw_box

OUT = "captures"


def report(name, r):
    print(f"{name:10s} u'v'=({r['u']:.4f}, {r['v']:.4f})  {r['cct']} K  Duv {r['duv']:+.4f}  "
          f"Y {r['Y']:.3f}  clipped {r['clip']:.1%}", flush=True)


def main():
    args = sys.argv[1:]
    cfg_path = "config.json"
    if args[:1] == ["--config"]:
        cfg_path, args = args[1], args[2:]
    if not args or args[0] not in ("shot", "target", "sweep"):
        sys.exit(__doc__)
    cfg = configlib.load(cfg_path)
    targets = {m["id"]: m for m in cfg["monitors"]}
    if cfg.get("card"):
        targets["card"] = cfg["card"]
    os.makedirs(OUT, exist_ok=True)
    cam = Camera(cfg["camera"]["device"], cfg["camera"]["width"], cfg["camera"]["height"])
    cam.fix_white_balance(cfg["camera"]["white_balance_k"])
    bias = cfg["camera"]["brightness"]
    bias_before = cam.get_ctl("brightness") if bias is not None else None
    if bias is not None:
        cam.ctl("brightness", bias)
    try:
        cmd = args[0]
        if cmd == "shot":
            name, pan, tilt, zoom = args[1], float(args[2]), float(args[3]), float(args[4])
            cam.aim(pan, tilt, zoom)
            f = cam.snapshot()
            cv2.imwrite(f"{OUT}/{name}.jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 95])
            small = cv2.resize(f, (1280, 720), interpolation=cv2.INTER_AREA)
            for i in range(1, 10):
                cv2.line(small, (128 * i, 0), (128 * i, 719), (0, 255, 0), 1)
                cv2.line(small, (0, 72 * i), (1279, 72 * i), (0, 255, 0), 1)
            cv2.imwrite(f"{OUT}/{name}_grid.jpg", small)
            print(f"saved {OUT}/{name}.jpg and {OUT}/{name}_grid.jpg")
        elif cmd == "target":
            for tid in args[1:]:
                t = targets[tid]
                cam.aim(*t["aim"])
                cam.wait_still(min_wait=0.6)
                r = cam.measure(t["roi"])
                report(tid, r)
                small = cv2.resize(cam.snapshot(), (1280, 720), interpolation=cv2.INTER_AREA)
                draw_box(small, {"roi": t["roi"], "label": t["name"], "bgr": t["bgr"]})
                cv2.imwrite(f"{OUT}/target_{tid}.jpg", small)
                with open(f"{OUT}/measurements.jsonl", "a") as f:
                    f.write(json.dumps({"t": time.time(), "id": tid, **r}) + "\n")
        else:
            t = targets[args[1]]
            cam.aim(*t["aim"])
            for step in args[2:]:
                if step != "-":
                    for w in step.split("+"):
                        bus, code, val = w.split(":")
                        subprocess.run(["ddcutil", "--bus", bus, "--noverify", "setvcp", code, val],
                                       check=True, capture_output=True)
                    time.sleep(0.8)
                cam.wait_still(min_wait=1.0)
                report(step, cam.measure(t["roi"]))
    finally:
        cam.auto_white_balance()
        if bias_before is not None:
            cam.ctl("brightness", bias_before)
        cam.close()


if __name__ == "__main__":
    main()

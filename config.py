"""Load and check config.json (see config.example.json)."""
import json
import os
import sys

# Categorical colors for the monitors, in order. Checked for color-blind separation on the dark UI.
PALETTE = ["#3987e5", "#d95926", "#199e70"]


def _bgr(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (b, g, r)


def load(path):
    if not os.path.exists(path):
        sys.exit(f"{path} not found. Copy config.example.json to config.json and edit it.")
    with open(path) as f:
        cfg = json.load(f)
    cam = cfg.setdefault("camera", {})
    cam.setdefault("device", "/dev/video0")
    cam.setdefault("width", 3840)
    cam.setdefault("height", 2160)
    cam.setdefault("white_balance_k", 6500)
    cam.setdefault("home", [0, 0, 100])
    cam.setdefault("brightness", None)      # v4l2 "brightness": biases the auto exposure
    cfg.setdefault("target", "gammastep")
    cfg.setdefault("gammastep_config", "~/.config/gammastep/config.ini")
    cfg.setdefault("white_background_cmd", None)
    cfg.setdefault("port", 8917)
    mons = cfg.get("monitors") or []
    if not 1 <= len(mons) <= len(PALETTE):
        sys.exit(f"config: list 1 to {len(PALETTE)} monitors, left to right")
    for i, m in enumerate(mons):
        for key in ("id", "name", "bus", "aim", "roi"):
            if key not in m:
                sys.exit(f"config: monitor {i} needs '{key}'")
        m.setdefault("model", "")
        m.setdefault("short", m["name"].split()[0])
        m["color"] = PALETTE[i]
        m["bgr"] = _bgr(PALETTE[i])
    cfg.setdefault("ui_monitor", mons[len(mons) // 2]["id"])
    card = cfg.get("card")
    if card:
        card.setdefault("id", "card")
        card.setdefault("name", "White card")
        card["bgr"] = (235, 235, 235)
    return cfg

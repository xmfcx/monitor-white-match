"""Monitor controls over DDC/CI, through ddcutil."""
import subprocess

# MCCS VCP codes
GAIN = {"r": "16", "g": "18", "b": "1A"}
BRIGHTNESS = "10"


def set_vcp(bus, code, value):
    subprocess.run(["ddcutil", "--bus", str(bus), "--noverify", "setvcp", code, str(value)],
                   check=True, capture_output=True)


def get_vcp(bus, code):
    out = subprocess.run(["ddcutil", "--bus", str(bus), "getvcp", code, "--brief"],
                         check=True, capture_output=True, text=True).stdout.split()
    return int(out[3])


def get_gains(bus):
    return {c: get_vcp(bus, code) for c, code in GAIN.items()}


def save(bus):
    """Store the current settings in the monitor's own memory."""
    subprocess.run(["ddcutil", "--bus", str(bus), "scs"], check=True, capture_output=True)

"""What this machine can record from, and which of it to offer.

Two sources of truth, in that order. Core Audio through the helper, which
identifies a device by UID — a name that means the same device tomorrow. And
ffmpeg's own listing as the fallback, which numbers devices by where they sit in
a list, so a stored `1` has already meant two different microphones on one
machine and the recording that came back was silence that looked exactly like a
bug in the recorder.

That is why nothing here hands a number back to be stored. `resolve_saved` and
`name_for` are the pair that keeps a remembered choice pointing at the same
physical device across a reboot, a headset, and a Bluetooth reconnect.
"""

from __future__ import annotations

import re
import sys

import syshelper
from config import Failed, recording_config
# Through the module rather than `from syshelper import system_audio`: a
# from-import takes a copy of the name, so anything that replaces the
# function on syshelper — a test, a future platform shim — would be talking
# to a binding nothing here ever reads. The constant is safe to copy.
from syshelper import SYSTEM_AUDIO
from tools import binary, capture

# What a device is called when it exists to carry audio back into the machine.
# Used to preselect the right one and to notice when there is none.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "vb-cable", "vb cable",
                  "aggregate", "multi-output", "monitor of", ".monitor")


AVF_HEADER = re.compile(r"AVFoundation (audio|video) devices:")
AVF_PREFIX = re.compile(r"^\[AVFoundation[^\]]*\]\s*")
AVF_DEVICE = re.compile(r"^\[(\d+)\]\s+(.+?)\s*$")
PULSE_DEVICE = re.compile(r"^[*\s]+(\S+)\s+\[(.*)\]\s*$")


def _list_command() -> list[str]:
    if sys.platform == "darwin":
        return [binary("ffmpeg"), "-hide_banner", "-f", "avfoundation",
                "-list_devices", "true", "-i", ""]
    return [binary("ffmpeg"), "-hide_banner", "-sources", "pulse"]


def _parse_avfoundation(text: str) -> list[dict]:
    """The audio half of `-list_devices true`, which prints video first."""
    found, in_audio = [], False
    for raw in text.splitlines():
        line = AVF_PREFIX.sub("", raw).strip()
        header = AVF_HEADER.search(line)
        if header:
            in_audio = header.group(1) == "audio"
            continue
        if not in_audio:
            continue
        m = AVF_DEVICE.match(line)
        if m:
            found.append({"id": m.group(1), "name": m.group(2)})
    return found


def _parse_pulse(text: str) -> list[dict]:
    """`-sources pulse`, one indented `name [description]` per line."""
    found = []
    for raw in text.splitlines():
        m = PULSE_DEVICE.match(raw)
        if m:
            found.append({"id": m.group(1), "name": m.group(2) or m.group(1)})
    return found


async def _default_input_name() -> str:
    """The input macOS would pick by itself, which is the sensible first guess.

    Handy asks CoreAudio for its default input rather than taking the first device
    it is offered, and it is right to: "first in the list" put a Bluetooth headset
    on somebody's voice channel here, which recorded forty decibels of nothing.
    """
    if sys.platform != "darwin":
        return ""
    try:
        _, out = await capture(["system_profiler", "SPAudioDataType"], timeout=20)
    except (Failed, OSError):
        return ""
    name = ""
    for raw in out.splitlines():
        line = raw.strip()
        if line.endswith(":") and not line.startswith("Default"):
            name = line[:-1]
        elif "Default Input Device: Yes" in line:
            return name
    return ""


def resolve_saved(saved: str, found: list[dict]) -> str:
    """A remembered device, found again in today's list by the name it was saved as.

    By name, because avfoundation numbers devices by position and the position
    moves. A stored "1" meant the built-in microphone one day and a disconnected
    headset the next, and the recording that came back was silence that looked
    exactly like a bug in the recorder.
    """
    if not saved:
        return ""
    if saved == SYSTEM_AUDIO:
        return SYSTEM_AUDIO
    for device in found:
        if device["name"] == saved:
            return device["id"]
    # Saved before this was stored by name: a bare index, honoured only while some
    # device still sits at it.
    if saved.isdigit() and any(device["id"] == saved for device in found):
        return saved
    return ""


def name_for(device_id: str, found: list[dict]) -> str:
    """What to remember a chosen device as."""
    if device_id == SYSTEM_AUDIO:
        return SYSTEM_AUDIO
    for device in found:
        if device["id"] == device_id:
            return device["name"]
    return device_id


def is_loopback(device: dict) -> bool:
    haystack = f"{device['name']} {device['id']}".lower()
    return any(hint in haystack for hint in LOOPBACK_HINTS)


async def _core_audio_inputs() -> list[dict]:
    """The microphones, asked of Core Audio through the helper.

    Identified by UID rather than by position. ffmpeg numbers devices by where
    they sit in a list and that moves the moment anything is plugged in or taken
    away — a stored `1` has already meant two different microphones on this
    machine, and the recording that came back was silence that looked exactly like
    a bug in the recorder.
    """
    helper = await syshelper.helper_path()
    if helper is None:
        return []
    try:
        code, out = await capture([str(helper), "--list-inputs"], timeout=20)
    except (Failed, OSError):
        return []
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            found.append({"id": parts[0], "name": parts[1],
                          "default": len(parts) > 2 and parts[2].strip() == "default"})
    return found


async def known_inputs() -> tuple[list[dict], str]:
    """Every input device this machine will admit to, and whatever was said getting it.

    Core Audio first, ffmpeg second. Both callers want the same thing and one of
    them wants the text as well: the listing output is worth showing only when it
    produced no devices, and is ffmpeg noise about a healthy machine otherwise.
    """
    found = await _core_audio_inputs() if sys.platform == "darwin" else []
    if found:
        return found, ""
    # `-list_devices true` has no input to process, so ffmpeg prints the list and
    # then exits non-zero. The code says nothing about whether listing worked.
    # Still the answer everywhere but macOS, and the fallback there if the helper
    # cannot be built.
    try:
        _, out = await capture(_list_command(), timeout=30)
    except (Failed, OSError):
        return [], ""
    return (_parse_avfoundation(out) if sys.platform == "darwin" else _parse_pulse(out)), out


async def devices() -> dict:
    """Audio inputs this machine offers, and what is missing if anything is.

    Never called from /api/state: listing devices spawns ffmpeg, and on macOS it
    opens each device briefly. Once per visit to the Record view is enough.
    """
    found, listing = await known_inputs()
    for device in found:
        device["loopback"] = is_loopback(device)
    # Listed last so that it is the computer side's first loopback without ever
    # being mistaken for the microphone, whose guess takes the first that is not.
    sysaudio = await syshelper.system_audio()
    if sysaudio is not None:
        found.append({"id": SYSTEM_AUDIO, "name": "System audio",
                      "loopback": True, "builtin": True})
    if not any(device.get("default") for device in found):
        default_input = await _default_input_name()
        for device in found:
            device["default"] = bool(default_input) and device["name"] == default_input
    conf = recording_config()
    advice = []
    if not found:
        advice.append("noDevices")
    elif not any(d["loopback"] for d in found):
        advice.append("needLoopback")
    return {
        "devices": found,
        # Remembered by name, handed back as whatever index that name has today.
        "voice": resolve_saved(conf["voice"], found),
        "computer": resolve_saved(conf["computer"], found),
        "folder": conf["folder"],
        "labels": list(conf["labels"]),
        "advice": advice,
        # Only when there is nothing to show: otherwise this is ffmpeg noise about
        # devices that listed perfectly well.
        "log": [] if found else listing.splitlines()[-20:],
    }

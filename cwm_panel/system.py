"""OpenBSD adapters. No shell interpolation; privileged operations are explicit."""
from __future__ import annotations

import platform
import os
import re
import subprocess
import socket
from dataclasses import dataclass, field
from typing import Callable


class SystemError(RuntimeError):
    pass


class Daemon:
    def __init__(self, path):
        self.path = path

    def request(self, operation, *values):
        payload = "\t".join([operation] + [str(v).encode().hex() for v in values]) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(35)
                sock.connect(self.path)
                sock.sendall(payload.encode("ascii"))
                with sock.makefile("rb") as stream:
                    response = stream.readline(140000)
            if not response.endswith(b"\n"):
                raise ValueError("incomplete response")
            status, encoded = response.decode("ascii").rstrip("\n").split("\t", 1)
            message = bytes.fromhex(encoded).decode("utf-8")
            if status == "ERR":
                raise SystemError(message)
            if status != "OK":
                raise ValueError("invalid status")
            return message.strip()
        except (OSError, ValueError, UnicodeError):
            raise SystemError("Cannot reach cairnd. Check service and cairn group membership.") from None


@dataclass
class Network:
    interface: str
    wireless: bool = False
    up: bool = False
    connected: bool = False
    ssid: str = ""
    address: str = ""
    signal: str = ""


@dataclass
class State:
    networks: list[Network] = field(default_factory=list)
    battery: int | None = None
    ac: bool | None = None
    minutes: int | None = None
    volume: int | None = None
    muted: bool = False
    brightness: int | None = None
    errors: list[str] = field(default_factory=list)


def parse_interfaces(output: str) -> list[Network]:
    result = []
    for block in re.split(r"(?=^\S[^\n]*: flags=)", output, flags=re.M):
        head = re.match(r"(\S+): flags=\d+<([^>]+)>", block)
        if not head or head[1].startswith(("lo", "enc", "pflog", "pfsync")):
            continue
        n = Network(head[1], wireless="ieee80211:" in block,
                    up="UP" in head[2].split(","))
        address = re.search(r"^\s+inet (\S+)", block, re.M)
        if not address:
            address = re.search(r"^\s+inet6 (?!fe80:)(\S+)", block, re.M)
        n.address = address[1] if address else ""
        ssid = re.search(r'\bnwid (?:"([^"]*)"|(.*?))(?= chan | bssid | wpakey | wpaprotos |$)', block, re.M)
        n.ssid = (ssid[1] or ssid[2] or "").strip() if ssid else ""
        signal = re.search(r"(-?\d+(?:%|dBm))", block)
        n.signal = signal[1] if signal else ""
        n.connected = bool(re.search(r"status: (?:active|associated)", block))
        result.append(n)
    return result


def parse_scan(output: str) -> list[tuple[str, str]]:
    found = {}
    for line in output.splitlines():
        m = re.search(r'\bnwid (?:"([^"]*)"|(.+?))(?= chan | bssid |$)', line)
        if m:
            name = (m[1] or m[2] or "").strip()
            if name:
                found[name] = "secured" if re.search(r"wpa|privacy", line, re.I) else "open / unknown"
    return sorted(found.items(), key=lambda item: item[0].casefold())


class OpenBSD:
    def __init__(self, config: dict, runner: Callable | None = None):
        self.config = config
        self.runner = runner or subprocess.run
        self.daemon = Daemon(config.get("socket", "/var/run/cairn/control.sock"))

    def run(self, *args: str) -> str:
        command = list(args)
        try:
            p = self.runner(command, capture_output=True, text=True, timeout=12,
                            env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise SystemError(f"{args[0]} unavailable or timed out") from None
        if p.returncode:
            # Never echo arguments: Wi-Fi keys can be among them.
            raise SystemError(f"{args[0]} failed. Check device support and configuration.")
        return p.stdout.strip()

    def read(self) -> State:
        s = State()
        def get(*args):
            try:
                return self.run(*args)
            except SystemError as e:
                s.errors.append(str(e))
                return ""
        s.networks = parse_interfaces(get("ifconfig", "-a"))
        for option, field_name in (("-l", "battery"), ("-a", "ac"), ("-m", "minutes")):
            value = get("apm", option)
            if value.isdigit():
                number = int(value)
                if field_name == "ac":
                    s.ac = bool(number) if number in (0, 1) else None
                elif field_name == "battery":
                    s.battery = number if 0 <= number <= 100 else None
                else:
                    s.minutes = number if number < 65535 else None
        audio = get("sndioctl", "-n", "output.level", "output.mute").splitlines()
        if audio:
            try:
                s.volume = round(float(audio[0]) * 100)
                s.muted = len(audio) > 1 and float(audio[1]) == 1
            except ValueError:
                s.errors.append("Could not read sndio output controls")
        try:
            brightness = self.daemon.request("brightness-get")
        except SystemError as e:
            s.errors.append(str(e))
            brightness = ""
        try:
            s.brightness = round(float(brightness.rstrip("%")))
        except ValueError:
            pass
        return s

    def volume(self, value: int):
        self.run("sndioctl", f"output.level={max(0, min(100, value))/100:.2f}")

    def mute(self, enabled: bool):
        self.run("sndioctl", f"output.mute={int(enabled)}")

    def brightness(self, value: int):
        self.daemon.request("brightness", max(1, min(100, value)))

    def wifi(self, interface: str, enabled: bool):
        self.daemon.request("wifi", interface, "up" if enabled else "down")

    def scan(self, interface: str):
        return parse_scan(self.daemon.request("scan", interface))

    def connect(self, interface: str, ssid: str, password: str | None):
        self.daemon.request("connect", interface, ssid, password or "")

    def device(self, value: str):
        if not re.fullmatch(r"[\w.-]+", value):
            raise SystemError("Invalid sndio device selector")
        self.run("sndioctl", f"server.device={value}")

    def suspend(self):
        self.daemon.request("suspend")

    def power(self, mode):
        self.daemon.request("power", mode)


class Demo:
    """In-memory backend: never invokes host system commands."""
    def __init__(self, config):
        self.state = State(
            networks=[Network("iwm0", True, True, True, "atelier", "192.168.1.24", "82%"),
                      Network("em0", False, False, False)],
            battery=84, ac=False, minutes=312, volume=42, brightness=72)

    def read(self):
        return self.state

    def volume(self, value): self.state.volume = value
    def mute(self, enabled): self.state.muted = enabled
    def brightness(self, value): self.state.brightness = value
    def wifi(self, interface, enabled):
        n = next(n for n in self.state.networks if n.interface == interface)
        n.up = enabled
        n.connected = enabled
    def scan(self, interface):
        return [("atelier", "secured"), ("cwm-lab", "secured"), ("guest", "open / unknown")]
    def connect(self, interface, ssid, password):
        n = next(n for n in self.state.networks if n.interface == interface)
        n.ssid, n.up, n.connected = ssid, True, True
    def device(self, value): pass
    def suspend(self): pass
    def power(self, mode): pass


def backend(config, demo=False):
    if demo:
        return Demo(config)
    if platform.system() != "OpenBSD":
        raise SystemError("Live controls require OpenBSD. Use --demo for the visual preview.")
    return OpenBSD(config)

__version__ = (1, 0, 2)

# meta developer: @bio_misterr
# requires: Pillow psutil
# scope: hikka_only
# scope: hikka_min 1.2.10

import contextlib
import io
import os
import platform
import pty
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

import fcntl
import termios
from typing import List, Optional, Tuple

import psutil
from PIL import Image, ImageDraw, ImageFont
from telethon.tl.types import Message

from .. import loader, utils

RGB = Tuple[int, int, int]
DEFAULT_BG: RGB = (11, 15, 20)
DEFAULT_FG: RGB = (229, 231, 235)
CARD_BG: RGB = (15, 23, 32)
BORDER: RGB = (30, 41, 59)
SHADOW: RGB = (5, 8, 12)

ANSI_16 = {
    0: (0, 0, 0),
    1: (205, 49, 49),
    2: (13, 188, 121),
    3: (229, 229, 16),
    4: (36, 114, 200),
    5: (188, 63, 188),
    6: (17, 168, 205),
    7: (229, 229, 229),
    8: (102, 102, 102),
    9: (241, 76, 76),
    10: (35, 209, 139),
    11: (245, 245, 67),
    12: (59, 142, 234),
    13: (214, 112, 214),
    14: (41, 184, 219),
    15: (255, 255, 255),
}

CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def fmt_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PiB"


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def read_os_name() -> str:
    with contextlib.suppress(Exception):
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            data = f.read()
        match = re.search(r'^PRETTY_NAME="?(.*?)"?$', data, re.M)
        if match:
            return match.group(1)
    with contextlib.suppress(Exception):
        return platform.platform()
    return "Unknown"


def ansi_256_to_rgb(index: int) -> RGB:
    if 0 <= index <= 15:
        return ANSI_16[index]
    if 16 <= index <= 231:
        index -= 16
        r = index // 36
        g = (index % 36) // 6
        b = index % 6
        steps = [0, 95, 135, 175, 215, 255]
        return (steps[r], steps[g], steps[b])
    if 232 <= index <= 255:
        gray = 8 + (index - 232) * 10
        return (gray, gray, gray)
    return DEFAULT_FG


def strip_non_sgr_escapes(text: str) -> str:
    saved_sgr = []

    def _save_sgr(match: re.Match) -> str:
        saved_sgr.append(match.group(0))
        return f"\0SGR{len(saved_sgr) - 1}\0"

    text = SGR_RE.sub(_save_sgr, text)
    text = OSC_RE.sub("", text)
    text = CSI_RE.sub("", text)
    text = re.sub(r"\x1b[()][0-9A-Za-z]", "", text)
    text = re.sub(r"\x1b[@-Z\\^_`]", "", text)

    def _restore_sgr(match: re.Match) -> str:
        return saved_sgr[int(match.group(1))]

    text = re.sub(r"\0SGR(\d+)\0", _restore_sgr, text)
    text = text.replace("\r", "")
    text = text.replace("\t", "    ")
    return text


def _append_plain(
    lines: List[List[Tuple[str, RGB, Optional[RGB]]]],
    plain: str,
    fg: RGB,
    bg: Optional[RGB],
) -> None:
    for ch in plain:
        if ch == "\n":
            lines.append([])
        else:
            lines[-1].append((ch, fg, bg))


def _apply_sgr_codes(codes: List[int], fg: RGB, bg: Optional[RGB]) -> Tuple[RGB, Optional[RGB]]:
    k = 0
    while k < len(codes):
        code = codes[k]
        if code == 0:
            fg = DEFAULT_FG
            bg = None
        elif code == 39:
            fg = DEFAULT_FG
        elif code == 49:
            bg = None
        elif 30 <= code <= 37:
            fg = ANSI_16[code - 30]
        elif 90 <= code <= 97:
            fg = ANSI_16[8 + code - 90]
        elif 40 <= code <= 47:
            bg = ANSI_16[code - 40]
        elif 100 <= code <= 107:
            bg = ANSI_16[8 + code - 100]
        elif code in (38, 48) and k + 1 < len(codes):
            is_fg = code == 38
            mode = codes[k + 1]
            if mode == 5 and k + 2 < len(codes):
                color = ansi_256_to_rgb(codes[k + 2])
                if is_fg:
                    fg = color
                else:
                    bg = color
                k += 2
            elif mode == 2 and k + 4 < len(codes):
                color = (codes[k + 2], codes[k + 3], codes[k + 4])
                if is_fg:
                    fg = color
                else:
                    bg = color
                k += 4
        k += 1

    return fg, bg


def parse_ansi(text: str):
    text = strip_non_sgr_escapes(text)
    lines: List[List[Tuple[str, RGB, Optional[RGB]]]] = [[]]
    fg = DEFAULT_FG
    bg = None
    pos = 0

    for match in SGR_RE.finditer(text):
        _append_plain(lines, text[pos : match.start()], fg, bg)
        params = match.group(1)
        try:
            codes = [int(p) if p else 0 for p in params.split(";")] if params else [0]
        except ValueError:
            codes = [0]
        fg, bg = _apply_sgr_codes(codes, fg, bg)
        pos = match.end()

    _append_plain(lines, text[pos:], fg, bg)

    while lines and not lines[-1]:
        lines.pop()
    return lines or [[(" ", DEFAULT_FG, None)]]


def load_mono_font(size: int = 18):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            with contextlib.suppress(Exception):
                return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _set_pty_size(fd: int, cols: int = 160, rows: int = 48) -> None:
    with contextlib.suppress(Exception):
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def render_terminal_to_png(text: str) -> io.BytesIO:
    font = load_mono_font(18)
    parsed = parse_ansi(text)

    bbox = font.getbbox("M")
    char_w = max(8, bbox[2] - bbox[0])
    line_h = max(18, bbox[3] - bbox[1] + 6)

    max_cols = max((len(line) for line in parsed), default=1)
    pad = 26
    card_pad = 18
    width = max(360, max_cols * char_w + pad * 2 + card_pad * 2)
    height = max(160, len(parsed) * line_h + pad * 2 + card_pad * 2)

    image = Image.new("RGB", (width, height), DEFAULT_BG)
    draw = ImageDraw.Draw(image)

    shadow_box = (14, 16, width - 10, height - 8)
    draw.rounded_rectangle(shadow_box, radius=24, fill=SHADOW)

    card_box = (8, 8, width - 16, height - 16)
    draw.rounded_rectangle(card_box, radius=24, fill=CARD_BG, outline=BORDER, width=1)

    x0 = card_box[0] + card_pad
    y0 = card_box[1] + card_pad

    for row, line in enumerate(parsed):
        y = y0 + row * line_h
        for col, (char, fg, bg) in enumerate(line):
            x = x0 + col * char_w
            if bg is not None:
                draw.rectangle((x, y, x + char_w, y + line_h), fill=bg)
            draw.text((x, y), char, font=font, fill=fg)

    out = io.BytesIO()
    image.save(out, format="PNG")
    out.name = "fetch.png"
    out.seek(0)
    return out


def capture_command_tty(cmd: List[str], timeout: int = 25) -> str:
    master_fd, slave_fd = pty.openpty()
    _set_pty_size(master_fd)
    _set_pty_size(slave_fd)

    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("COLUMNS", "160")
    env.setdefault("LINES", "48")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        text=False,
    )
    os.close(slave_fd)

    chunks = bytearray()
    start = time.time()
    try:
        while True:
            if proc.poll() is not None and time.time() - start > 0.2:
                with contextlib.suppress(OSError):
                    while True:
                        chunk = os.read(master_fd, 4096)
                        if not chunk:
                            break
                        chunks.extend(chunk)
                break

            if time.time() - start > timeout:
                proc.kill()
                break

            with contextlib.suppress(OSError):
                chunk = os.read(master_fd, 4096)
                if chunk:
                    chunks.extend(chunk)
                    continue
            time.sleep(0.03)
    finally:
        with contextlib.suppress(Exception):
            os.close(master_fd)
        with contextlib.suppress(Exception):
            proc.wait(timeout=1)

    return chunks.decode("utf-8", "ignore")


@loader.tds
class ServerFetchMod(loader.Module):
    """Neofetch screenshot + server load for Hikka"""

    strings = {
        "name": "ServerFetch",
        "loading_fetch": '<tg-emoji emoji-id="5368324170671202286">⏳</tg-emoji> <b>Rendering fetch...</b>',
        "loading_ol": '<tg-emoji emoji-id="5368324170671202286">⏳</tg-emoji> <b>Collecting system stats...</b>',
        "fetch_missing": (
            '<tg-emoji emoji-id="5368324170671202286">⚠️</tg-emoji> <b>Neither <code>neofetch</code> nor <code>fastfetch</code> was found.</b>\n'
            "<i>Ubuntu:</i> <code>sudo apt update && sudo apt install neofetch -y</code>"
        ),
        "fetch_failed": '<tg-emoji emoji-id="5368324170671202286">⚠️</tg-emoji> <b>Failed to build fetch screenshot.</b>',
        "ol": (
            '<tg-emoji emoji-id="5368324170671202286">🖥️</tg-emoji> <b>Overall load</b>\n\n'
            '<tg-emoji emoji-id="5368324170671202286">⚙️</tg-emoji> <b>CPU:</b> {cpu_percent}% (<code>{cores}C/{threads}T</code>)\n'
            '<tg-emoji emoji-id="5368324170671202286">📈</tg-emoji> <b>Load avg:</b> {loadavg}\n'
            '<tg-emoji emoji-id="5368324170671202286">🧠</tg-emoji> <b>RAM:</b> {ram_used} / {ram_total} ({ram_percent}%)\n'
            '<tg-emoji emoji-id="5368324170671202286">💾</tg-emoji> <b>Disk /:</b> {disk_used} / {disk_total} ({disk_percent}%)\n'
            '<tg-emoji emoji-id="5368324170671202286">⏱️</tg-emoji> <b>Uptime:</b> {uptime}\n'
            '<tg-emoji emoji-id="5368324170671202286">🐧</tg-emoji> <b>OS:</b> {os_name}\n'
            '<tg-emoji emoji-id="5368324170671202286">🧩</tg-emoji> <b>Kernel:</b> {kernel}\n'
            '<tg-emoji emoji-id="5368324170671202286">🐍</tg-emoji> <b>Python:</b> {python}'
        ),
    }

    strings_ru = {
        "loading_fetch": '<tg-emoji emoji-id="5368324170671202286">⏳</tg-emoji> <b>Рисую fetch...</b>',
        "loading_ol": '<tg-emoji emoji-id="5368324170671202286">⏳</tg-emoji> <b>Собираю статистику...</b>',
        "fetch_missing": (
            '<tg-emoji emoji-id="5368324170671202286">⚠️</tg-emoji> <b>Не найден ни <code>neofetch</code>, ни <code>fastfetch</code>.</b>\n'
            "<i>Для Ubuntu:</i> <code>sudo apt update && sudo apt install neofetch -y</code>"
        ),
        "fetch_failed": '<tg-emoji emoji-id="5368324170671202286">⚠️</tg-emoji> <b>Не удалось собрать скрин fetch.</b>',
        "ol": (
            '<tg-emoji emoji-id="5368324170671202286">🖥️</tg-emoji> <b>Нагрузка сервера</b>\n\n'
            '<tg-emoji emoji-id="5368324170671202286">⚙️</tg-emoji> <b>CPU:</b> {cpu_percent}% (<code>{cores}C/{threads}T</code>)\n'
            '<tg-emoji emoji-id="5368324170671202286">📈</tg-emoji> <b>Load avg:</b> {loadavg}\n'
            '<tg-emoji emoji-id="5368324170671202286">🧠</tg-emoji> <b>RAM:</b> {ram_used} / {ram_total} ({ram_percent}%)\n'
            '<tg-emoji emoji-id="5368324170671202286">💾</tg-emoji> <b>Диск /:</b> {disk_used} / {disk_total} ({disk_percent}%)\n'
            '<tg-emoji emoji-id="5368324170671202286">⏱️</tg-emoji> <b>Аптайм:</b> {uptime}\n'
            '<tg-emoji emoji-id="5368324170671202286">🐧</tg-emoji> <b>OS:</b> {os_name}\n'
            '<tg-emoji emoji-id="5368324170671202286">🧩</tg-emoji> <b>Ядро:</b> {kernel}\n'
            '<tg-emoji emoji-id="5368324170671202286">🐍</tg-emoji> <b>Python:</b> {python}'
        ),
        "_cls_doc": "Скрин neofetch и сводка по нагрузке сервера",
        "_cmd_doc_fetch": "Отправить цветной скрин neofetch/fastfetch",
        "_cmd_doc_ol": "Показать CPU, RAM, диск и аптайм",
    }

    def _pick_fetch_command(self) -> Optional[List[str]]:
        if shutil.which("neofetch"):
            return ["neofetch"]
        if shutil.which("fastfetch"):
            return ["fastfetch"]
        return None

    async def _build_fetch_image(self) -> Optional[io.BytesIO]:
        cmd = self._pick_fetch_command()
        if not cmd:
            return None

        text = await utils.run_sync(capture_command_tty, cmd)
        if not text.strip():
            return None

        return await utils.run_sync(render_terminal_to_png, text)

    def _loadavg(self) -> str:
        with contextlib.suppress(Exception):
            a1, a5, a15 = os.getloadavg()
            return f"{a1:.2f} / {a5:.2f} / {a15:.2f}"
        return "n/a"

    def _overall_stats(self):
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu_percent = psutil.cpu_percent(interval=0.4)
        return {
            "cpu_percent": f"{cpu_percent:.1f}",
            "cores": psutil.cpu_count(logical=False) or 0,
            "threads": psutil.cpu_count(logical=True) or 0,
            "loadavg": self._loadavg(),
            "ram_used": fmt_bytes(vm.used),
            "ram_total": fmt_bytes(vm.total),
            "ram_percent": f"{vm.percent:.1f}",
            "disk_used": fmt_bytes(disk.used),
            "disk_total": fmt_bytes(disk.total),
            "disk_percent": f"{disk.percent:.1f}",
            "uptime": fmt_uptime(time.time() - psutil.boot_time()),
            "host": utils.escape_html(socket.gethostname()),
            "os_name": utils.escape_html(read_os_name()),
            "kernel": utils.escape_html(platform.release()),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }

    @loader.command(ru_doc="Отправить цветной скрин neofetch/fastfetch")
    async def fetch(self, message: Message):
        """Send colored neofetch/fastfetch screenshot"""
        status = await utils.answer(message, self.strings("loading_fetch"))

        if not self._pick_fetch_command():
            await utils.answer(status, self.strings("fetch_missing"))
            return

        image = await self._build_fetch_image()
        if image is None:
            await utils.answer(status, self.strings("fetch_failed"))
            return

        await self._client.send_file(
            message.peer_id,
            image,
            reply_to=getattr(message, "reply_to_msg_id", None),
        )

        with contextlib.suppress(Exception):
            await status.delete()

    @loader.command(ru_doc="Показать CPU, RAM, диск и аптайм")
    async def ol(self, message: Message):
        """Show CPU, RAM, disk and uptime"""
        status = await utils.answer(message, self.strings("loading_ol"))
        stats = await utils.run_sync(self._overall_stats)
        text = self.strings("ol").format(**stats)

        try:
            await self._client.send_message(
                message.peer_id,
                text,
                parse_mode="HTML",
                reply_to=getattr(message, "reply_to_msg_id", None),
            )
        finally:
            with contextlib.suppress(Exception):
                await status.delete()

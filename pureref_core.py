"""
PureRef automation core.

Drives PureRef 2.1+'s official command-line interface (load / save / exportScene /
exportImages) in a separate headless process per call, and adds:
  - scene measurement (scene-coordinate bounds via a marker render)
  - previews with a scene-coordinate grid, optionally zoomed to a region
  - text labels rendered to transparent PNGs
  - URL image downloads
  - safe saving (temp file -> backup original -> replace), refusing scenes open in the GUI

The CLI cannot move, delete or edit existing items, and cannot create native
PureRef text notes. Labels are added as images.

Coordinates are PureRef scene units (1 unit = 1 image pixel at 100% zoom),
x grows right, y grows down. PureRef's `load` places an image's CENTER at (x, y).
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None

EXE_CANDIDATES = [
    r"C:\Program Files\PureRef\PureRef.exe",
    r"C:\Program Files (x86)\PureRef\PureRef.exe",
    r"D:\Program Files\PureRef\PureRef.exe",
]
CACHE_DIR = Path(os.environ.get("PUREREF_MCP_CACHE") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pureref-mcp")
# Relative scene names and pureref_list_scenes resolve against this folder.
DEFAULT_ROOT = Path(os.environ.get("PUREREF_MCP_ROOT") or Path.home() / "Documents" / "PureRef")

# Windows caps a command line at 32767 chars; leave headroom for the exe path and quoting.
MAX_CMDLINE = 28000
MARKER_SIZE = 512
MARKER_RGB = np.array([255, 0, 255], dtype=np.int16)
MAX_RENDER_BOX = 10000
BACKUPS_KEPT = 20
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


try:
    # Subclassing ToolError lets the MCP server pass our message to the client instead of masking it.
    from mcp.server.mcpserver.exceptions import ToolError as _ErrorBase
except ImportError:
    _ErrorBase = RuntimeError


class PureRefError(_ErrorBase):
    pass


@dataclass
class Bounds:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float: return self.max_x - self.min_x

    @property
    def height(self) -> float: return self.max_y - self.min_y

    def as_dict(self) -> dict:
        return {k: round(v) for k, v in
                dict(min_x=self.min_x, min_y=self.min_y, max_x=self.max_x, max_y=self.max_y,
                     width=self.width, height=self.height).items()}


# ------------------------------------------------------------------ helpers

def cache(sub: str) -> Path:
    p = CACHE_DIR / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_exe() -> str:
    env = os.environ.get("PUREREF_EXE")
    for cand in ([env] if env else []) + EXE_CANDIDATES:
        if cand and Path(cand).is_file():
            return cand
    found = shutil.which("PureRef")
    if found:
        return found
    raise PureRefError("PureRef.exe not found. Set the PUREREF_EXE environment variable.")


def resolve_scene(scene: str | os.PathLike) -> Path:
    p = Path(scene)
    if not p.is_absolute():
        p = DEFAULT_ROOT / p
    if p.suffix.lower() != ".pur":
        p = p.with_suffix(".pur")
    return p.resolve()


def cli_path(p: str | os.PathLike) -> str:
    s = str(Path(p).resolve()).replace("\\", "/")
    # The CLI splits command arguments on ';' with no escape mechanism.
    if ";" in s:
        raise PureRefError(f"Path contains ';', which the PureRef CLI cannot handle: {s}")
    return s


def load_arg(p: str | os.PathLike) -> str:
    # `load` takes a comma-separated list; literal commas are escaped by doubling.
    return cli_path(p).replace(",", ",,")


def run(commands: list[str], timeout: float = 900) -> list[str]:
    """Run commands in a fresh PureRef process (never touches an open GUI instance)."""
    args = [find_exe()]
    for c in [*commands, "exit"]:
        args += ["-c", c]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired as e:
        raise PureRefError(f"PureRef timed out after {timeout:.0f}s") from e
    lines = [ln.rstrip() for ln in (r.stdout + "\n" + r.stderr).splitlines() if ln.strip()]
    critical = [ln for ln in lines if ln.startswith("[Critical]")]
    if critical or r.returncode != 0:
        raise PureRefError("PureRef reported errors:\n" + "\n".join(critical or lines[-15:]))
    return [ln for ln in lines if ln.startswith("[Warning]")]


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def run_save(commands: list[str], save_path: Path, timeout: float = 900) -> list[str]:
    """Run commands then `save`, waiting for the save to land.

    PureRef's `save` finishes asynchronously, and a trailing `exit` kills it mid-write
    (leaving an empty file). So we omit `exit`, watch for the file to be replaced
    (Qt saves via temp file + rename), then end the process ourselves.
    """
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(b"")  # CLI quirk: save fails if the path doesn't exist yet
    before = save_path.stat().st_mtime_ns
    args = [find_exe()]
    for c in [*commands, f"save;{cli_path(save_path)}"]:
        args += ["-c", c]
    log = new_temp(".log")
    pr_tmp_before = _pureref_tmp_files()
    with open(log, "wb") as fh:
        proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT, startupinfo=_hidden_startupinfo(),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.time() + timeout
    try:
        while True:
            time.sleep(0.25)
            text = log.read_text("utf-8", "replace")
            if "[Critical]" in text:
                raise PureRefError("PureRef reported errors:\n" +
                                   "\n".join(ln for ln in text.splitlines() if ln.startswith("[Critical]")))
            st = save_path.stat() if save_path.exists() else None
            if st and st.st_size > 0 and st.st_mtime_ns != before:
                time.sleep(0.75)
                if save_path.stat().st_size == st.st_size:
                    break
            if proc.poll() is not None:
                raise PureRefError("PureRef exited before the save completed:\n" + text[-1500:])
            if time.time() > deadline:
                raise PureRefError(f"PureRef save timed out after {timeout:.0f}s")
    finally:
        _kill_tree(proc)
        _clean_pureref_tmp(pr_tmp_before)
        warnings = [ln for ln in log.read_text("utf-8", "replace").splitlines() if ln.startswith("[Warning]")]
        _unlink_quietly(log)
    return warnings


def _kill_tree(proc: subprocess.Popen) -> None:
    """End PureRef and its crashpad_handler child (which inherits our log handle)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


PUREREF_TMP = Path(os.environ.get("TEMP", "")) / "PureRef"


def _pureref_tmp_files() -> set[str]:
    return {f.name for f in PUREREF_TMP.iterdir()} if PUREREF_TMP.is_dir() else set()


def _clean_pureref_tmp(before: set[str]) -> None:
    """A killed instance leaves its autosave/image cache in %TEMP%\PureRef. Remove only files that
    appeared during our run, and never those of a session that was already running (a live GUI)."""
    new = _pureref_tmp_files() - before
    # A killed instance leaves its own .lock behind, so only locks that predate our run mark a live GUI.
    locked = {n.split(".")[0] for n in before if n.endswith(".lock")}
    for name in new:
        if name.split("_")[0].split(".")[0] in locked:
            continue
        f = PUREREF_TMP / name
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            _unlink_quietly(f)


def _unlink_quietly(path: Path, tries: int = 20) -> None:
    """Delete a temp file, retrying while a just-killed process releases it; leftovers are
    swept by clean_tmp()."""
    for _ in range(tries):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)


def clean_tmp(max_age_s: float = 3600) -> None:
    """Remove stale temp files from earlier runs."""
    cutoff = time.time() - max_age_s
    for f in cache("tmp").iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def open_in_gui() -> list[str]:
    """Window titles of running PureRef GUI instances ("<name> - PureRef - <path>")."""
    if os.name != "nt":
        return []
    r = subprocess.run(["tasklist", "/v", "/fo", "csv", "/fi", "imagename eq PureRef.exe"],
                       capture_output=True, text=True, errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    rows = list(csv.reader(io.StringIO(r.stdout)))
    return [row[-1] for row in rows[1:] if len(row) > 1 and row[-1] not in ("N/A", "")]


def scene_is_open(scene: Path) -> bool:
    needle = str(scene).replace("/", "\\").lower()
    return any(needle in t.replace("/", "\\").lower() for t in open_in_gui())


def backup(scene: Path) -> Path | None:
    if not scene.exists():
        return None
    dest_dir = cache("backups")
    dest = dest_dir / f"{scene.stem}_{time.strftime('%Y%m%d-%H%M%S')}.pur"
    shutil.copy2(scene, dest)
    olds = sorted(dest_dir.glob(f"{scene.stem}_*.pur"))
    for old in olds[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return dest


def new_temp(suffix: str, sub: str = "tmp") -> Path:
    return cache(sub) / f"{uuid.uuid4().hex}{suffix}"


# ------------------------------------------------------------------ reading

def list_scenes(root: str | os.PathLike | None = None) -> list[dict]:
    base = Path(root) if root else DEFAULT_ROOT
    out = []
    for p in sorted(base.rglob("*.pur")):
        st = p.stat()
        out.append({"path": str(p), "mb": round(st.st_size / 1e6, 1),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def scene_sources(scene: Path) -> dict:
    """Best-effort parse of image source paths/URLs stored in a PureRef 2.x scene."""
    data = scene.read_bytes()
    version = ""
    try:
        n = int.from_bytes(data[0:4], "big")
        version = data[4:4 + n].decode("utf-16-be")
    except Exception:
        pass
    sources: list[str] = []
    for m in re.finditer(rb"\x02\x02((?:[A-Za-z]:/|https?://).+?)(?:JPG|PNG|WEBP|GIF|BMP|TIF)", data):
        raw = m.group(1)
        path = raw[: len(raw) // 2].decode("utf-8", "replace")  # source is stored twice back-to-back
        if path not in sources:
            sources.append(path)
    local = [s for s in sources if not s.startswith("http")]
    return {
        "version": version,
        "file_mb": round(len(data) / 1e6, 1),
        "sources": len(sources),
        "local": [{"path": s, "exists": Path(s).exists()} for s in local],
        "web": [s for s in sources if s.startswith("http")],
    }


def _marker() -> Path:
    p = cache("assets") / f"_marker_{MARKER_SIZE}.png"
    if not p.exists():
        Image.new("RGB", (MARKER_SIZE, MARKER_SIZE), tuple(int(c) for c in MARKER_RGB)).save(p)
    return p


def _bounds_from_marker_render(path: Path) -> Bounds | None:
    im = np.asarray(Image.open(path).convert("RGBA"))
    rgb, alpha = im[..., :3].astype(np.int16), im[..., 3]
    magenta = (np.abs(rgb - MARKER_RGB).sum(-1) < 60) & (alpha > 200)
    rows, cols = magenta.sum(1), magenta.sum(0)
    if rows.max(initial=0) == 0:
        raise PureRefError("Measurement marker not found in render.")
    # Dense rows/cols only, so stray magenta pixels in references don't skew the marker box.
    ry = np.nonzero(rows >= rows.max() * 0.5)[0]
    cx = np.nonzero(cols >= cols.max() * 0.5)[0]
    mx0, mx1, my0, my1 = cx[0], cx[-1] + 1, ry[0], ry[-1] + 1
    scale = ((mx1 - mx0) + (my1 - my0)) / (2 * MARKER_SIZE)

    content = alpha > 0
    # Pad the cut-out: the marker's anti-aliased edge pixels fail the magenta test but aren't content.
    pad = 3
    content[max(0, my0 - pad):my1 + pad, max(0, mx0 - pad):mx1 + pad] = False
    any_x, any_y = content.any(0), content.any(1)
    if not any_x.any():
        return None
    px0, px1 = int(np.argmax(any_x)), len(any_x) - int(np.argmax(any_x[::-1]))
    py0, py1 = int(np.argmax(any_y)), len(any_y) - int(np.argmax(any_y[::-1]))

    def sx(px): return (px - mx0) / scale - MARKER_SIZE / 2
    def sy(py): return (py - my0) / scale - MARKER_SIZE / 2
    return Bounds(sx(px0), sy(py0), sx(px1), sy(py1))


def _content_bbox(path: Path) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    im = Image.open(path).convert("RGBA")
    return im, im.getchannel("A").getbbox()


def view_scene(scene: Path, max_px: int = 1600, region: list[float] | None = None,
               grid: bool = True) -> tuple[Path, Bounds | None, Bounds | None]:
    """Render a preview JPEG. Returns (preview path, full scene bounds, shown region)."""
    measure_png, view_png = new_temp(".png"), new_temp(".png")
    commands = [f"load;{load_arg(scene)}"]
    box = max_px
    if region:
        # Render the whole scene large enough that the region alone fills ~max_px, then crop.
        # Needs bounds first, so this path costs one extra PureRef launch.
        full = measure(scene)
        if full is None:
            raise PureRefError("Scene is empty.")
        rw, rh = max(1.0, region[2] - region[0]), max(1.0, region[3] - region[1])
        box = int(min(MAX_RENDER_BOX, max_px * max(full.width / rw, full.height / rh)))
    commands += [f"exportScene;{cli_path(view_png)};{box};{box};false;true",
                 f"load;{load_arg(_marker())};0;0",
                 f"exportScene;{cli_path(measure_png)};4000;4000;false;false"]
    try:
        run(commands)
        bounds = _bounds_from_marker_render(measure_png)
        im, bbox = _content_bbox(view_png)
    finally:
        measure_png.unlink(missing_ok=True)
    view_png.unlink(missing_ok=True)
    if bounds is None or bbox is None:
        raise PureRefError("Scene is empty.")

    im = im.crop(bbox)
    scale = im.width / max(1.0, bounds.width)  # preview px per scene unit
    shown = bounds
    if region:
        x0, y0 = (region[0] - bounds.min_x) * scale, (region[1] - bounds.min_y) * scale
        x1, y1 = (region[2] - bounds.min_x) * scale, (region[3] - bounds.min_y) * scale
        im = im.crop((int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1))))
        shown = Bounds(*region)
    bg = Image.new("RGBA", im.size, (29, 29, 29, 255))
    bg.alpha_composite(im)
    out = bg.convert("RGB")
    if max(out.size) > max_px:
        f = max_px / max(out.size)
        out = out.resize((max(1, int(out.width * f)), max(1, int(out.height * f))), Image.LANCZOS)
    if grid:
        out = _draw_grid(out, shown)
    path = cache("renders") / f"view_{scene.stem}.jpg"
    out.save(path, quality=88)
    return path, bounds, shown


def _nice_step(span: float, target_lines: int = 8) -> float:
    raw = span / target_lines
    mag = 10 ** math.floor(math.log10(max(raw, 1e-9)))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _draw_grid(im: Image.Image, b: Bounds) -> Image.Image:
    im = im.copy()
    d = ImageDraw.Draw(im, "RGBA")
    sx, sy = im.width / max(1.0, b.width), im.height / max(1.0, b.height)
    step = _nice_step(max(b.width, b.height))
    font = _font(max(11, im.width // 110), bold=False)
    x = math.ceil(b.min_x / step) * step
    while x <= b.max_x:
        px = (x - b.min_x) * sx
        d.line([(px, 0), (px, im.height)], fill=(0, 255, 255, 90 if x else 200), width=1)
        d.text((px + 3, 2), f"x={x:g}", fill=(0, 255, 255, 230), font=font)
        x += step
    y = math.ceil(b.min_y / step) * step
    while y <= b.max_y:
        py = (y - b.min_y) * sy
        d.line([(0, py), (im.width, py)], fill=(255, 220, 0, 90 if y else 200), width=1)
        d.text((3, py + 2), f"y={y:g}", fill=(255, 220, 0, 230), font=font)
        y += step
    return im


def measure(scene: Path) -> Bounds | None:
    png = new_temp(".png")
    try:
        run([f"load;{load_arg(scene)}", f"load;{load_arg(_marker())};0;0",
             f"exportScene;{cli_path(png)};4000;4000;false;false"])
        return _bounds_from_marker_render(png)
    finally:
        png.unlink(missing_ok=True)


def export_images(scene: Path, out_dir: Path, crop: bool = True, name_template: str = "") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time() - 2
    cmd = f"exportImages;{cli_path(out_dir)};{'true' if crop else 'false'}"
    if name_template:
        if ";" in name_template:
            raise PureRefError("name_template cannot contain ';'")
        cmd += f";{name_template}"
    run([f"load;{load_arg(scene)}", cmd])
    # PureRef overwrites same-named files, so "written by this call" is decided by mtime.
    return sorted(p for p in out_dir.iterdir() if p.is_file() and p.stat().st_mtime >= started)


def contact_sheet(files: list[Path], start: int = 0, thumb: int = 256, cols: int = 6) -> Path:
    files = files[start:]
    rows = max(1, math.ceil(len(files) / cols))
    label_h = 18
    sheet = Image.new("RGB", (cols * thumb, rows * (thumb + label_h)), (29, 29, 29))
    d = ImageDraw.Draw(sheet)
    font = _font(12, bold=False)
    for i, f in enumerate(files):
        x, y = (i % cols) * thumb, (i // cols) * (thumb + label_h)
        try:
            im = Image.open(f)
            im.thumbnail((thumb - 6, thumb - 6))
            im = im.convert("RGBA")
            sheet.paste(im, (x + (thumb - im.width) // 2, y + (thumb - im.height) // 2), im)
        except Exception:
            d.text((x + 6, y + thumb // 2), "unreadable", fill=(255, 80, 80), font=font)
        d.text((x + 4, y + thumb + 2), f"{start + i}: {f.stem[:30]}", fill=(220, 220, 220), font=font)
    path = cache("renders") / f"sheet_{uuid.uuid4().hex[:8]}.jpg"
    sheet.save(path, quality=85)
    return path


# ------------------------------------------------------------------ writing

def _font(size: int, bold: bool = True, name: str | None = None) -> ImageFont.FreeTypeFont:
    candidates = [name] if name else []
    candidates += ["arialbd.ttf", "segoeuib.ttf"] if bold else ["arial.ttf", "segoeui.ttf"]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _rgba(color: str | None, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if not color:
        return default
    c = color.lstrip("#")
    if len(c) == 6:
        c += "ff"
    if len(c) != 8:
        raise PureRefError(f"Bad color '{color}', use #RRGGBB or #RRGGBBAA")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]


def render_label(text: str, font_size: int = 96, color: str = "#ffffff", background: str | None = None,
                 padding: int | None = None, max_width: int | None = None, bold: bool = True,
                 font: str | None = None) -> Path:
    key = hashlib.sha1(repr((text, font_size, color, background, padding, max_width, bold, font)).encode()).hexdigest()[:16]
    path = cache("assets") / f"label_{key}.png"
    if path.exists():
        return path
    f = _font(font_size, bold, font)
    pad = padding if padding is not None else max(8, font_size // 4)
    lines: list[str] = []
    for para in text.split("\n"):
        if max_width:
            avg = max(1, f.getlength("abcdefghijklmnopqrstuvwxyz") / 26)
            lines += textwrap.wrap(para, width=max(1, int(max_width / avg))) or [""]
        else:
            lines.append(para)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    spacing = font_size // 5
    box = probe.multiline_textbbox((0, 0), "\n".join(lines), font=f, spacing=spacing)
    w, h = box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
    im = Image.new("RGBA", (w, h), _rgba(background, (0, 0, 0, 0)))
    ImageDraw.Draw(im).multiline_text((pad - box[0], pad - box[1]), "\n".join(lines), font=f,
                                      fill=_rgba(color, (255, 255, 255, 255)), spacing=spacing)
    im.save(path)
    return path


USER_AGENT = "pureref-mcp/1.0 (reference board builder; PureRef CLI automation)"


def http_get(url: str, timeout: float = 60, retries: int = 4) -> bytes:
    """GET with a polite User-Agent, backing off on 429/5xx (honours Retry-After)."""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                        timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise PureRefError(f"HTTP {e.code} ({e.reason}) fetching {url}") from e
            wait = e.headers.get("Retry-After")
            time.sleep(min(60.0, float(wait)) if wait and wait.isdigit() else 3.0 * 2 ** attempt)
        except urllib.error.URLError as e:
            if attempt == retries:
                raise PureRefError(f"Could not fetch {url}: {e.reason}") from e
            time.sleep(2.0 * 2 ** attempt)
    raise AssertionError("unreachable")


def fetch_image(src: str, max_size: int | None = None) -> Path:
    """Local path or URL -> a local file safe to pass to the CLI (optionally downscaled copy)."""
    if re.match(r"https?://", src):
        ext = Path(src.split("?")[0]).suffix.lower()
        path = cache("downloads") / (hashlib.sha1(src.encode()).hexdigest()[:20] + (ext if ext in IMAGE_EXTS else ".img"))
        if not path.exists():
            data = http_get(src)
            Image.open(io.BytesIO(data)).verify()
            path.write_bytes(data)
    else:
        path = Path(src).resolve()
        if not path.is_file():
            raise PureRefError(f"Image not found: {src}")
    if ";" in str(path):
        safe = cache("assets") / (hashlib.sha1(str(path).encode()).hexdigest()[:16] + path.suffix)
        shutil.copy2(path, safe)
        path = safe
    if max_size:
        with Image.open(path) as im:
            if max(im.size) > max_size:
                im = im.copy()
                im.thumbnail((max_size, max_size), Image.LANCZOS)
                small = cache("assets") / f"{path.stem}_{max_size}.png"
                im.save(small)
                path = small
    return path


def _image_size(p: Path) -> tuple[int, int]:
    with Image.open(p) as im:
        return im.size


def place_item(path: Path, x: float, y: float, anchor: str = "center") -> str:
    if anchor == "top_left":
        w, h = _image_size(path)
        x, y = x + w / 2, y + h / 2
    elif anchor != "center":
        raise PureRefError("anchor must be 'center' or 'top_left'")
    # The CLI rejects non-integer coordinates ("Failed to parse argument '-3276.0'").
    return f"load;{load_arg(path)};{round(x)};{round(y)}"


def write_scene(target: Path, load_cmds: list[str], base: Path | None, allow_open: bool = False) -> dict:
    """Load `base` (if any), apply load commands, save to `target` safely."""
    if scene_is_open(target) and not allow_open:
        raise PureRefError(
            f"{target.name} is open in PureRef. Close it there first (otherwise your next save in the GUI "
            f"would overwrite these changes), or write to a different output path.")
    exe_len = len(find_exe()) + 64
    batches: list[list[str]] = [[]]
    size = 0
    for c in load_cmds:
        if size + len(c) + 8 > MAX_CMDLINE - exe_len and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(c)
        size += len(c) + 8
    warnings: list[str] = []
    staged: list[Path] = []
    try:
        source = base
        for batch in batches:
            # Each launch loads the previous launch's output; long item lists exceed one command line.
            out = new_temp(".pur")
            staged.append(out)
            prefix = [f"load;{load_arg(source)}"] if source else []
            warnings += run_save(prefix + batch, out)
            source = out
        backup_path = backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    finally:
        for p in staged:
            _unlink_quietly(p)
    return {"saved": str(target), "backup": str(backup_path) if backup_path else None,
            "items_added": len(load_cmds), "launches": len(batches), "warnings": warnings}


def open_gui(scene: Path) -> None:
    subprocess.Popen([find_exe(), str(scene)], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

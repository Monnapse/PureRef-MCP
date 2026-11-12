"""
PureRef MCP server.

Lets an agent see and build PureRef boards through PureRef 2.1+'s command-line
interface. Each call runs PureRef as a separate headless process, so an open
PureRef window is never touched. See pureref_core.py for the details.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

import pureref_core as core

INSTRUCTIONS = """\
Tools for viewing and building PureRef (.pur) reference boards.

Coordinates: PureRef scene units (1 unit = 1 image pixel at 100% zoom). x grows right, y grows down.
Image positions are CENTERS unless anchor="top_left".

Workflow:
1. pureref_list_scenes, then pureref_view_scene to see the board with a scene-coordinate grid
   (cyan x= lines, yellow y= lines). Use region=[x0,y0,x1,y1] to zoom in.
2. pureref_view_images for a numbered contact sheet. Export full-res copies with
   pureref_export_images and Read them for detail.
3. Finding images: any image URL or local file works, so pass in results from web or image-search tools.
4. pureref_build_board lays out titled sections of paths / URLs in one launch (new scene, or
   appended below existing content). pureref_add_items / pureref_add_image_grid place items exactly.
   Writing a scene that is open in the PureRef GUI is refused unless allow_open=true.
   The original is backed up first, and the tool result shows the backup path.

Limits (PureRef CLI): existing items cannot be moved, resized, deleted or relabeled. Text labels are
images, not native PureRef notes. The user must reopen a scene in PureRef to see changes.
"""

mcp = MCPServer("pureref", instructions=INSTRUCTIONS)


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _scene(scene: str, must_exist: bool = True) -> Path:
    p = core.resolve_scene(scene)
    if must_exist and not p.exists():
        raise core.PureRefError(f"Scene not found: {p}")
    return p


@mcp.tool()
def pureref_list_scenes(root: str = "") -> str:
    """List .pur scenes under a folder (default: the PUREREF_MCP_ROOT folder)."""
    return _j({"root": str(Path(root) if root else core.DEFAULT_ROOT), "scenes": core.list_scenes(root or None)})


@mcp.tool()
def pureref_scene_info(scene: str) -> str:
    """Quick scene summary without launching PureRef: format version and every image's source
    (local file path and whether it still exists, or the web URL it was dragged in from)."""
    return _j(core.scene_sources(_scene(scene)))


@mcp.tool()
def pureref_view_scene(scene: str, max_px: int = 1600, region: list[float] | None = None,
                       grid: bool = True) -> list[str | Image]:
    """Render the board as an image with a scene-coordinate grid overlay, and return its bounds.

    scene: path to a .pur (absolute, or relative to the PureRef folder).
    max_px: longest side of the returned preview.
    region: optional [min_x, min_y, max_x, max_y] in scene units to zoom into.
    grid: draw x=/y= coordinate lines so you can pick positions for new items.
    """
    path, bounds, shown = core.view_scene(_scene(scene), max_px=max_px, region=region, grid=grid)
    info = {"scene": str(_scene(scene)), "scene_bounds": bounds.as_dict() if bounds else None,
            "shown_region": shown.as_dict() if shown else None, "preview_file": str(path)}
    return [_j(info), Image(path=path)]


@mcp.tool()
def pureref_view_images(scene: str, start: int = 0, count: int = 36, thumb: int = 240) -> list[str | Image]:
    """Export the scene's images (cached) and return a numbered contact sheet, plus the paths of the
    full-resolution PNGs so you can Read individual images."""
    p = _scene(scene)
    out = core.cache("exports") / f"{p.stem}_{int(p.stat().st_mtime)}"
    files = sorted(out.glob("*.png")) if out.exists() else []
    if not files:
        files = core.export_images(p, out, crop=True, name_template="%2_%0")
    page = files[start:start + count]
    sheet = core.contact_sheet(page, start=0, thumb=thumb)
    info = {"total_images": len(files), "showing": f"{start}..{start + len(page) - 1}",
            "files": [f"{start + i}: {f}" for i, f in enumerate(page)]}
    return [_j(info), Image(path=sheet)]


@mcp.tool()
def pureref_export_images(scene: str, out_dir: str, crop: bool = True, name_template: str = "") -> str:
    """Export every image in the scene to individual PNGs in out_dir (created if missing).

    crop: apply the crops set in PureRef (false = original, unmodified images).
    name_template: %0 original name, %1 sequence number, %2 zero-padded sequence, %3 scene name.
                   Empty = original names.
    """
    files = core.export_images(_scene(scene), Path(out_dir), crop=crop, name_template=name_template)
    return _j({"exported": len(files), "files": [str(f) for f in files]})


@mcp.tool()
def pureref_render_png(scene: str, out_path: str, width: int = -1, height: int = -1,
                       background: bool = True, borders: bool = True) -> str:
    """Export the whole board to a PNG (PureRef's own exportScene). -1 = native scene size
    (can be huge). If both width and height are given, the board is fitted inside that box."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    core.run([f"load;{core.load_arg(_scene(scene))}",
              f"exportScene;{core.cli_path(out)};{width};{height};{str(background).lower()};{str(borders).lower()}"])
    return _j({"saved": str(out)})


def _build_loads(items: list[dict]) -> list[str]:
    cmds: list[str] = []
    for i, it in enumerate(items):
        if "x" not in it or "y" not in it:
            raise core.PureRefError(f"item {i}: x and y are required")
        anchor = it.get("anchor", "center")
        if it.get("text") is not None:
            path = core.render_label(str(it["text"]), font_size=int(it.get("font_size", 96)),
                                     color=it.get("color", "#ffffff"), background=it.get("background"),
                                     padding=it.get("padding"), max_width=it.get("max_width"),
                                     bold=bool(it.get("bold", True)), font=it.get("font"))
        elif it.get("image"):
            path = core.fetch_image(str(it["image"]), max_size=it.get("max_size"))
        else:
            raise core.PureRefError(f"item {i}: needs 'image' or 'text'")
        cmds.append(core.place_item(path, float(it["x"]), float(it["y"]), anchor))
    return cmds


def _grid_items(images: list[str], x: float, y: float, columns: int, cell: int, gap: int,
                title: str = "", title_size: int = 120) -> tuple[list[dict], dict]:
    """Lay images out on a cell grid under an optional title. Returns (items, bounds)."""
    columns = max(1, columns)
    items: list[dict] = []
    top = y
    if title:
        items.append({"text": title, "x": x, "y": y, "anchor": "top_left", "font_size": title_size})
        top = y + title_size * 1.6 + gap
    for i, img in enumerate(images):
        r, c = divmod(i, columns)
        items.append({"image": img, "max_size": cell,
                      "x": x + c * (cell + gap) + cell / 2, "y": top + r * (cell + gap) + cell / 2})
    rows = -(-len(images) // columns)
    used_cols = min(columns, max(1, len(images)))
    return items, {"min_x": x, "min_y": y, "max_x": x + used_cols * (cell + gap) - gap,
                   "max_y": top + rows * (cell + gap) - gap}


def _write(scene: str, output: str, items: list[dict], allow_open: bool) -> dict:
    src = _scene(scene, must_exist=False)
    target = core.resolve_scene(output) if output else src
    result = core.write_scene(target, _build_loads(items), base=src if src.exists() else None, allow_open=allow_open)
    result["note"] = "Reopen the scene in PureRef to see the changes (or use pureref_open)."
    return result


@mcp.tool()
def pureref_add_items(scene: str, items: list[dict], output: str = "", allow_open: bool = False) -> str:
    """Add images and text labels to a scene (creates the scene if it doesn't exist).

    items: list of objects, each either
      {"image": "<local path or http(s) URL>", "x": 0, "y": 0, "anchor": "center"|"top_left", "max_size": 2048?}
      {"text": "LOBBY", "x": 0, "y": -600, "anchor": "center"|"top_left", "font_size": 96,
       "color": "#ffffff", "background": "#000000aa"?, "padding": 24?, "max_width": 1200?, "bold": true}
      max_size downsizes a copy of an image so its longest side fits; max_width wraps label text.
    output: save to this .pur instead of overwriting `scene`.
    allow_open: write even if the target scene is open in the PureRef GUI (the GUI won't show the
                changes, and saving there will overwrite them).
    """
    return _j(_write(scene, output, items, allow_open))


@mcp.tool()
def pureref_add_image_grid(scene: str, images: list[str], x: float, y: float, columns: int = 4,
                           cell: int = 800, gap: int = 60, title: str = "", title_size: int = 120,
                           output: str = "", allow_open: bool = False) -> str:
    """Add many images (local paths or URLs) as a tidy grid. (x, y) is the grid's top-left corner.
    Each image is downscaled (copy only) to fit a `cell`x`cell` square and centered in its cell. An
    optional title label goes above the grid. Use pureref_view_scene first to find empty space."""
    items, bounds = _grid_items(images, x, y, columns, cell, gap, title, title_size)
    result = _write(scene, output, items, allow_open)
    result["grid_bounds"] = bounds
    return _j(result)


@mcp.tool()
def pureref_build_board(scene: str, sections: list[dict], title: str = "", subtitle: str = "",
                        columns: int = 4, cell: int = 700, gap: int = 50, section_columns: int = 2,
                        output: str = "", allow_open: bool = False) -> str:
    """Build a whole titled board (or append to an existing one) in a single PureRef launch.

    sections: [{"title": "CONTROL ROOM", "images": ["C:/path.jpg", "https://..."],
                "columns": 4?}, ...]
    Sections are laid out `section_columns` across, each a grid of `cell`-sized slots. On an existing
    scene the new block goes below the current content. For finding images on any subject, use the
    reference-anything MCP (search_references / get_asset) and pass its image URLs or downloaded files.
    """
    if not sections:
        raise core.PureRefError("sections is empty")
    src = _scene(scene, must_exist=False)
    x0, y = 0.0, 0.0
    if src.exists():
        b = core.measure(src)
        if b:
            x0, y = float(b.min_x), float(b.max_y + 600)
    items: list[dict] = []
    if title:
        items.append({"text": title, "x": x0, "y": y, "anchor": "top_left", "font_size": 220})
        y += 220 * 1.45
    if subtitle:
        items.append({"text": subtitle, "x": x0, "y": y, "anchor": "top_left", "font_size": 60,
                      "color": "#aaaaaa", "bold": False, "max_width": 4000})
        y += 60 * 1.6
    if title or subtitle:
        y += 250
    per_row = max(1, section_columns)
    widest = max(min(int(s.get("columns", columns)), max(1, len(s.get("images", [])))) for s in sections)
    col_w = widest * (cell + gap) - gap + 450
    layout = []
    for row_start in range(0, len(sections), per_row):
        row_bottom = y
        for k, sec in enumerate(sections[row_start:row_start + per_row]):
            imgs = [str(i) for i in sec.get("images", [])]
            sec_items, bounds = _grid_items(imgs, x0 + k * col_w, y, int(sec.get("columns", columns)), cell, gap,
                                            str(sec.get("title", "")), 120)
            items += sec_items
            layout.append({"section": sec.get("title", ""), "images": len(imgs), "bounds": bounds})
            row_bottom = max(row_bottom, bounds["max_y"])
        y = row_bottom + 500
    result = _write(scene, output, items, allow_open)
    result["sections"] = layout
    return _j(result)


@mcp.tool()
def pureref_open(scene: str) -> str:
    """Open a scene in the PureRef GUI for the user."""
    p = _scene(scene)
    core.open_gui(p)
    return f"Opened {p}"


@mcp.tool()
def pureref_list_backups(scene: str = "") -> str:
    """List the automatic backups taken before scenes were overwritten (newest last)."""
    stem = core.resolve_scene(scene).stem if scene else ""
    files = sorted(core.cache("backups").glob(f"{stem}_*.pur" if stem else "*.pur"))
    return _j([str(f) for f in files])


if __name__ == "__main__":
    core.clean_tmp()
    mcp.run()

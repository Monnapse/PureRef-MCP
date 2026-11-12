# PureRef MCP

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Claude Desktop, or any MCP client) **see and build [PureRef](https://www.pureref.com) reference boards**.

It drives PureRef 2.1+'s official command-line interface. Each call runs PureRef as a separate headless process, so an open PureRef window is never touched.

## What it can do

| Tool | What it does |
|---|---|
| `pureref_list_scenes` | Lists the `.pur` boards in a folder |
| `pureref_scene_info` | Shows where each image on a board came from (local file or web URL) without launching PureRef |
| `pureref_view_scene` | Renders the board with a scene-coordinate grid, optionally zoomed to a region |
| `pureref_view_images` | Shows a numbered contact sheet of the board's images |
| `pureref_export_images` | Exports full-resolution copies of the images |
| `pureref_render_png` | Renders the board to a PNG |
| `pureref_add_items` | Places images (paths or URLs) and text labels at exact positions |
| `pureref_add_image_grid` | Places a grid of images |
| `pureref_build_board` | Lays out a titled board from sections of images in one go |
| `pureref_open` | Opens a board in the PureRef app |
| `pureref_list_backups` | Lists the automatic backups of a board |

### How it stays safe

- **Backups:** before a board is written, the original is backed up. The last 20 backups per board are kept in `%LOCALAPPDATA%\pureref-mcp\backups`.
- **Open boards:** writing a board that's open in the PureRef window is refused unless `allow_open=true`.
- **Atomic saves:** saves go to a temp file first, then replace the original.

### Limits (from PureRef's CLI)

- It **can't move, resize, delete or relabel items that are already on a board**. It can only add new ones.
- Text labels are added as images, not native PureRef notes.
- Reopen the board in PureRef to see the changes.

## Requirements

- Windows. The CLI and exe lookup are only tested there.
- [PureRef](https://www.pureref.com) 2.1 or newer.
- Python 3.11+.

## Setup

1. Install the Python packages:
   ```
   pip install -r requirements.txt
   ```
2. Register the server with your MCP client. For Claude Code:
   ```
   claude mcp add pureref -s user -e PUREREF_MCP_ROOT="C:\path\to\your\boards" -- python "C:\path\to\PureRef-MCP\server.py"
   ```
   For Claude Desktop, or any client that uses an `mcpServers` JSON config:
   ```json
   {
     "mcpServers": {
       "pureref": {
         "command": "python",
         "args": ["C:\\path\\to\\PureRef-MCP\\server.py"],
         "env": { "PUREREF_MCP_ROOT": "C:\\path\\to\\your\\boards" }
       }
     }
   }
   ```

## Settings (environment variables)

| Variable | Default | What it's for |
|---|---|---|
| `PUREREF_MCP_ROOT` | `~/Documents/PureRef` | The folder that relative board names and `pureref_list_scenes` use |
| `PUREREF_EXE` | Looks in `Program Files` | Path to `PureRef.exe`, if it's installed somewhere else |
| `PUREREF_MCP_CACHE` | `%LOCALAPPDATA%\pureref-mcp` | Where previews, downloads and backups go |

## Coordinates

- Positions are in PureRef scene units: 1 unit is 1 image pixel at 100% zoom.
- x grows to the right and y grows downward.
- Image positions are **centers** unless you pass `anchor="top_left"`.
- `pureref_view_scene` draws the grid (cyan `x=` lines, yellow `y=` lines), so the agent can read positions off the preview.

## License

MIT, see [LICENSE](LICENSE). PureRef is a product of Idyllic Pixel AB. This project isn't affiliated with or endorsed by them.

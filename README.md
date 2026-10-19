# snip2tex-mac

Lasso handwriting on a reMarkable Paper Pro, tap a button, get the result from a local vision model running on a Mac.

## Spec

| | |
|---|---|
| Tablet | reMarkable Paper Pro, firmware 3.27.3.0, developer mode, xovi |
| Computer | Apple Silicon Mac, macOS 14+ |
| Model | Gemma 4 vision (MLX), in-process via mlx-vlm. Default E2B (~4 GB). Any MLX vision model in `~/.lmstudio/models` works |
| Optional backend | LM Studio OpenAI-compatible API |
| Link | USB (10.11.99.x). Optional Wi-Fi via Tailscale Funnel |
| LaTeX | tectonic (downloaded on first run) |
| Math | SymPy |
| Plots | matplotlib |
| Dashboard | http://localhost:8799 |
| Data | `~/.snip2tex` (config, snips, log) |

## Buttons (lasso menu)

| Button | Result |
|---|---|
| ∑ | Rendered LaTeX, inserted in place of the handwriting |
| = | Evaluated result (SymPy), popup |
| graph | Function plot, inserted below |
| ? | Short answer in a popup, or a diagram inserted below (function graph, geometry, flowchart / concept map, chart) |
| ✦ Solve steps | Worked solution, inserted below; answer in a popup |
| ✦ Check work | First wrong line, or "looks right", popup |

Popups close on tap or after 30 s.

## Install

Mac:

```sh
brew install uv            # or https://docs.astral.sh/uv/
./make_app.sh              # builds Snip2TeX.app in /Applications (or ~/Applications)
open -a Snip2TeX
```

Menu bar: ∑. First run downloads tectonic and the default model.

Tablet (USB, xovi installed with qt-resource-rebuilder, qt-command-executor, framebuffer-spy, xovi-message-broker):

```sh
uv run install_tablet.py
```

xovi does not survive a tablet reboot. While the tablet is plugged in, the app restarts it.

## Settings (dashboard)

| Setting | Default |
|---|---|
| Model | MLX folder under `~/.lmstudio/models` |
| ∑ replaces handwriting | on |
| graph replaces handwriting | off |
| Selection padding | 5 px |
| Popup auto-hide | 30 s |
| Stay awake with lid closed | off (`pmset disablesleep`, restored on quit and at 15% battery) |
| Wi-Fi mode | off (Tailscale Funnel, token-protected, tablet endpoints only) |

## Files

| Path | Role |
|---|---|
| `snip2tex.py` | HTTP server, dashboard, pipelines |
| `model.py` | mlx-vlm and LM Studio backends |
| `diagrams.py` | ? routing and diagram templates |
| `tutor.py` | Solve steps, Check work |
| `tablet.py` | USB watcher: restarts xovi, pushes settings |
| `remote.py` | Tailscale Funnel |
| `power.py` | Lid-closed mode |
| `app.py`, `launcher.c`, `make_app.sh` | Menu bar app and launcher |
| `tablet/` | xovi QML patch, icons, upload script |
| `install_tablet.py` | Tablet installer |

## License

MIT

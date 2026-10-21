# snip2tex-mac

Lasso handwriting on a reMarkable Paper Pro, tap a button, get the result from a local vision model running on a Mac.

## Spec

| | |
|---|---|
| Tablet | reMarkable Paper Pro, firmware 3.27.3.0, developer mode, xovi |
| Computer | Apple Silicon Mac, macOS 14+ |
| Model | Gemma 4 vision (MLX), in-process via mlx-vlm. Default E2B (~4 GB). Any MLX vision model in `~/.lmstudio/models` works |
| Optional backends | LM Studio OpenAI-compatible API; Claude (Haiku, Sonnet, Opus, Fable) through the Claude Code CLI on your Claude plan |
| Link | USB (10.11.99.x). Optional Wi-Fi via Tailscale Funnel, off by default ([WIFI.md](WIFI.md)) |
| LaTeX | tectonic (downloaded on first run) |
| Math | SymPy |
| Plots | matplotlib |
| Dashboard | http://localhost:8799 |
| Data | `~/.snip2tex` (config, history, usage, log) |
| Gallery | http://localhost:8799/gallery |

## Buttons (lasso menu)

| Button | Result |
|---|---|
| ∑ | Rendered LaTeX, inserted in place of the handwriting. Drawings come back as TikZ |
| = | Evaluated result (SymPy), popup |
| graph | Function plot, inserted below |
| ? | Short answer in a popup, or a diagram inserted below (function graph, geometry, flowchart / concept map, chart). With a stronger Claude model, free-form TikZ diagrams |
| ✦ Solve steps | Worked solution, inserted below; answer in a popup |
| ✦ Check work | First wrong line, or "looks right", popup |

Popups close on tap or after 30 s.

Only the lassoed strokes are sent, not other ink inside the box. Ink colours are kept.

## Toolbar (✦)

A ✦ button at the end of the document toolbar opens a menu:

| Item | Effect |
|---|---|
| Model rows | Switch between the local model and the Claude models |
| Thinking | Let the model think first (always on for Opus) |
| Use Wi-Fi (Tailscale) | Prefer the Funnel URL over USB; turns Wi-Fi mode on if needed |

## Install

Mac:

```sh
brew install uv            # or https://docs.astral.sh/uv/
./make_app.sh              # builds Snip2TeX.app in /Applications (or ~/Applications)
open -a Snip2TeX
```

Menu bar: ∑. First run downloads tectonic and the default model. Every press is saved under `~/.snip2tex/history` and shown in the Gallery (dashboard button or menu bar).

Before anything on the tablet: back up its root SSH password. It is under Settings > General > Help > Copyrights and licenses (developer mode on). Store it somewhere off the tablet (password manager). You need it to install, to get back in over SSH if the key stops working, and to recover if xovi leaves the tablet in a bad state. It changes after a factory reset.

Tablet (USB, xovi installed with qt-resource-rebuilder, qt-command-executor, framebuffer-spy, xovi-message-broker): on first launch the dashboard shows "Set up the tablet". Plug the tablet in, enter the root password (used once to install an SSH key, not saved), confirm it is backed up, press Set up tablet. Again later: Connection card, "Set up tablet…". From a terminal instead:

```sh
uv run install_tablet.py
```

xovi does not survive a tablet reboot. While the tablet is plugged in, the app restarts it.

## Settings (dashboard)

| Setting | Default |
|---|---|
| Model | MLX folder under `~/.lmstudio/models` |
| Use Claude | off. Needs the `claude` CLI signed in. While on, the local model is not loaded |
| Claude model | Haiku 5.5 |
| Thinking | off |
| Free diagrams | on (Sonnet or better, or Haiku with thinking) |
| Colour | on |
| Render size | 226 dpi (screen density), 120 to 480 |
| Render font | classic (or sans: Fira Sans, Fira Math) |
| Exact selection | on |
| ∑ replaces handwriting | on |
| graph replaces handwriting | off |
| Selection padding | 5 px |
| Popup auto-hide | 30 s |
| Stay awake with lid closed | off (`pmset disablesleep`, restored on quit and at 15% battery) |
| Wi-Fi mode | off (Tailscale Funnel, token-protected, tablet endpoints only). Setup: [WIFI.md](WIFI.md) |

## Files

| Path | Role |
|---|---|
| `snip2tex.py` | HTTP server, dashboard, pipelines |
| `model.py` | mlx-vlm, LM Studio and Claude Code backends |
| `gallery.py` | History gallery page |
| `diagrams.py` | ? routing and diagram templates |
| `tutor.py` | Solve steps, Check work |
| `tablet.py` | USB watcher: restarts xovi, pushes settings |
| `remote.py` | Tailscale Funnel |
| `power.py` | Lid-closed mode |
| `app.py`, `launcher.c`, `make_app.sh` | Menu bar app and launcher |
| `tablet/` | xovi QML patches (lasso menu, toolbar), icons, upload and model scripts |
| `install_tablet.py` | Tablet installer |

## License

MIT

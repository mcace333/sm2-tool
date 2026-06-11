# SM2 Screenshot & Discord-Logger Tool

A hotkey-driven tool for **Space Marine 2** that automatically captures all 4 battle-result tabs, then opens a form to generate a formatted Discord post and copy it to the clipboard.

## How it works

1. Press **Home**, **End**, or **F7** while the results screen is open.
2. The tool presses `Enter` to open the detail view, then takes 4 screenshots — switching tabs with `E` between each.
3. A small GUI opens where you fill in mission details.
4. Click **Generate & copy text** — the formatted result is on your clipboard, ready to paste into Discord.

Screenshots are saved to `SM2_Results/<YYYY-MM-DD_HH-MM-SS>/`.

## Requirements

- Linux with X11 or XWayland (`DISPLAY` must be set)
- `xdotool` (for key simulation and window focus)
- Screenshot backend — first available is used:
  - `spectacle` (KDE, recommended)
  - `scrot`
  - `mss` + `Pillow` (fallback, may produce black images in some games)
- Python 3.10+

## Installation & usage

```bash
git clone https://github.com/mcace333/sm2-tool.git
cd sm2-tool
./run.sh
```

`run.sh` creates a virtual environment and installs dependencies automatically on first run. On subsequent runs it launches the tool directly.

> On KDE/Wayland, a permission dialog may appear on the first run — allow both screenshot and key-input access once.

## Configuration

All tuneable values are at the top of `sm2_tool.py`:

| Variable | Default | Description |
|---|---|---|
| `HOTKEYS` | `["Home", "End", "F7"]` | Keys that trigger the sequence |
| `INITIAL_KEY` | `"Return"` | Key pressed once to open the detail screen |
| `TAB_KEY` | `"e"` | Key used to switch tabs |
| `INITIAL_PAUSE` | `2.0` s | Delay before the first screenshot |
| `TAB_SWITCH_PAUSE` | `4.0` s | Delay between tab switch and next screenshot |
| `CAPTURE_SCALE` | `1.0` | Fraction of the screen captured (1.0 = full screen) |
| `OUTPUT_DIR` | `"SM2_Results"` | Folder where screenshots are saved |

## GUI modes

**Operation** — fills in Mission, Difficulty, Geneseed, Armorydata, and an optional Challenge name.

**Siege** — fills in Mission type (Normal / Hard), Waves survived, and an optional Challenge name.

## Windows build (.exe)

A standalone `sm2_tool.exe` can be built on Windows using the files in the `Windows/` folder.

Requirements:
- Python 3.10+ from [python.org](https://www.python.org/downloads/) — during installation, tick **"Add Python to PATH"**.

Build:

```cmd
cd Windows
build.bat
```

`build.bat` installs the dependencies (`mss`, `pynput`, `Pillow`, `pyinstaller`) and runs PyInstaller. The finished binary is written to `Windows\dist\sm2_tool.exe` and can be run directly — no Python installation required on the target machine.

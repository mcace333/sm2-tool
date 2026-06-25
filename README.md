# SM2 Screenshot & Discord-Logger Tool

**Version 1.1**

A hotkey-driven tool for **Space Marine 2** that captures the end-of-mission
screens, reads the relevant data via OCR, and generates a ready-to-paste Discord
post — including player names in `@discord` format.

## How it works

1. Press **Home**, **End**, or **F7** while the results screen is open.
2. The tool walks the result screens and takes **5 screenshots**:
   - the **Victory** screen,
   - the **3 player stat** screens (cycled with `E`),
   - and — after another `Enter` — the **Rewards / Character-Progress** screen.
3. The screenshots are analyzed automatically (OCR):
   - **Gene-Seed** retrieved? (from the Victory screen)
   - **Mode** (Operation / Siege), **Map** (Operations), **Waves** survived (Siege)
   - per player: **name** + **class**, and the **chip** value from the Rewards screen
4. Player names differ between game and Discord, so the first time an unknown
   in-game name appears a small popup asks for the matching Discord name (e.g.
   in-game `McAce` → Discord `Techmarine Ace`). The mapping is stored in
   `players.json` and applied automatically afterwards.
5. The GUI opens with the auto-detected values pre-filled. Adjust the manual
   fields (Difficulty, Armorydata, Challenge) and click **Generate & copy text**
   — the formatted result is on your clipboard.

Example output:

```
Mission: Decapitation
Difficulty: Lethal
Geneseed: Retrieved
Armorydata: 3
Brothers: @Techmarine Ace, @AboutZero, @EarthWindFire
Chips: Techmarine Ace: 3
```

Screenshots are saved to `SM2_Results/<YYYY-MM-DD_HH-MM-SS>/`.

## What is read automatically vs. entered manually

| Automatic (OCR) | Manual (GUI) |
|---|---|
| Mode (Operation / Siege) | Difficulty |
| Map / Mission (Operations) | Armorydata |
| Waves survived (Siege) | Challenge name |
| Gene-Seed status | |
| Brothers (`@discord`) | |
| Chip value(s) | |

> The in-game player name is only an OCR *suggestion* (stylized font) — you
> confirm/correct it in the popup, and the Discord name is what gets stored.

## Requirements

- Linux with X11 or XWayland (`DISPLAY` must be set)
- `xdotool` (for key simulation and window focus)
- Screenshot backend — first available is used:
  - `spectacle` (KDE, recommended)
  - `scrot`
  - `mss` + `Pillow` (fallback, may produce black images in some games)
- Python 3.10+
- For the OCR autofill (optional but recommended):
  - `numpy`, `Pillow`, `pytesseract` (installed via `requirements.txt`)
  - the **tesseract** binary (system package):
    `sudo pacman -S tesseract tesseract-data-eng`

Without tesseract the tool still captures screenshots and opens the GUI — only
the automatic autofill is disabled.

## Installation & usage

```bash
git clone https://github.com/mcace333/sm2-tool.git
cd sm2-tool
./run.sh
```

`run.sh` creates a virtual environment and installs dependencies on first run,
self-heals missing packages on later runs, and warns if the tesseract binary is
missing. On subsequent runs it launches the tool directly.

> On KDE/Wayland, a permission dialog may appear on the first run — allow both
> screenshot and key-input access once.

## Configuration

All tuneable values are at the top of `sm2_tool.py`:

| Variable | Default | Description |
|---|---|---|
| `HOTKEYS` | `["Home", "End", "F7"]` | Keys that trigger the sequence |
| `INITIAL_KEY` | `"Return"` | Key pressed to advance to the next screen group |
| `TAB_KEY` | `"e"` | Key used to switch tabs / players |
| `N_STAT_SCREENS` | `3` | Player stat screens captured (cycled with `TAB_KEY`) |
| `N_REWARD_SCREENS` | `1` | Rewards screens captured after the second `Enter` (set to `3` to capture all players' chips) |
| `INITIAL_PAUSE` | `2.0` s | Delay before the first screenshot |
| `TAB_SWITCH_PAUSE` | `4.0` s | Delay between tab switch and next screenshot |
| `CAPTURE_SCALE` | `1.0` | Fraction of the screen captured (1.0 = full screen) |
| `OUTPUT_DIR` | `"SM2_Results"` | Folder where screenshots are saved |

OCR crop regions (fractional, calibrated for 3440×1440) live at the top of
`sm2_ocr.py` and `sm2_analyze.py`.

## Player mapping

`players.json` maps in-game names to Discord names:

```json
{
  "McAce": "Techmarine Ace"
}
```

It is created/extended automatically through the popup. The file is personal and
not tracked by git.

## GUI modes

**Operation** — Mission, Difficulty, Geneseed, Armorydata, optional Challenge.
Mission, Geneseed and the Brothers/Chips lines are pre-filled from OCR.

**Siege** — Mission type (Normal / Hard), Waves survived, optional Challenge.
Waves and the Brothers line are pre-filled from OCR.

## Helper scripts

These can be run directly on existing screenshot folders (useful for testing):

```bash
# Analyze a match folder (mode, map, gene-seed, players, chips); --map opens
# the Discord-name popup for unknown players.
.venv/bin/python sm2_ocr.py [--map] SM2_Results/<folder>

# Detect the rewards chip value across all screenshots and write chips.csv.
.venv/bin/python sm2_analyze.py
```

The chip is found by colour-robust template matching (`assets/chip_template.png`)
via FFT normalized cross-correlation, so its position and colour don't matter.

## Windows build (.exe)

A standalone `sm2_tool.exe` (the basic capture tool, **without** the OCR
autofill) can be built on Windows using the files in the `Windows/` folder.

Requirements:
- Python 3.10+ from [python.org](https://www.python.org/downloads/) — during
  installation, tick **"Add Python to PATH"**.

Build:

```cmd
cd Windows
build.bat
```

`build.bat` installs the dependencies (`mss`, `pynput`, `Pillow`, `pyinstaller`)
and runs PyInstaller. The finished binary is written to
`Windows\dist\sm2_tool.exe` and runs without a Python installation on the target.

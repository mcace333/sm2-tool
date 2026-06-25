#!/usr/bin/env python3
"""
SM2 Screenshot & Discord-Logger Tool
Waits for a global hotkey (Home/End/F7), captures the end-of-mission screens,
reads mode/map/gene-seed/players/chips via OCR, and pre-fills a GUI that copies
a ready-to-paste Discord post (with players as @discord) to the clipboard.
"""

__version__ = "1.12"

import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk
from Xlib import X, XK
from Xlib import display as xdisplay

# Optionale OCR-Auswertung (Namen/Klasse/Victory/Chips -> Autofill).
try:
    import sm2_ocr
    from sm2_analyze import ChipDetector
    _OCR_AVAILABLE = True
except Exception as _ocr_exc:  # numpy/Pillow/pytesseract/tesseract fehlt
    sm2_ocr = None
    ChipDetector = None
    _OCR_AVAILABLE = False
    print(f"[WARN] OCR-Auswertung nicht verfügbar: {_ocr_exc}")

_CHIP_DET = None      # ChipDetector (lazy, in main initialisiert)
_PLAYER_MAP = None    # sm2_ocr.PlayerMap (In-Game -> Discord)

# ---------------------------------------------------------------------------
# Configuration (easy to adjust)
# ---------------------------------------------------------------------------
CAPTURE_SCALE    = 1.0     # Fraction of screen to capture (centered)
TAB_SWITCH_PAUSE = 4.0     # Pause between tab switch and next screenshot (sec)
INITIAL_PAUSE    = 2.0     # Pause before the first screenshot
REWARD_ENTER_PAUSE = 3.0   # Pause before the second Enter (stats -> rewards)
INITIAL_KEY      = "Return"  # Key pressed once at the start (open screen)
TAB_KEY          = "e"       # Key for switching tabs between screenshots
HOTKEYS          = ["Home", "End", "F7"]  # Keys that trigger the screenshot sequence
OUTPUT_DIR       = "SM2_Results"

# Capture-Sequenz (insgesamt 1 + N_STAT_SCREENS + N_REWARD_SCREENS Screenshots):
#   1 Victory-Screen
#   -> INITIAL_KEY (Enter) -> N_STAT_SCREENS Spieler-Stat-Screens (mit TAB_KEY)
#   -> INITIAL_KEY (Enter) -> N_REWARD_SCREENS Rewards-Screen(s) (mit TAB_KEY)
# Default 3 Stats + 1 Rewards = 5 Screenshots. Auf N_REWARD_SCREENS=3 erhöhen,
# falls die Chips aller 3 Spieler erfasst werden sollen (Rewards mit "e" blättern).
N_STAT_SCREENS   = 3
N_REWARD_SCREENS = 1

# ---------------------------------------------------------------------------
# Dropdown options
# ---------------------------------------------------------------------------
MISSIONS = [
    "INFERNO", "DECAPITATION", "VOX LIBERATIS", "RELIQUARY", "TERMINATION", "VORTEX",
    "FALL OF ATREUS", "BALLISTIC ENGINE", "OBELISK", "EXFILTRATION", "RECLAMATION", "GILDED FATE", "EXTRACTION", "DISRUPTION", "PURGATION"
]
DIFFICULTIES = [
    "MINIMAL", "AVERAGE", "SUBSTANTIAL", "RUTHLESS", "LETHAL", "ABSOLUTE",
    "DAILY STRATAGEM NORMAL", "DAILY STRATAGEM HARD",
    "WEEKLY STRATAGEM NORMAL", "WEEKLY STRATAGEM HARD",
]
GENESEED     = ["RETRIEVED", "LOST / NOT FOUND"]
ARMORYDATA   = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
WAVES        = [str(i) for i in range(1, 51)]
SIEGE_MISSIONS = ["NORMAL", "HARD"]

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_sequence_running = threading.Event()


def check_environment() -> dict:
    env = {}
    display = os.environ.get("DISPLAY", "")
    if not display:
        print("[WARN] DISPLAY is not set – xdotool key simulation will fail.")
    else:
        env["DISPLAY"] = display

    xdotool_path = shutil.which("xdotool")
    if xdotool_path:
        env["xdotool"] = xdotool_path
    else:
        print("[WARN] xdotool not found – tab switching will not work.")

    # Determine screenshot tool (priority: spectacle > scrot > mss fallback)
    for tool in ("spectacle", "scrot"):
        path = shutil.which(tool)
        if path:
            env["screenshot_tool"] = tool
            env[tool] = path
            print(f"[INFO] Screenshot tool: {tool} ({path})")
            break
    else:
        env["screenshot_tool"] = "mss"
        print("[INFO] Screenshot tool: mss (fallback – may produce black images in games)")

    return env


def ensure_output_dir(base_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = base_path / OUTPUT_DIR / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _crop_to_region(image_path: Path, region: dict) -> None:
    from PIL import Image
    x, y, w, h = region["left"], region["top"], region["width"], region["height"]
    if x == 0 and y == 0:
        with Image.open(image_path) as img:
            if img.size == (w, h):
                return
    with Image.open(image_path) as img:
        cropped = img.crop((x, y, x + w, y + h))
        cropped.save(str(image_path), "PNG")


def take_screenshot(output_path: Path, region: dict, env: dict) -> None:
    tool = env.get("screenshot_tool", "mss")
    display = env.get("DISPLAY", ":0")

    if tool == "spectacle":
        result = subprocess.run(
            [env["spectacle"], "-b", "-f", "-n", "-o", str(output_path)],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"[WARN] spectacle error: {result.stderr.decode().strip()}")
        _crop_to_region(output_path, region)

    elif tool == "scrot":
        result = subprocess.run(
            [env["scrot"], str(output_path)],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"[WARN] scrot error: {result.stderr.decode().strip()}")
        _crop_to_region(output_path, region)

    else:
        # mss fallback
        import mss as _mss
        from PIL import Image
        with _mss.mss() as sct:
            raw = sct.grab(region)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            img.save(str(output_path), "PNG")


def get_active_window(env: dict) -> str | None:
    xdotool = env.get("xdotool")
    if not xdotool:
        return None
    result = subprocess.run(
        [xdotool, "getactivewindow"],
        capture_output=True, text=True,
        env={**os.environ, "DISPLAY": env.get("DISPLAY", ":0")},
    )
    return result.stdout.strip() if result.returncode == 0 else None


def simulate_key(env: dict, key: str = TAB_KEY, window_id: str | None = None) -> None:
    xdotool = env.get("xdotool")
    if not xdotool:
        print("[WARN] xdotool not available, skipping key simulation.")
        return
    display = env.get("DISPLAY", ":0")
    xenv = {**os.environ, "DISPLAY": display}
    if window_id:
        subprocess.run([xdotool, "windowfocus", "--sync", window_id], env=xenv, check=False)
        time.sleep(0.1)
    print(f"  → Key: {key}")
    subprocess.run(
        [xdotool, "key", "--clearmodifiers", key],
        env=xenv,
        check=False,
    )


def compute_region() -> dict:
    import mss as _mss
    with _mss.mss() as sct:
        m = sct.monitors[1]
        sw, sh = m["width"], m["height"]
    rw = int(sw * CAPTURE_SCALE)
    rh = int(sh * CAPTURE_SCALE)
    region = {"left": (sw - rw) // 2, "top": (sh - rh) // 2, "width": rw, "height": rh}
    print(f"[INFO] Screen: {sw}x{sh} → Capture region: {rw}x{rh} at ({region['left']}, {region['top']})")
    return region


def run_screenshot_sequence(env: dict, base_path: Path) -> list[Path]:
    session_dir = ensure_output_dir(base_path)
    region = compute_region()
    paths = []

    window_id = get_active_window(env)
    if window_id:
        print(f"  Game window ID: {window_id}")
    else:
        print("[WARN] Could not determine game window – keys may go to the wrong window.")

    time.sleep(INITIAL_PAUSE)

    idx = 1

    def shot():
        nonlocal idx
        p = session_dir / f"screenshot_{idx}.png"
        take_screenshot(p, region, env)
        print(f"Screenshot {idx} saved: {p}")
        paths.append(p)
        idx += 1

    def cycle(n):
        """n Screens aufnehmen, dazwischen TAB_KEY drücken."""
        for j in range(n):
            shot()
            if j < n - 1:
                simulate_key(env, TAB_KEY, window_id)
                time.sleep(TAB_SWITCH_PAUSE)

    # 1) Victory-Screen (vor dem ersten Enter)
    shot()

    # 2) Enter -> Spieler-Stat-Screens
    simulate_key(env, INITIAL_KEY, window_id)
    time.sleep(TAB_SWITCH_PAUSE)
    cycle(N_STAT_SCREENS)

    # 3) Enter -> Rewards-Screens (5. Screen ff.)
    if N_REWARD_SCREENS > 0:
        time.sleep(REWARD_ENTER_PAUSE)   # vor dem letzten Enter warten
        simulate_key(env, INITIAL_KEY, window_id)
        time.sleep(TAB_SWITCH_PAUSE)
        cycle(N_REWARD_SCREENS)

    return paths


def _brothers_lines(autofill: dict | None) -> list[str]:
    """Brothers-Zeile aus den automatisch ausgelesenen Daten."""
    brothers = (autofill or {}).get("brothers") or []
    return ["Brothers: " + ", ".join(brothers) if brothers else "Brothers: "]


def generate_result_text(mission: str, difficulty: str, geneseed: str, armorydata: str, challenge: str = "", autofill: dict | None = None) -> str:
    lines = []
    if challenge.strip():
        lines.append(f"Challenge: {challenge.strip()}")
    lines += [
        f"Mission: {mission.title()}",
        f"Difficulty: {difficulty.title()}",
        f"Geneseed: {geneseed.title()}",
        f"Armorydata: {armorydata}",
    ]
    lines += _brothers_lines(autofill)
    return "\n".join(lines)


def generate_siege_text(mission: str, waves: str, challenge: str = "", autofill: dict | None = None) -> str:
    lines = []
    if challenge.strip():
        lines.append(f"Challenge: {challenge.strip()}")
    lines += [
        f"Mission: {mission.title()} Siege",
        f"Waves: {waves}",
    ]
    lines += _brothers_lines(autofill)
    return "\n".join(lines)


def on_copy_button(win: tk.Toplevel, mode_var: tk.StringVar, vars_op: dict, vars_siege: dict, status_label: tk.Label, parent: tk.Tk, autofill: dict | None = None) -> None:
    if mode_var.get() == "SIEGE":
        text = generate_siege_text(
            mission   = vars_siege["mission"].get(),
            waves     = vars_siege["waves"].get(),
            challenge = vars_siege["challenge"].get(),
            autofill  = autofill,
        )
    else:
        text = generate_result_text(
            mission    = vars_op["mission"].get(),
            difficulty = vars_op["difficulty"].get(),
            geneseed   = vars_op["geneseed"].get(),
            armorydata = vars_op["armorydata"].get(),
            challenge  = vars_op["challenge"].get(),
            autofill   = autofill,
        )
    win.clipboard_clear()
    win.clipboard_append(text)
    win.update()
    print(f"Copied:\n{text}")
    status_label.config(text="✓ Copied to clipboard!")
    win.after(500, win.destroy)
    parent.after(700, parent.quit)


def analyze_session(session_dir: Path | None) -> object | None:
    """OCR-Auswertung des Session-Ordners (im Hintergrund-Thread aufgerufen).
    Liefert sm2_ocr.MatchData oder None."""
    if not (_OCR_AVAILABLE and _CHIP_DET and session_dir):
        return None
    try:
        return sm2_ocr.analyze_match(session_dir, _CHIP_DET)
    except Exception as exc:
        print(f"[WARN] OCR-Auswertung fehlgeschlagen: {exc}")
        return None


def build_autofill_with_names(md, parent: tk.Tk) -> dict:
    """Spielernamen -> Discord auflösen (Popups im Main-Thread) und Autofill
    erzeugen."""
    if md is None:
        return {}
    if _PLAYER_MAP is not None:
        for pl in md.players:
            try:
                _PLAYER_MAP.resolve(pl, parent=parent)
            except Exception as exc:
                print(f"[WARN] Namens-Mapping fehlgeschlagen: {exc}")
    for w in getattr(md, "warnings", []):
        print(f"[WARN] {w}")
    return sm2_ocr.build_autofill(md)


def open_results_gui(parent: tk.Tk, screenshot_paths: list[Path], autofill: dict | None = None) -> None:
    autofill = autofill or {}
    win = tk.Toplevel(parent)
    win.title("SM2 Mission Results")
    win.resizable(False, False)

    outer = ttk.Frame(win, padding=16)
    outer.grid(sticky="nsew")

    # Mode selection
    mode_var = tk.StringVar(value="OPERATION")
    ttk.Label(outer, text="MODE:", anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
    ttk.Combobox(outer, textvariable=mode_var, values=["OPERATION", "SIEGE"], state="readonly", width=24).grid(
        row=0, column=1, sticky="ew", pady=(0, 8)
    )
    ttk.Separator(outer, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))

    # --- Operation frame ---
    op_frame = ttk.Frame(outer)
    op_frame.grid(row=2, column=0, columnspan=2, sticky="ew")

    vars_op = {
        "challenge":  tk.StringVar(value=""),
        "mission":    tk.StringVar(value=MISSIONS[0]),
        "difficulty": tk.StringVar(value=DIFFICULTIES[0]),
        "geneseed":   tk.StringVar(value=GENESEED[0]),
        "armorydata": tk.StringVar(value=ARMORYDATA[0]),
    }
    ttk.Label(op_frame, text="CHALLENGE:", anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(op_frame, textvariable=vars_op["challenge"], width=26).grid(row=0, column=1, sticky="ew", pady=4)
    for row_idx, (lbl, key, opts) in enumerate([
        ("MISSION:",    "mission",    MISSIONS),
        ("DIFFICULTY:", "difficulty", DIFFICULTIES),
        ("GENESEED:",   "geneseed",   GENESEED),
        ("ARMORYDATA:", "armorydata", ARMORYDATA),
    ], start=1):
        ttk.Label(op_frame, text=lbl, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(op_frame, textvariable=vars_op[key], values=opts, state="readonly", width=24).grid(
            row=row_idx, column=1, sticky="ew", pady=4
        )

    # --- Siege frame ---
    siege_frame = ttk.Frame(outer)

    vars_siege = {
        "challenge": tk.StringVar(value=""),
        "mission":   tk.StringVar(value=SIEGE_MISSIONS[0]),
        "waves":     tk.StringVar(value=WAVES[0]),
    }
    ttk.Label(siege_frame, text="CHALLENGE:", anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(siege_frame, textvariable=vars_siege["challenge"], width=26).grid(row=0, column=1, sticky="ew", pady=4)
    for row_idx, (lbl, key, opts) in enumerate([
        ("MISSION:", "mission", SIEGE_MISSIONS),
        ("WAVES:",   "waves",   WAVES),
    ], start=1):
        ttk.Label(siege_frame, text=lbl, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(siege_frame, textvariable=vars_siege[key], values=opts, state="readonly", width=24).grid(
            row=row_idx, column=1, sticky="ew", pady=4
        )

    def on_mode_change(*_):
        if mode_var.get() == "SIEGE":
            op_frame.grid_remove()
            siege_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        else:
            siege_frame.grid_remove()
            op_frame.grid(row=2, column=0, columnspan=2, sticky="ew")

    mode_var.trace_add("write", on_mode_change)

    # --- Automatisch ausgelesene Werte vorbelegen -------------------------
    def _set_if_known(var, value, options):
        if value and value in options:
            var.set(value)
    if autofill.get("mode") == "siege":
        mode_var.set("SIEGE")
        if autofill.get("wave"):
            _set_if_known(vars_siege["waves"], autofill["wave"], WAVES)
    elif autofill.get("mode") == "operation":
        mode_var.set("OPERATION")
    _set_if_known(vars_op["mission"], autofill.get("mission", ""), MISSIONS)
    _set_if_known(vars_op["geneseed"], autofill.get("geneseed", ""), GENESEED)

    # Info-Zeile: erkannte Brothers zur Kontrolle anzeigen.
    info_bits = []
    if autofill.get("brothers"):
        info_bits.append("Brothers: " + ", ".join(autofill["brothers"]))
    if info_bits:
        ttk.Label(outer, text="  |  ".join(info_bits), foreground="#555",
                  wraplength=360, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    status_label = ttk.Label(outer, text="", foreground="green")
    status_label.grid(row=4, column=0, columnspan=2, pady=(4, 0))

    ttk.Button(
        outer,
        text="Generate & copy text",
        command=lambda: on_copy_button(win, mode_var, vars_op, vars_siege, status_label, parent, autofill),
    ).grid(row=3, column=0, columnspan=2, pady=(12, 4), sticky="ew")

    parent.wait_window(win)


def start_hotkey_listener(env: dict, base_path: Path, gui_queue: queue.Queue):
    disp = xdisplay.Display(env.get("DISPLAY", ":0"))
    root = disp.screen().root

    # Grab only without modifiers + NumLock/CapsLock variants.
    # AnyModifier would also catch Shift+Home etc., leaving Shift "stuck".
    _LOCK_VARIANTS = [0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask]

    hotkey_codes: set[int] = set()
    for key_name in HOTKEYS:
        ksym = XK.string_to_keysym(key_name)
        if ksym == 0:
            print(f"[WARN] Unknown hotkey name: {key_name!r}")
            continue
        kc = disp.keysym_to_keycode(ksym)
        if kc:
            for mod in _LOCK_VARIANTS:
                root.grab_key(kc, mod, True, X.GrabModeAsync, X.GrabModeAsync)
            hotkey_codes.add(kc)
    disp.flush()

    def listen():
        while True:
            event = disp.next_event()
            if event.type == X.KeyPress and event.detail in hotkey_codes:
                if _sequence_running.is_set():
                    print("[INFO] Screenshot sequence already running, hotkey ignored.")
                    continue
                _sequence_running.set()
                print("[INFO] Hotkey detected, starting screenshot sequence...")

                def run():
                    try:
                        gui_queue.put("Taking screenshots...")
                        paths = run_screenshot_sequence(env, base_path)
                        session_dir = paths[0].parent if paths else None
                        if _OCR_AVAILABLE:
                            gui_queue.put("Analyzing screens (OCR)...")
                        md = analyze_session(session_dir)
                        gui_queue.put(("results", session_dir, md))
                    except Exception as exc:
                        print(f"[ERROR] Screenshot sequence failed: {exc}")
                        gui_queue.put("Error – see terminal.")
                    finally:
                        _sequence_running.clear()

                threading.Thread(target=run, daemon=True).start()

    t = threading.Thread(target=listen, daemon=True, name="hotkey-listener")
    t.start()
    return t


def warmup_permissions(env: dict) -> None:
    display = env.get("DISPLAY", ":0")
    xenv = {**os.environ, "DISPLAY": display}

    # --- Screenshot permission (spectacle / KDE portal) ---
    if env.get("screenshot_tool") == "spectacle":
        tmp = Path("/tmp/sm2_warmup.png")
        print("Permission check: requesting screenshot permission...")
        print("→ If a dialog appears, please allow it once.")
        result = subprocess.run(
            [env["spectacle"], "-b", "-f", "-n", "-o", str(tmp)],
            env=xenv, capture_output=True, timeout=30,
        )
        if tmp.exists():
            tmp.unlink()
        if result.returncode == 0:
            print("✓ Screenshot permission granted.")
        else:
            print(f"[WARN] Screenshot warmup failed: {result.stderr.decode().strip()}")

    # --- Key input permission (xdotool) ---
    if env.get("xdotool"):
        print("Permission check: requesting key input permission...")
        print("→ If a dialog appears, please allow it once.")
        result = subprocess.run(
            [env["xdotool"], "key", "--clearmodifiers", "Hyper_L"],
            env=xenv, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            print("✓ Key input permission granted.")
        else:
            print(f"[WARN] Key input warmup failed: {result.stderr.decode().strip()}")


def main() -> None:
    base_path = Path(__file__).parent.resolve()

    print("SM2 Screenshot & Discord-Logger Tool")
    print("=====================================")

    env = check_environment()

    # OCR-Auswertung initialisieren (ChipDetector + Spieler-Mapping).
    global _CHIP_DET, _PLAYER_MAP
    if _OCR_AVAILABLE:
        tpl = base_path / "assets" / "chip_template.png"
        if tpl.exists():
            try:
                _CHIP_DET = ChipDetector(tpl)
                _PLAYER_MAP = sm2_ocr.PlayerMap(base_path / sm2_ocr.PLAYERS_FILE)
                print("[INFO] OCR-Auswertung aktiv (Autofill).")
            except Exception as exc:
                print(f"[WARN] OCR-Init fehlgeschlagen: {exc}")
        else:
            print(f"[WARN] Chip-Template fehlt: {tpl} – OCR-Autofill deaktiviert.")

    root = tk.Tk()
    root.withdraw()

    warmup_permissions(env)

    gui_queue: queue.Queue = queue.Queue()

    listener = start_hotkey_listener(env, base_path, gui_queue)
    print(f"Waiting for Home/End/F7 key... (output in {base_path / OUTPUT_DIR})")

    def poll_queue():
        try:
            item = gui_queue.get_nowait()
            if isinstance(item, str):
                print(f"[INFO] {item}")
            elif isinstance(item, tuple) and item and item[0] == "results":
                _, session_dir, md = item
                print("Results ready – fill in the form.")
                autofill = build_autofill_with_names(md, root)
                paths = sorted(session_dir.glob("*.png")) if session_dir else []
                open_results_gui(root, paths, autofill)
                print("Done.")
            else:  # rückwärtskompatibel: reine Pfadliste
                print("Results ready – fill in the form.")
                open_results_gui(root, item)
                print("Done.")
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    root.after(100, poll_queue)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nExiting.")


if __name__ == "__main__":
    main()

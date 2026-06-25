#!/usr/bin/env python3
"""
SM2 OCR – Auswertung der End-of-Mission-Screens
================================================

Liest aus den Screenshots eines Matches automatisch aus:
  * Victory-Screen:  Modus (Operation/Siege), Mission, Wave, Gene-Seed gefunden?, Total Score
  * Stat-Screen:     Spielername (In-Game) + Klasse je Spieler
  * Rewards-Screen:  Chip-Wert + Klasse je Spieler  (Chip via sm2_analyze.ChipDetector)

Spielernamen unterscheiden sich zwischen Spiel und Discord. Über `players.json`
wird In-Game-Name -> Discord-Name gemappt; unbekannte Namen werden per
tkinter-Popup abgefragt (zeigt den Namens-Ausschnitt + OCR-Vorschlag).

Dieses Modul ist importierbar (für sm2_tool.py) und als CLI testbar:

    python sm2_ocr.py SM2_Results/2026-06-17_15-15-50      # Ordner auswerten (ohne Popup)
    python sm2_ocr.py --map SM2_Results/<ordner>           # mit Namens-Popup
"""

from __future__ import annotations

import difflib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
import pytesseract

from sm2_analyze import ChipDetector, DETECT_THRESHOLD, _ocr_available

log = logging.getLogger("sm2_ocr")

# ---------------------------------------------------------------------------
# Konfiguration (fraktional -> auflösungsunabhängig; kalibriert auf 3440x1440)
# ---------------------------------------------------------------------------
CLASSES = ["TACTICAL", "ASSAULT", "VANGUARD", "BULWARK", "SNIPER", "HEAVY",
           "TECHMARINE"]

# Bekannte Operation-Maps (zum Fuzzy-Abgleich des verrauschten Titel-OCR).
MISSIONS = [
    "INFERNO", "DECAPITATION", "VOX LIBERATIS", "RELIQUARY", "TERMINATION",
    "VORTEX", "FALL OF ATREUS", "BALLISTIC ENGINE", "OBELISK", "EXFILTRATION",
    "RECLAMATION", "GILDED FATE", "EXTRACTION", "DISRUPTION", "PURGATION",
]

# Stat-Label-Schlüsselwörter: zuverlässige Signatur eines Stat-Screens
# (kommen nie auf dem Victory-Screen vor).
STAT_LABELS = ["SPECIAL KILLS", "MELEE", "RANGED", "INCAPACITAT",
               "REVIVED", "DAMAGE TAKEN", "ITEMS FOUND"]

# Stat-Screen: Name- und Klassen-Zeile oben im rechten Panel.
NAME_BOX  = (0.55, 0.212, 0.70, 0.252)
CLASS_BOX = (0.55, 0.252, 0.70, 0.285)

# Victory-Screen: Missions-/Status-Kopfzeile (oben links) + Objective-Körper.
TITLE_BOX  = (0.12, 0.048, 0.46, 0.086)
STATUS_BOX = (0.10, 0.086, 0.42, 0.120)
VICTORY_BODY = (0.30, 0.10, 0.72, 0.88)
# Bereich, in dem Stat-Labels liegen (links der Wertespalten).
STATLABEL_BOX = (0.45, 0.20, 0.78, 0.82)

# Rewards-Screen: "Vanguard 25"-Zeile unter CHARACTER PROGRESS.
PROGRESS_BOX = (0.55, 0.62, 0.85, 0.72)

PLAYERS_FILE = "players.json"


# ---------------------------------------------------------------------------
# OCR-Hilfen
# ---------------------------------------------------------------------------
def _frac_crop(im: Image.Image, box) -> Image.Image:
    W, H = im.size
    l, t, r, b = box
    return im.crop((int(l * W), int(t * H), int(r * W), int(b * H)))


def _prep_max(crop: Image.Image, up: int = 4, floor: int = 110) -> Image.Image:
    """Binarisieren über den Max-Kanal: farbiger Text (auch dunkles Blau) wird
    sichtbar, weil er in mind. einem Kanal hell ist – anders als bei reiner
    Luminanz, wo blaue Namen verschwinden."""
    a = np.asarray(crop.convert("RGB")).max(2).astype(np.uint8)
    thr = max(floor, int(a.mean() + a.std()))
    b = Image.fromarray(((a > thr) * 255).astype(np.uint8))
    return b.resize((b.width * up, b.height * up))


def _ocr_line(img: Image.Image, psm: int = 7, whitelist: str | None = None) -> str:
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(img, config=cfg).strip()


def _fuzzy_class(s: str) -> str | None:
    letters = "".join(ch for ch in s.upper() if ch.isalpha())
    if not letters:
        return None
    m = difflib.get_close_matches(letters, CLASSES, n=1, cutoff=0.4)
    return m[0] if m else None


def _fuzzy_mission(raw: str) -> str:
    """Verrauschtes Titel-OCR gegen die bekannten Maps matchen. Liefert die
    beste Map oder den (bereinigten) Rohtext, wenn nichts passt."""
    s = "".join(ch for ch in raw.upper() if ch.isalpha() or ch == " ")
    key = s.replace(" ", "")
    norm = {m.replace(" ", ""): m for m in MISSIONS}
    m = difflib.get_close_matches(key, list(norm), n=1, cutoff=0.45)
    if m:
        return norm[m[0]]
    return s.strip()


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------
@dataclass
class PlayerData:
    ingame_name: str = ""          # OCR-Vorschlag / bestätigter In-Game-Name
    discord_name: str = ""         # aus players.json
    klass: str = ""                # TACTICAL, VANGUARD, ...
    chip: int | None = None        # Rewards-Chip-Wert
    name_crop: Image.Image | None = field(default=None, repr=False)


@dataclass
class MatchData:
    folder: str = ""
    mode: str = ""                 # 'operation' | 'siege' | ''
    mission: str = ""
    wave: str = ""
    geneseed: bool = False
    total_score: str = ""
    players: list[PlayerData] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Screen-Klassifizierung
# ---------------------------------------------------------------------------
def classify_screen(im: Image.Image, chip_det: ChipDetector | None = None) -> str:
    """-> 'victory' | 'stats' | 'rewards' | 'other'.

    Reihenfolge: rewards -> stats (über Stat-Labels) -> victory. Die Stat-Labels
    sind die zuverlässigste Signatur und verhindern, dass Victory-Screens (deren
    OBJECTIVES/VICTORY-Text tesseract oft verstümmelt) fälschlich als stats
    durchrutschen."""
    # Rewards: "CHARACTER PROGRESS" / "REWARDS" oben rechts, oder ein Chip.
    tr = _frac_crop(im, (0.55, 0.20, 1.0, 0.70)).convert("L")
    tr_txt = pytesseract.image_to_string(tr, config="--psm 6").upper()
    if "CHARACTER PROGRESS" in tr_txt or "REWARDS" in tr_txt:
        return "rewards"
    if chip_det is not None and chip_det.detect(im)["score"] >= DETECT_THRESHOLD:
        return "rewards"

    # Stats: Stat-Label-Schlüsselwörter im Panel.
    sl = _frac_crop(im, STATLABEL_BOX).convert("L")
    sl_txt = pytesseract.image_to_string(sl, config="--psm 6").upper()
    if sum(1 for k in STAT_LABELS if k in sl_txt) >= 2:
        return "stats"

    # Victory: Objective-/Score-Panel.
    body_txt = pytesseract.image_to_string(
        _frac_crop(im, VICTORY_BODY).convert("L"), config="--psm 6").upper()
    if ("TOTAL SCORE" in body_txt or "BJECTI" in body_txt
            or "VICTOR" in body_txt or "HONOUR" in body_txt
            or "WAVE" in body_txt):
        return "victory"
    return "other"


# ---------------------------------------------------------------------------
# Extraktion je Screen-Typ
# ---------------------------------------------------------------------------
def extract_victory(im: Image.Image) -> dict:
    import re
    body = _frac_crop(im, VICTORY_BODY).convert("L")
    txt = pytesseract.image_to_string(body, config="--psm 6")
    up = txt.upper()
    out = {"mode": "", "mission": "", "wave": "", "geneseed": False,
           "total_score": ""}

    # Gene-Seed: Sekundär-Objective "Gene-Seed Found".
    low = txt.lower()
    out["geneseed"] = ("gene" in low and "seed" in low)

    # Modus + Wave: primär aus der STATUS-Zeile ("STATUS: SUCCESS" / "WAVE 15").
    status = _ocr_line(_prep_max(_frac_crop(im, STATUS_BOX)), psm=7).upper()
    m = re.search(r"WAVE\s*(\d+)", status) or re.search(r"WAVE\s*(\d+)", up)
    if m or "WAVE" in status or "WAVE" in up:
        out["mode"] = "siege"
        if m:
            out["wave"] = m.group(1)
    else:
        # Victory-Screen ohne WAVE -> Operation (einziger anderer Modus).
        out["mode"] = "operation"

    # Total Score (letzte längere Zahl im Text).
    nums = re.findall(r"\d{3,}", txt.replace(" ", ""))
    if nums:
        out["total_score"] = nums[-1]

    # Map/Missionsname aus der Titelzeile, Fuzzy gegen bekannte Maps.
    title = _ocr_line(_prep_max(_frac_crop(im, TITLE_BOX), floor=80), psm=7)
    # "MISSION"-Präfix abtrennen, Rest matchen.
    after = re.split(r"MISSION", title.upper())
    cand = after[-1] if len(after) > 1 else title
    out["mission"] = _fuzzy_mission(cand)
    return out


def extract_player_stats(im: Image.Image) -> PlayerData:
    name_crop = _frac_crop(im, NAME_BOX)
    name = _ocr_line(_prep_max(name_crop), psm=7)
    name = name.strip()
    cls_raw = _ocr_line(_prep_max(_frac_crop(im, CLASS_BOX)), psm=7)
    klass = _fuzzy_class(cls_raw) or ""
    return PlayerData(ingame_name=name, klass=klass, name_crop=name_crop)


def extract_rewards(im: Image.Image, chip_det: ChipDetector,
                    fname: str = "") -> dict:
    from sm2_analyze import ocr_number_below
    out = {"klass": "", "chip": None}
    det = chip_det.detect(im)
    if det["score"] >= DETECT_THRESHOLD and _ocr_available():
        val, conf = ocr_number_below(im, det["center"], det["size"])
        out["chip"] = val if val is not None else 0
    else:
        out["chip"] = 0
    # Klasse aus "Vanguard 25" unter CHARACTER PROGRESS.
    prog = _ocr_line(_prep_max(_frac_crop(im, PROGRESS_BOX)), psm=7)
    out["klass"] = _fuzzy_class(prog) or ""
    return out


# ---------------------------------------------------------------------------
# Match-Auswertung
# ---------------------------------------------------------------------------
def analyze_match(folder: Path, chip_det: ChipDetector) -> MatchData:
    md = MatchData(folder=str(folder))
    images = sorted(folder.glob("*.png"))
    rewards = []
    for p in images:
        im = Image.open(p).convert("RGB")
        kind = classify_screen(im, chip_det)
        if kind == "victory":
            v = extract_victory(im)
            md.mode = v["mode"] or md.mode
            md.mission = v["mission"] or md.mission
            md.wave = v["wave"] or md.wave
            md.geneseed = md.geneseed or v["geneseed"]
            md.total_score = v["total_score"] or md.total_score
        elif kind == "stats":
            md.players.append(extract_player_stats(im))
        elif kind == "rewards":
            rewards.append(extract_rewards(im, chip_det, p.name))

    # Chips den Spielern über die Klasse zuordnen.
    _assign_chips(md, rewards)
    if not md.players:
        md.warnings.append("Keine Stat-Screens gefunden – keine Spielernamen.")
    return md


def _assign_chips(md: MatchData, rewards: list[dict]) -> None:
    if not rewards:
        return
    by_class = {}
    for r in rewards:
        by_class.setdefault(r["klass"], []).append(r["chip"])
    for pl in md.players:
        vals = by_class.get(pl.klass)
        if vals:
            pl.chip = vals.pop(0)
    # Falls Zuordnung über Klasse mehrdeutig/unmöglich war:
    leftover = [c for v in by_class.values() for c in v]
    if leftover and len(md.players) == len(rewards):
        md.warnings.append(
            f"Chip-Zuordnung über Klasse unsicher (Restwerte {leftover}).")


# ---------------------------------------------------------------------------
# Spieler-Mapping (In-Game -> Discord)
# ---------------------------------------------------------------------------
class PlayerMap:
    def __init__(self, path: Path):
        self.path = path
        self.map: dict[str, str] = {}
        if path.exists():
            try:
                self.map = json.loads(path.read_text())
            except Exception as exc:
                log.warning("players.json nicht lesbar: %s", exc)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.map, indent=2, ensure_ascii=False))

    def closest(self, ingame: str) -> str | None:
        """Bekannten In-Game-Namen per Fuzzy-Match finden (gegen OCR-Schwankung,
        case-unabhängig)."""
        if ingame in self.map:
            return ingame
        low = {k.casefold(): k for k in self.map}
        key = ingame.casefold()
        if key in low:
            return low[key]
        m = difflib.get_close_matches(key, list(low), n=1, cutoff=0.7)
        return low[m[0]] if m else None

    def resolve(self, pl: PlayerData, parent=None) -> str:
        """Discord-Namen liefern; unbekannte Namen per Popup abfragen."""
        key = self.closest(pl.ingame_name)
        if key:
            pl.ingame_name = key
            pl.discord_name = self.map[key]
            return pl.discord_name
        # unbekannt -> abfragen
        discord = ask_discord_name(pl, list(self.map.items()), parent)
        if discord:
            ingame = pl.ingame_name.strip() or discord
            self.map[ingame] = discord
            self.save()
            pl.discord_name = discord
        return pl.discord_name


def ask_discord_name(pl: PlayerData, known: list[tuple[str, str]],
                     parent=None) -> str:
    """tkinter-Popup: Namens-Crop + OCR-Vorschlag, Eingabe des Discord-Namens
    oder Auswahl eines bekannten Spielers. Gibt den Discord-Namen zurück."""
    import tkinter as tk
    from tkinter import ttk

    owns_root = parent is None
    root = tk.Tk() if owns_root else tk.Toplevel(parent)
    root.title("Spieler zuordnen")
    # Vor das (Vollbild-)Spiel zwingen – sonst öffnet das Popup im Hintergrund
    # und das Tool scheint zu hängen, weil es auf die Eingabe wartet.
    print(f"→ Popup: Discord-Name für In-Game '{pl.ingame_name}' eingeben "
          f"(ggf. Alt-Tab).", flush=True)
    try:
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(50, root.focus_force)
    except Exception:
        pass
    frm = ttk.Frame(root, padding=14)
    frm.grid(sticky="nsew")

    ttk.Label(frm, text="Unbekannter Spieler – Discord-Name zuordnen:",
              font=("", 11, "bold")).grid(row=0, column=0, columnspan=2,
                                          sticky="w", pady=(0, 8))

    # Namens-Ausschnitt anzeigen (falls vorhanden).
    if pl.name_crop is not None:
        try:
            from PIL import ImageTk
            crop = pl.name_crop.copy()
            crop.thumbnail((360, 90))
            photo = ImageTk.PhotoImage(crop)
            lbl = ttk.Label(frm, image=photo)
            lbl.image = photo  # Referenz halten
            lbl.grid(row=1, column=0, columnspan=2, pady=(0, 8))
        except Exception:
            pass

    ttk.Label(frm, text=f"In-Game (OCR): {pl.ingame_name!r}   Klasse: {pl.klass}"
              ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

    ttk.Label(frm, text="In-Game-Name:").grid(row=3, column=0, sticky="w")
    ingame_var = tk.StringVar(value=pl.ingame_name)
    ttk.Entry(frm, textvariable=ingame_var, width=28).grid(row=3, column=1,
                                                           sticky="ew", pady=2)

    ttk.Label(frm, text="Discord-Name:").grid(row=4, column=0, sticky="w")
    discord_var = tk.StringVar()
    ent = ttk.Entry(frm, textvariable=discord_var, width=28)
    ent.grid(row=4, column=1, sticky="ew", pady=2)
    ent.focus_set()

    if known:
        ttk.Label(frm, text="…oder bekannten Spieler wählen:").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        sel = tk.StringVar()
        combo = ttk.Combobox(frm, textvariable=sel, state="readonly",
                             values=[f"{i}  →  {d}" for i, d in known], width=34)
        combo.grid(row=6, column=0, columnspan=2, sticky="ew", pady=2)

        def on_pick(*_):
            idx = combo.current()
            if idx >= 0:
                ingame_var.set(known[idx][0])
                discord_var.set(known[idx][1])
        sel.trace_add("write", on_pick)

    result = {"discord": ""}

    def confirm():
        pl.ingame_name = ingame_var.get().strip() or pl.ingame_name
        result["discord"] = discord_var.get().strip()
        root.destroy()

    ttk.Button(frm, text="Übernehmen", command=confirm).grid(
        row=7, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    root.bind("<Return>", lambda _e: confirm())

    if owns_root:
        root.mainloop()
    else:
        parent.wait_window(root)
    return result["discord"]


# ---------------------------------------------------------------------------
# Text-Erzeugung (für Copy&Paste)
# ---------------------------------------------------------------------------
def build_autofill(md: MatchData) -> dict:
    """Liefert die automatisch ermittelten Felder für das GUI / den Text."""
    brothers = [f"@{p.discord_name}" for p in md.players if p.discord_name]
    chips = [(p.discord_name or p.ingame_name, p.chip)
             for p in md.players if p.chip is not None]
    return {
        "mission": md.mission,
        "mode": md.mode,
        "wave": md.wave,
        "geneseed": "RETRIEVED" if md.geneseed else "LOST / NOT FOUND",
        "brothers": brothers,
        "chips": chips,
    }


# ---------------------------------------------------------------------------
# CLI (Test)
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:]
    do_map = "--map" in args
    args = [a for a in args if a != "--map"]
    if not args:
        print("Usage: python sm2_ocr.py [--map] <match-folder>")
        sys.exit(1)

    root = Path(__file__).parent.resolve()
    chip_det = ChipDetector(root / "assets" / "chip_template.png")
    folder = Path(args[0])

    md = analyze_match(folder, chip_det)
    print(f"\n== {md.folder} ==")
    print(f"  mode={md.mode} mission={md.mission!r} wave={md.wave} "
          f"geneseed={md.geneseed} score={md.total_score}")
    for p in md.players:
        print(f"  player: ingame={p.ingame_name!r} class={p.klass} chip={p.chip}")
    for w in md.warnings:
        print(f"  [WARN] {w}")

    if do_map:
        pm = PlayerMap(root / PLAYERS_FILE)
        for p in md.players:
            pm.resolve(p)
        print("  brothers:", build_autofill(md)["brothers"])


if __name__ == "__main__":
    main()

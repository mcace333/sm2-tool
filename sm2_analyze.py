#!/usr/bin/env python3
"""
SM2 Rewards Analyzer
====================

Reads the "Character Progress / Rewards" screen of Space Marine 2 and extracts
the value shown in the *chip* icon (a hexagonal icon containing a small
circuit-board / IC glyph) inside the REWARDS bar.

The chip can sit at any slot in the REWARDS bar and its colour varies
(green / purple / yellow / orange) – all are treated identically. It is found by
its *shape* via colour-robust template matching, so it is not confused with:
  * the coin icon (round, concentric rings, e.g. "405" / "260")
  * the skull-with-crossed-swords icons (rhombus, locked reward slots)

If no chip is found, the value is 0.

The extracted chip value is written to a CSV, one row per analyzed screen.

OCR of the digit uses tesseract (via pytesseract). The chip *detection* itself
needs only numpy + Pillow, so detection works even without tesseract installed;
in that case the chip value is left empty and a warning is logged.

Usage:
    python sm2_analyze.py                  # scan SM2_Results/, write chips.csv
    python sm2_analyze.py --debug          # also save annotated crops to /tmp
    python sm2_analyze.py path/to/img.png  # analyze a single file

System dependency for OCR (one-time):
    sudo pacman -S tesseract tesseract-data-eng
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration (easy to adjust)
# ---------------------------------------------------------------------------
RESULTS_DIR   = "SM2_Results"
CSV_OUTPUT    = "chips.csv"
TEMPLATE_PATH = "assets/chip_template.png"

# Fractional crop region of the REWARDS bar, expressed as fractions of the
# image (left, top, right, bottom). Made deliberately generous so it works for
# both 16:9 (1920x1080 / 3840x2160) and 21:9 ultrawide (3440x1440) captures,
# where the HUD anchors at a different horizontal fraction.
REWARDS_BAND = (0.45, 0.18, 1.00, 0.46)

# The template was cut from a 3840-wide screenshot at ~70 px. The matcher
# scales it relative to the analyzed image width and sweeps factors around that
# to absorb layout/aspect-ratio differences (16:9 vs 21:9). Matching uses an
# FFT-based normalized cross-correlation, so large (detailed = discriminative)
# templates are cheap regardless of source resolution.
TEMPLATE_REF_WIDTH = 3840
TEMPLATE_REF_SIZE  = 70
SCALE_FACTORS      = (0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.4)

# Detection score (normalized cross-correlation, -1..1) above which we accept a
# chip as present. Validated on the dataset: real rewards screens score
# 0.91-1.00, the highest non-rewards screen 0.695 – a wide, clean gap. 0.80
# sits in the middle of it. Lower it if future captures miss a chip.
DETECT_THRESHOLD = 0.80

# Tesseract word confidence (0..100) below which we log a manual-review warning.
OCR_CONFIDENCE_WARN = 60.0

log = logging.getLogger("sm2_analyze")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def _to_gray(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("L"), dtype=np.float32)


def _integral(a: np.ndarray) -> np.ndarray:
    s = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.float64)
    s[1:, 1:] = a.cumsum(0).cumsum(1)
    return s


def _boxsum(s: np.ndarray, h: int, w: int) -> np.ndarray:
    return s[h:, w:] - s[:-h, w:] - s[h:, :-w] + s[:-h, :-w]


def _ncc_map(band: np.ndarray, tpl: np.ndarray) -> np.ndarray | None:
    """Normalized cross-correlation of `tpl` over every position in `band`,
    computed via FFT (numerator) + integral images (local variance).

    Mean-subtraction makes it robust to brightness/colour: it keys on the
    glyph *structure*, not its hue. Returns a 2-D score map (-1..1). Equivalent
    to the textbook sliding-window NCC (verified to ~1e-7) but cheap for large
    templates and high-resolution bands."""
    band = band.astype(np.float64)
    tpl = tpl.astype(np.float64)
    th, tw = tpl.shape
    bh, bw = band.shape
    if bh < th or bw < tw:
        return None

    t0 = tpl - tpl.mean()
    tnorm = float(np.sqrt((t0 * t0).sum())) + 1e-9
    n = th * tw

    # numerator = valid cross-correlation of band with the zero-mean template
    fb = np.fft.rfft2(band, s=(bh, bw))
    ft = np.fft.rfft2(t0[::-1, ::-1], s=(bh, bw))
    full = np.fft.irfft2(fb * ft, s=(bh, bw))
    num = full[th - 1:bh, tw - 1:bw]

    # denominator = local std of band over the template window * tnorm
    s1 = _boxsum(_integral(band), th, tw)
    s2 = _boxsum(_integral(band * band), th, tw)
    var = s2 - (s1 * s1) / n
    var[var < 0] = 0.0
    std = np.sqrt(var)
    out = np.zeros_like(num)
    # Only score windows with real local contrast; flat (dark) regions have
    # std -> 0, where num/den would blow up on FFT round-off. A genuine glyph
    # window has std of many grey levels.
    valid = std > 1.0
    out[valid] = num[valid] / (std[valid] * tnorm)
    return np.clip(out, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Chip detection
# ---------------------------------------------------------------------------
class ChipDetector:
    def __init__(self, template_path: Path):
        self.tpl = _to_gray(Image.open(template_path))

    def detect(self, im: Image.Image) -> dict:
        """Locate the chip glyph. Returns dict with score, center (x, y) and
        the glyph size in the full image, or score below threshold if none.

        Matching is done on a downscaled copy of the rewards band (fixed
        working width) for speed/memory, then coordinates are mapped back."""
        W, H = im.size
        l, t, r, b = REWARDS_BAND
        box = (int(l * W), int(t * H), int(r * W), int(b * H))
        band = _to_gray(im.crop(box))

        base = W / TEMPLATE_REF_WIDTH
        best = {"score": -1.0, "center": None, "size": None}
        for f in SCALE_FACTORS:
            sz = max(10, int(TEMPLATE_REF_SIZE * base * f))
            tpl = np.asarray(
                Image.fromarray(self.tpl).resize((sz, sz)), dtype=np.float32
            )
            m = _ncc_map(band, tpl)
            if m is None:
                continue
            iy, ix = np.unravel_index(int(np.argmax(m)), m.shape)
            score = float(m[iy, ix])
            if score > best["score"]:
                best = {
                    "score": score,
                    "center": (box[0] + ix + sz // 2, box[1] + iy + sz // 2),
                    "size": (sz, sz),
                }
        return best


# ---------------------------------------------------------------------------
# Digit OCR (tesseract via pytesseract)
# ---------------------------------------------------------------------------
def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from shutil import which
        return which("tesseract") is not None
    except Exception:
        return False


def ocr_number_below(im: Image.Image, center, size) -> tuple[int | None, float]:
    """OCR the number printed directly below the chip glyph.

    Returns (value, confidence). value is None if nothing readable."""
    import pytesseract

    cx, cy = center
    gw, gh = size
    # Number sits centered below the glyph; take a box a bit wider than the
    # glyph and ~1 glyph-height tall, starting just under it.
    x0 = int(cx - gw * 0.8)
    x1 = int(cx + gw * 0.8)
    y0 = int(cy + gh * 0.35)
    y1 = int(cy + gh * 1.15)
    crop = im.crop((x0, y0, x1, y1)).convert("L")

    # Upscale + binarize for a cleaner OCR on small HUD digits.
    crop = crop.resize((crop.width * 4, crop.height * 4))
    arr = np.asarray(crop, dtype=np.uint8)
    thr = max(140, int(arr.mean() + arr.std()))
    binimg = Image.fromarray(((arr > thr) * 255).astype(np.uint8))

    cfg = "--psm 7 -c tessedit_char_whitelist=0123456789"
    data = pytesseract.image_to_data(
        binimg, config=cfg, output_type=pytesseract.Output.DICT
    )
    best_val, best_conf = None, -1.0
    for txt, conf in zip(data["text"], data["conf"]):
        txt = txt.strip()
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = -1.0
        if txt.isdigit() and conf > best_conf:
            best_val, best_conf = int(txt), conf
    return best_val, (best_conf if best_conf >= 0 else 0.0)


# ---------------------------------------------------------------------------
# Per-image pipeline
# ---------------------------------------------------------------------------
def analyze_image(path: Path, detector: ChipDetector, have_ocr: bool,
                  debug: bool = False) -> dict:
    im = Image.open(path)
    W, H = im.size
    row = {
        "file": str(path),
        "resolution": f"{W}x{H}",
        "chips": 0,
        "detect_score": "",
        "ocr_confidence": "",
        "needs_review": "",
    }

    det = detector.detect(im)
    row["detect_score"] = f"{det['score']:.3f}"

    if det["score"] < DETECT_THRESHOLD:
        # No chip in this rewards bar -> value 0 (per spec).
        return row

    if not have_ocr:
        row["chips"] = ""
        row["needs_review"] = "yes (tesseract not installed)"
        log.warning("%s: chip found (score %.3f) but tesseract missing – "
                    "cannot read digit. Install: sudo pacman -S tesseract "
                    "tesseract-data-eng", path.name, det["score"])
        return row

    value, conf = ocr_number_below(im, det["center"], det["size"])
    row["ocr_confidence"] = f"{conf:.0f}"

    if value is None:
        row["chips"] = ""
        row["needs_review"] = "yes (OCR empty)"
        log.warning("%s: chip found (score %.3f) but OCR read no digit – "
                    "check manually.", path.name, det["score"])
    else:
        row["chips"] = value
        if conf < OCR_CONFIDENCE_WARN:
            row["needs_review"] = "yes (low OCR confidence)"
            log.warning("%s: low OCR confidence (%.0f) for chip value %d – "
                        "verify manually.", path.name, conf, value)

    if debug:
        cx, cy = det["center"]
        gw, gh = det["size"]
        dbg = im.crop((cx - gw, cy - gh, cx + gw, cy + int(gh * 1.4)))
        out = Path("/tmp") / f"chipdbg_{path.stem}.png"
        dbg.save(out)
        log.info("debug crop -> %s", out)

    return row


# ---------------------------------------------------------------------------
# Discovery + CSV
# ---------------------------------------------------------------------------
def find_images(base: Path) -> list[Path]:
    """All PNGs under SM2_Results (recursively)."""
    return sorted(base.rglob("*.png"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Extract REWARDS chip value via OCR.")
    ap.add_argument("path", nargs="?", help="single image; default scans SM2_Results/")
    ap.add_argument("--debug", action="store_true", help="save annotated crops to /tmp")
    ap.add_argument("--out", default=CSV_OUTPUT, help=f"CSV output (default {CSV_OUTPUT})")
    args = ap.parse_args()

    root = Path(__file__).parent.resolve()
    tpl_path = root / TEMPLATE_PATH
    if not tpl_path.exists():
        log.error("Chip template not found: %s", tpl_path)
        sys.exit(1)
    detector = ChipDetector(tpl_path)

    have_ocr = _ocr_available()
    if not have_ocr:
        log.warning("tesseract not available – chip detection will run but "
                    "digits cannot be read. Install: sudo pacman -S tesseract "
                    "tesseract-data-eng")

    if args.path:
        images = [Path(args.path)]
    else:
        images = find_images(root / RESULTS_DIR)
    log.info("Analyzing %d image(s)...", len(images))

    rows = []
    for p in images:
        try:
            rows.append(analyze_image(p, detector, have_ocr, debug=args.debug))
        except Exception as exc:  # keep going on a single bad file
            log.error("%s: failed (%s)", p, exc)

    out_path = root / args.out
    fields = ["file", "resolution", "chips", "detect_score",
              "ocr_confidence", "needs_review"]
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n_chip = sum(1 for r in rows if str(r["chips"]) not in ("", "0"))
    n_review = sum(1 for r in rows if r["needs_review"])
    log.info("Done. %d images, %d with a chip value, %d flagged for review. "
             "CSV: %s", len(rows), n_chip, n_review, out_path)


if __name__ == "__main__":
    main()

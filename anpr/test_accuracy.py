"""Batch-tests the ANPR engine against test_images/ and reports accuracy.

Each file in test_images/ is named after its actual plate (e.g.
LND-113JN.jpeg), so the ground truth comes directly from the filename -
no separate labels file needed.
"""

import difflib
import glob
import os
import time

from anpr_engine import run_anpr as tesseract_run_anpr

TEST_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_images")


def character_accuracy(actual, predicted):
    """Character-level similarity between the actual and predicted plate, as a %.

    Uses sequence-matching ratio rather than exact position comparison so a
    single dropped/extra character doesn't zero out an otherwise-good read.
    """
    if not predicted:
        return 0.0
    return difflib.SequenceMatcher(None, actual, predicted).ratio() * 100


def run_tests(run_anpr=tesseract_run_anpr):
    """Runs the given ANPR engine's run_anpr(image_path) on every image in
    test_images/ and returns per-image results.

    Defaults to the Tesseract-based engine (anpr_engine.py). Pass
    anpr_engine_easyocr.run_anpr to test the EasyOCR variant instead - both
    modules share the same run_anpr(image_path) -> str | None interface.
    """
    results = []
    for path in sorted(glob.glob(os.path.join(TEST_IMAGES_DIR, "*.jp*g"))):
        filename = os.path.basename(path)
        actual = os.path.splitext(filename)[0].upper()

        start = time.perf_counter()
        predicted = run_anpr(path)
        elapsed = time.perf_counter() - start

        exact_match = predicted is not None and predicted.upper() == actual

        results.append({
            "filename": filename,
            "actual_plate": actual,
            "predicted_plate": predicted or "(none)",
            "exact_match": "YES" if exact_match else "NO",
            "char_accuracy": round(character_accuracy(actual, predicted or ""), 1),
            "processing_time": round(elapsed, 3),
        })

    return results


def summarize(results):
    """Aggregates per-image results into overall accuracy/timing stats."""
    total = len(results)
    exact_matches = sum(1 for r in results if r["exact_match"] == "YES")

    return {
        "total_images": total,
        "exact_match_count": exact_matches,
        "exact_match_accuracy_pct": round(100 * exact_matches / total, 1) if total else 0.0,
        "avg_char_accuracy_pct": round(sum(r["char_accuracy"] for r in results) / total, 1) if total else 0.0,
        "avg_processing_time_sec": round(sum(r["processing_time"] for r in results) / total, 3) if total else 0.0,
    }


if __name__ == "__main__":
    import sys

    if "--engine" in sys.argv and sys.argv[sys.argv.index("--engine") + 1] == "easyocr":
        from anpr_engine_easyocr import run_anpr as engine_run_anpr
    else:
        engine_run_anpr = tesseract_run_anpr

    test_results = run_tests(run_anpr=engine_run_anpr)
    for r in test_results:
        print(r)

    print()
    print(summarize(test_results))

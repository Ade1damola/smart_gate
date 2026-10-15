"""Measure sustained RapidOCR latency and resource use on a Raspberry Pi."""

import argparse
import glob
import multiprocessing
import os
import statistics
import time
from pathlib import Path

import psutil

IMAGE_PATTERNS = ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp")


def _benchmark_worker(image_paths, duration_seconds, results):
    try:
        from anpr_engine_rapidocr import run_anpr
        run_anpr(image_paths[0])
        results.put(("ready", None))

        timings = []
        recognized = 0
        deadline = time.perf_counter() + duration_seconds
        while time.perf_counter() < deadline:
            for image_path in image_paths:
                started = time.perf_counter()
                plate = run_anpr(image_path)
                timings.append(time.perf_counter() - started)
                recognized += plate is not None
                if time.perf_counter() >= deadline:
                    break

        results.put(("done", {
            "images_processed": len(timings),
            "plates_recognized": recognized,
            "mean_seconds": statistics.mean(timings) if timings else 0.0,
            "median_seconds": statistics.median(timings) if timings else 0.0,
        }))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _cpu_temperature_c():
    temperatures = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        zone = Path(path).parent
        try:
            zone_type = (zone / "type").read_text(encoding="ascii").strip().lower()
            if "cpu" not in zone_type:
                continue
            value = int(Path(path).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        temperatures.append(value / 1000 if value > 1000 else value)
    return max(temperatures) if temperatures else None


def _run_benchmark(image_paths, duration_seconds):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    worker = context.Process(
        target=_benchmark_worker,
        args=(image_paths, duration_seconds, results),
    )
    worker.start()

    try:
        status, value = results.get(timeout=900)
    except Exception:
        worker.terminate()
        worker.join()
        raise RuntimeError(f"RapidOCR did not finish loading/warming up within 15 minutes")

    if status == "error":
        worker.join()
        raise RuntimeError(f"RapidOCR failed during warmup: {value}")

    process = psutil.Process(worker.pid)
    process.cpu_percent()
    cpu_samples = []
    peak_memory_mb = 0.0
    temperatures = []

    while worker.is_alive():
        try:
            cpu_samples.append(process.cpu_percent(interval=0.25))
            peak_memory_mb = max(peak_memory_mb, process.memory_info().rss / (1024 * 1024))
        except psutil.Error:
            break
        temperature = _cpu_temperature_c()
        if temperature is not None:
            temperatures.append(temperature)

    worker.join()
    if worker.exitcode != 0:
        raise RuntimeError(f"RapidOCR worker exited with code {worker.exitcode}")

    try:
        status, value = results.get(timeout=5)
    except Exception as exc:
        raise RuntimeError(f"RapidOCR worker finished without a summary") from exc
    if status != "done":
        raise RuntimeError(f"RapidOCR failed: {value}")

    cpu_count = psutil.cpu_count(logical=True) or 1
    value.update({
        "cpu_percent": statistics.mean(cpu_samples) / cpu_count if cpu_samples else 0.0,
        "peak_memory_mb": peak_memory_mb,
        "peak_cpu_temperature_c": max(temperatures) if temperatures else None,
        "temperature_samples": len(temperatures),
    })
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", default="test_images", help="Directory of representative plate images")
    parser.add_argument("--duration", type=int, default=300, help="Measured seconds after warmup")
    args = parser.parse_args()

    image_paths = sorted({
        path
        for pattern in IMAGE_PATTERNS
        for path in glob.glob(os.path.join(args.image_dir, pattern))
    })
    if not image_paths:
        parser.error(f"No supported images found in {args.image_dir!r}")
    if args.duration < 1:
        parser.error("--duration must be at least 1 second")

    print(f"RapidOCR on {len(image_paths)} images for {args.duration}s (after warmup)...")
    result = _run_benchmark(image_paths, args.duration)
    temperature = result["peak_cpu_temperature_c"]
    print(f"\nImages processed: {result['images_processed']}\n")
    print(f"Mean processing time per image (s): {result['mean_seconds']:.3f}")
    print(f"Median processing time per image (s): {result['median_seconds']:.3f}")
    print(f"CPU use during recognition (%): {result['cpu_percent']:.1f}")
    print(f"Memory use (MB): {result['peak_memory_mb']:.1f}")
    print("CPU temperature under continuous operation (°C): "
          + (f"{temperature:.1f}" if temperature is not None else "unavailable"))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

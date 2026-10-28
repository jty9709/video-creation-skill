#!/usr/bin/env python3
"""Read-only video inspection and review evidence. Requires ffmpeg and ffprobe."""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


def number(value):
    try:
        result = float(Fraction(str(value)))
        return result if math.isfinite(result) else None
    except (ValueError, ZeroDivisionError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="New or empty output directory")
    parser.add_argument("--cuts", default="", help="Comma-separated cut/event times in seconds")
    parser.add_argument("--samples", type=int, default=12, help="Overview frame count")
    parser.add_argument("--clips", action="store_true", help="Extract short clips around cuts")
    parser.add_argument("--expect-audio", action="store_true", help="Fail if audio is missing")
    args = parser.parse_args()
    source, out = args.video.expanduser().resolve(), args.out.expanduser().resolve()
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        parser.error("ffmpeg and ffprobe must be available on PATH")
    if not source.is_file():
        parser.error("video must be an existing local file")
    if args.samples < 1:
        parser.error("--samples must be positive")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error("--out must be a new or empty directory; preserve older QA separately")

    probe = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)])
    if probe.returncode:
        parser.error("ffprobe failed: " + probe.stderr.strip())
    info = json.loads(probe.stdout)
    videos = [s for s in info.get("streams", []) if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in info.get("streams", []) if s["codec_type"] == "audio"]
    if not videos:
        parser.error("no video stream found")
    video = videos[0]
    duration = number(video.get("duration")) or number(info.get("format", {}).get("duration"))
    fps = number(video.get("avg_frame_rate")) or number(video.get("r_frame_rate"))
    if not duration or duration <= 0:
        parser.error("could not determine a positive video duration")
    try:
        cuts = sorted(set(float(v.strip()) for v in args.cuts.split(",") if v.strip()))
    except ValueError:
        parser.error("--cuts must contain numbers separated by commas")
    if any(not math.isfinite(t) or t < 0 or t >= duration for t in cuts):
        parser.error("every cut must be within [0, video duration)")

    out.mkdir(parents=True, exist_ok=True)
    frames = out / "frames"
    frames.mkdir()
    with source.open("rb") as stream:
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
        digest = hasher.hexdigest()
    report = {
        "source": str(source), "sha256": digest, "bytes": source.stat().st_size,
        "duration": duration, "fps": fps,
        "width": video.get("width"), "height": video.get("height"),
        "probe": info, "checks": {}, "review_frames": [], "review_clips": [],
        "review_status": "Technical evidence only; visual continuity, meaning and listening review remain required.",
    }
    checks = report["checks"]
    checks["required_audio"] = bool(audios) if args.expect_audio else None
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    scan_args = base + ["-xerror", "-err_detect", "explode", "-i", str(source), "-map", f"0:{video['index']}"]
    if audios:
        scan_args += ["-map", f"0:{audios[0]['index']}"]
    scan_args += ["-vf", "blackdetect=d=0.3:pix_th=0.08,freezedetect=n=-50dB:d=0.8"]
    if audios:
        scan_args += ["-af", "ebur128=peak=true"]
    scan = run(scan_args + ["-f", "null", "-"])
    (out / "inspection.log").write_text(scan.stderr, encoding="utf-8")
    checks["full_decode_and_scan"] = scan.returncode == 0
    report["review_candidates"] = [line.strip() for line in scan.stderr.splitlines()
                                   if re.search(r"black_start:|freeze_(start|duration|end):", line)]
    summary = scan.stderr.rsplit("Summary:", 1)[1].strip() if "Summary:" in scan.stderr else None
    report["loudness_summary"] = summary.split("\n[", 1)[0].strip() if summary else None
    report["measured_audio_stream"] = audios[0]["index"] if audios else None
    if len(videos) + len(audios) > 2:
        report["stream_note"] = "Scan covers the selected primary video and first audio stream; review other tracks separately."

    last = max(0.0, duration - 1 / (fps or 30))
    count = min(args.samples, max(1, math.ceil(duration * (fps or 30))))
    overview = [last * i / (count - 1) if count > 1 else last / 2 for i in range(count)]
    boundaries = [min(last, max(0.0, t + dt)) for t in cuts for dt in (-0.4, -0.1, 0, 0.1, 0.4)]
    times = sorted(set(round(t, 6) for t in overview + boundaries))
    failures = []
    print(f"Scan complete; extracting {len(times)} frames.", flush=True)
    for index, time in enumerate(times):
        target = frames / f"frame_{index:04d}_{time:010.4f}.jpg"
        result = run(base + ["-v", "error", "-ss", str(time), "-i", str(source),
                             "-map", f"0:{video['index']}", "-frames:v", "1", "-vf", "scale=960:-2",
                             "-q:v", "2", str(target)])
        if result.returncode or not target.is_file() or target.stat().st_size == 0:
            failures.append({"step": "frame", "time": time, "error": result.stderr})
        else:
            report["review_frames"].append({"requested_time": time, "file": str(target)})

    if report["review_frames"]:
        contact = out / "contact.jpg"
        rows = math.ceil(len(report["review_frames"]) / 4)
        result = run(base + ["-v", "error", "-framerate", "1", "-pattern_type", "glob",
                             "-i", str(frames / "frame_*.jpg"), "-vf",
                             f"scale=320:-2,tile=4x{rows}:padding=4:margin=4", "-frames:v", "1",
                             "-q:v", "2", str(contact)])
        if result.returncode or not contact.is_file():
            failures.append({"step": "contact", "error": result.stderr})
        else:
            report["contact_sheet"] = str(contact)

    if args.clips:
        for index, time in enumerate(cuts):
            start, end = max(0.0, time - 0.8), min(duration, time + 0.8)
            target = out / f"clip_{index:03d}_{time:010.4f}.mp4"
            clip_args = base + ["-v", "error", "-ss", str(start), "-i", str(source),
                                "-t", str(end - start), "-map", f"0:{video['index']}"]
            if audios:
                clip_args += ["-map", f"0:{audios[0]['index']}"]
            result = run(clip_args + ["-vf", "scale=960:-2", "-c:v", "libx264", "-crf", "20",
                                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(target)])
            if result.returncode or not target.is_file():
                failures.append({"step": "clip", "time": time, "error": result.stderr})
            else:
                report["review_clips"].append({"start": start, "end": end, "file": str(target)})

    checks["evidence_extraction"] = not failures
    report["extraction_failures"] = failures
    report["technical_pass"] = all(value is not False for value in checks.values())
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(out / "report.json"), "checks": checks,
                      "review_candidates": len(report["review_candidates"]),
                      "visual_and_listening_review": "not automatically assessed"}, ensure_ascii=False))
    return 0 if report["technical_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

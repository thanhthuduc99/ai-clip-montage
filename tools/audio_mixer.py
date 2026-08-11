"""Trộn âm thanh sau khi ghép hình: whoosh SFX tại mỗi điểm chuyển cảnh + nhạc nền.
Video giữ nguyên (-c:v copy) nên bước này nhanh."""
from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import config


def _probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def pick_sfx() -> str:
    files = sorted(config.SFX_DIR.glob("*.mp3")) + sorted(config.SFX_DIR.glob("*.wav"))
    return str(files[0]) if files else ""


def pick_bgm(name: str = "random") -> str:
    """name: 'none' | 'random' | tên file trong assets/music."""
    if name == "none":
        return ""
    files = sorted(config.MUSIC_DIR.glob("*.mp3")) + sorted(config.MUSIC_DIR.glob("*.wav"))
    if not files:
        return ""
    if name and name != "random":
        for f in files:
            if f.name == name:
                return str(f)
    return str(random.choice(files))


def mix_audio(video_path: str, out_path: str, transition_offsets: list[float],
              bgm_path: str = "", sfx_path: str = "") -> str:
    """Ghép whoosh tại các offset chuyển cảnh + BGM volume thấp. Không có gì để
    trộn → trả video_path gốc."""
    sfx_path = sfx_path or pick_sfx()
    have_sfx = bool(sfx_path and transition_offsets and Path(sfx_path).exists())
    have_bgm = bool(bgm_path and Path(bgm_path).exists())
    if not have_sfx and not have_bgm:
        return video_path

    dur = _probe_duration(video_path)
    inputs = ["-i", video_path]
    filters: list[str] = []
    mix_labels = ["[0:a]"]
    idx = 1

    if have_sfx:
        for k, off in enumerate(transition_offsets):
            inputs += ["-i", sfx_path]
            delay_ms = max(0, int((off - 0.15) * 1000))
            filters.append(
                f"[{idx}:a]adelay={delay_ms}|{delay_ms},"
                f"volume={config.SFX_VOLUME}[w{k}]"
            )
            mix_labels.append(f"[w{k}]")
            idx += 1

    if have_bgm:
        inputs += ["-stream_loop", "-1", "-i", bgm_path]
        fade_start = max(0.0, dur - 1.5)
        filters.append(
            f"[{idx}:a]volume={config.BGM_VOLUME},"
            f"afade=t=out:st={fade_start:.2f}:d=1.5[bgm]"
        )
        mix_labels.append("[bgm]")
        idx += 1

    fc = (
        ";".join(filters)
        + (";" if filters else "")
        + "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:normalize=0[aout]"
    )
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
           "-map", "0:v", "-c:v", "copy",
           "-map", "[aout]", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"[audio_mixer] lỗi mix ({r.returncode}): {r.stderr[-400:]} — giữ audio gốc",
              file=sys.stderr)
        return video_path
    return out_path

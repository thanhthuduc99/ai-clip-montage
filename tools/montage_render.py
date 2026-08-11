"""Render video montage: mỗi đoạn = VO + 1 clip loop đúng thời lượng + phụ đề khớp
giọng, ghép xfade + acrossfade, rồi trộn whoosh + BGM. Adapt từ list_review_render
của dulich-pipeline.

Spec:
{
  "job_id": "...", "title": "...", "hook_sub": "...",
  "transition": "fade", "voice_provider": "edge", "voice_id": "",
  "bgm": "random" | "none" | "<tên file>",
  "segments": [{"vo": "...", "clip_path": "/abs/path.mp4"}, ...]
}
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

import config
from tools.audio_mixer import mix_audio, pick_bgm
from tools.subtitles import build_caption_png, build_hook_png, timed_cues

W, H = 1080, 1920
FPS = 30
RENDER_DIR = config.OUTPUT_DIR / "renders"
RENDER_DIR.mkdir(parents=True, exist_ok=True)

XFADE_TRANSITIONS = {"fade", "dissolve", "wipeleft", "wiperight",
                     "slideleft", "slideright", "circleopen", "circlecrop"}


class RenderCancelled(RuntimeError):
    pass


def _ffprobe_dur(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _run(cmd, cwd=None, timeout=240):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg quá thời gian ({timeout}s).") from e
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg lỗi: {' '.join(str(c) for c in cmd[:6])}...\n{r.stderr[-1000:]}"
        )
    return r


def _normalize(src: str, dst: str, need_dur: float, allow_loop: bool = True):
    """Scale-cover 1080x1920 30fps, bỏ audio, cắt đúng need_dur tính từ đầu clip.
    allow_loop=False thì clip ngắn hơn cứ để ngắn, phần thiếu do clip khác bù."""
    src_dur = _ffprobe_dur(src)
    cmd = ["ffmpeg", "-y"]
    if allow_loop and 0 < src_dur < need_dur:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", src,
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},fps={FPS}",
            "-t", f"{need_dur:.2f}",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", dst]
    _run(cmd, timeout=300)


def _build_base(clip_paths: list[str], dst: Path, need_dur: float, work: Path,
                idx: int) -> None:
    """Dựng nền hình dài đúng need_dur từ một hoặc nhiều clip.

    Clip đầu chạy hết mình rồi tới clip kế, mỗi clip đều lấy từ giây 0. Clip cuối
    bị cắt ở chỗ vừa đủ. Hết clip mà vẫn thiếu thì clip cuối lặp cho đủ phần còn
    lại — vẫn hơn để màn hình trống.
    """
    usable = [p for p in clip_paths if p and os.path.exists(p)]
    if not usable:
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x0f172a:s={W}x{H}:r={FPS}",
              "-t", f"{need_dur:.2f}", "-c:v", "libx264", "-preset", "veryfast",
              "-pix_fmt", "yuv420p", str(dst)])
        return

    if len(usable) == 1:
        _normalize(usable[0], str(dst), need_dur)
        return

    parts: list[Path] = []
    remaining = need_dur
    for k, path in enumerate(usable):
        if remaining <= 0.05:
            break
        last = k == len(usable) - 1
        take = _ffprobe_dur(path)
        # Clip cuối phải gánh hết phần còn lại, kể cả khi phải lặp.
        take = remaining if (last or take <= 0) else min(take, remaining)
        piece = work / f"part_{idx}_{k}.mp4"
        _normalize(path, str(piece), take, allow_loop=last)
        actual = _ffprobe_dur(str(piece))
        if actual <= 0.05:
            continue
        parts.append(piece)
        remaining -= actual

    if not parts:
        _normalize(usable[0], str(dst), need_dur)
        return
    if len(parts) == 1:
        shutil.move(str(parts[0]), str(dst))
        return

    # Cùng codec/kích thước/fps nên concat demuxer nối được, không phải mã hoá lại.
    listing = work / f"parts_{idx}.txt"
    listing.write_text(
        "".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing.name,
          "-c", "copy", dst.name], cwd=str(work), timeout=300)
    print(f"[montage] đoạn {idx + 1}: ghép {len(parts)} clip cho đủ {need_dur:.1f}s",
          file=sys.stderr)


def _render_segment(idx: int, seg: dict, work: Path, spec: dict) -> str:
    from tools.voice_generator import VoiceGenerator

    vo_text = (seg.get("vo") or "").strip()
    if not vo_text:
        raise RuntimeError(f"Đoạn {idx + 1} không có lời đọc.")

    # 1) Voice — Edge ghi kèm .words.json cho phụ đề
    vg = VoiceGenerator(provider=spec.get("voice_provider") or config.VOICE_PROVIDER)
    vo_path = vg.generate_voice(
        text=vo_text,
        voice_id=spec.get("voice_id") or config.VOICE_ID,
        output_name=f"mv_{spec['job_id']}_{idx}",
        speed=float(spec.get("voice_speed") or 1.08),
    )
    if not vo_path or not os.path.exists(vo_path):
        raise RuntimeError(f"Không tạo được giọng cho đoạn {idx + 1}.")
    dur = max(
        1.2,
        _ffprobe_dur(vo_path) + 0.25,
        float(seg.get("target_duration") or 0),
    )

    # 2) Base video: một hay nhiều clip nối lại cho đủ thời lượng lời đọc
    base = work / f"base_{idx}.mp4"
    clip_paths = seg.get("clip_paths") or []
    if not clip_paths and seg.get("clip_path"):
        clip_paths = [seg["clip_path"]]
    if not any(p and os.path.exists(p) for p in clip_paths):
        print(f"[montage] đoạn {idx + 1} không có clip nào, dùng nền trơn",
              file=sys.stderr)
    _build_base(clip_paths, base, dur, work, idx)

    # 3) Overlay hook (chỉ cảnh đầu, hiện 3s) + phụ đề động
    overlays: list[tuple[str, float, float]] = []   # (png, start, end)
    if idx == 0 and (spec.get("title") or "").strip():
        hook_png = work / "hook.png"
        build_hook_png(spec.get("title", ""), spec.get("hook_sub", ""), str(hook_png))
        overlays.append((hook_png.name, 0.0, min(3.0, dur)))
    for li, (ln, st, en) in enumerate(timed_cues(vo_path, vo_text, dur)):
        p = work / f"cap_{idx}_{li}.png"
        build_caption_png(ln, str(p))
        overlays.append((p.name, st, en))

    # 4) Gộp base + overlays + VO
    out = work / f"seg_{idx:03d}.mp4"
    inputs = ["-i", base.name]
    for nm, _, _ in overlays:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", nm]
    fc_parts = []
    last = "0:v"
    for k, (_, st, en) in enumerate(overlays):
        nxt = f"v{k + 1}"
        fc_parts.append(
            f"[{last}][{k + 1}:v]overlay=0:0:shortest=1:"
            f"enable='between(t,{st:.2f},{en:.2f})'[{nxt}]"
        )
        last = nxt
    audio_idx = 1 + len(overlays)
    fc = ";".join(fc_parts) if fc_parts else "[0:v]copy[v0]"
    if not fc_parts:
        last = "v0"
    _run(["ffmpeg", "-y", *inputs, "-i", vo_path,
          "-filter_complex", fc,
          "-map", f"[{last}]", "-map", f"{audio_idx}:a",
          "-t", f"{dur:.2f}",
          "-af", "apad",
          "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", out.name], cwd=str(work), timeout=360)
    return str(out)


def _concat_with_xfade(segs: list[str], out_path: Path, work: Path,
                       transition: str, d: float) -> list[float]:
    """Ghép segment bằng xfade (hình) + acrossfade (tiếng).
    Trả list offset (giây) của từng điểm chuyển cảnh — dùng đặt whoosh."""
    n = len(segs)
    if n == 1:
        _run(["ffmpeg", "-y", "-i", Path(segs[0]).name,
              "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
              "-movflags", "+faststart",
              "-c:a", "aac", "-b:a", "160k", str(out_path)], cwd=str(work))
        return []
    durs = [max(0.3, _ffprobe_dur(p)) for p in segs]
    inputs = []
    for p in segs:
        inputs += ["-i", Path(p).name]
    parts = [f"[{i}:v]setpts=PTS-STARTPTS[v{i}]" for i in range(n)]
    parts += [f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]" for i in range(n)]
    fc = ";".join(parts)
    offsets: list[float] = []
    prev, off = "v0", 0.0
    for i in range(n - 1):
        off += durs[i] - d
        offsets.append(off)
        out = f"vx{i}"
        fc += f";[{prev}][v{i + 1}]xfade=transition={transition}:duration={d:.3f}:offset={off:.3f}[{out}]"
        prev = out
    aprev = "a0"
    for i in range(n - 1):
        out = f"ax{i}"
        fc += f";[{aprev}][a{i + 1}]acrossfade=d={d:.3f}[{out}]"
        aprev = out
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", fc,
          "-map", f"[{prev}]", "-map", f"[{aprev}]",
          "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
          "-movflags", "+faststart",
          "-c:a", "aac", "-b:a", "160k", str(out_path)], cwd=str(work), timeout=600)
    return offsets


def make_preview(out_path: Path) -> Path | None:
    """Bản 480p nhẹ để phát ngay trong trang output."""
    prev = out_path.with_name(out_path.stem + "_preview.mp4")
    try:
        _run(["ffmpeg", "-y", "-i", str(out_path), "-vf", "scale=-2:854",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
              "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(prev)],
             timeout=600)
        return prev if prev.exists() else None
    except Exception as e:
        print(f"[montage] preview lỗi: {e}", file=sys.stderr)
        return None


def render_montage(spec: dict, cancel_check=None) -> dict:
    job_id = spec.get("job_id", "demo")
    transition = spec.get("transition") or config.TRANSITION
    if transition not in XFADE_TRANSITIONS:
        transition = "fade"
    td = min(max(0.2, float(spec.get("transition_duration")
                            or config.TRANSITION_DURATION)), 0.5)
    segments = spec.get("segments") or []
    if not segments:
        return {"success": False, "error": "Không có đoạn nào để render."}

    target_seconds = max(0.0, float(spec.get("target_seconds") or 0))
    if target_seconds:
        word_counts = [max(1, len(str(s.get("vo") or "").split())) for s in segments]
        total_words = sum(word_counts)
        raw_target = target_seconds + max(0, len(segments) - 1) * td
        # voice_wps là tốc độ đọc thật của giọng đã chọn (xem VOICE_CHOICES).
        # Chỉnh nhịp trong biên tự nhiên; lệch nhỏ còn lại apad bù bằng khoảng nghỉ.
        desired_voice = max(1.0, raw_target - len(segments) * 0.25)
        wps = max(1.5, min(float(spec.get("voice_wps") or 3.05), 8.0))
        voice_speed = min(1.15, max(0.75, total_words / (wps * desired_voice)))
        spec = {**spec, "voice_speed": voice_speed}
        segments = [
            {**segment, "target_duration": raw_target * words / total_words}
            for segment, words in zip(segments, word_counts)
        ]

    def _check_cancel():
        if cancel_check and cancel_check():
            raise RenderCancelled("Job đã được hủy.")

    work = Path(tempfile.mkdtemp(prefix=f"mv_{job_id}_"))
    try:
        seg_paths: list[str] = []
        for i, seg in enumerate(segments):
            _check_cancel()
            print(f"[montage] segment {i + 1}/{len(segments)}...", file=sys.stderr)
            seg_paths.append(_render_segment(i, seg, work, spec))

        _check_cancel()
        raw = work / f"montage_{job_id}_raw.mp4"
        offsets = _concat_with_xfade(seg_paths, raw, work, transition, td)

        _check_cancel()
        out_path = RENDER_DIR / f"montage_{job_id}.mp4"
        bgm = pick_bgm(spec.get("bgm") or "random")
        mixed = mix_audio(str(raw), str(out_path), offsets, bgm_path=bgm)
        if mixed != str(out_path):
            shutil.copy2(raw, out_path)     # mix lỗi/không có gì → giữ bản raw

        thumb_path = out_path.with_suffix(".jpg")
        try:
            _run(["ffmpeg", "-y", "-ss", "1", "-i", str(out_path), "-frames:v", "1",
                  "-vf", "scale=360:-2", "-q:v", "5", str(thumb_path)])
        except Exception:
            thumb_path = None
        prev_path = make_preview(out_path)

        return {
            "success": True,
            "video_path": str(out_path.resolve()),
            "thumb_path": str(thumb_path.resolve()) if thumb_path and thumb_path.exists() else "",
            "preview_path": str(prev_path.resolve()) if prev_path else "",
            "segments": len(seg_paths),
            "duration": _ffprobe_dur(str(out_path)),
            "bgm": Path(bgm).name if bgm else "",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)

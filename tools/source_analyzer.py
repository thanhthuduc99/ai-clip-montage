"""Phân tích 1 clip source: ffmpeg trích frame → OpenRouter vision → mô tả + tags.
Kết quả lưu vào resources kind='source' (pipeline_store)."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import config
from tools.llm import chat_json, image_content


def _prompt(brand_name: str, brand_context: str) -> str:
    who = f"thương hiệu {brand_name}" if brand_name else "một thương hiệu"
    ctx = f"\nBối cảnh thương hiệu: {brand_context}\n" if brand_context else "\n"
    return (
        f"Bạn nhận các khung hình trích đều từ MỘT clip quay cho {who}.{ctx}"
        "Mô tả clip này để sau đó hệ thống chọn đúng clip minh họa cho từng đoạn kịch bản video.\n"
        "Trả DUY NHẤT JSON: {\"description\": \"...\", \"tags\": [\"...\"], \"location_guess\": \"...\"}.\n"
        "- description: 1-3 câu tiếng Việt, nói rõ cảnh gì, chủ thể nào, chi tiết nổi bật, "
        "không khí/ánh sáng, góc quay (cận/toàn/lia/flycam), có người hay không, có động tác gì.\n"
        "- tags: 4-8 tag ngắn tiếng Việt, nối bằng gạch ngang (vd: can-canh-san-pham, khong-gian-quan, "
        "tho-lam-viec, anh-sang-am, co-khach).\n"
        "- location_guess: nơi chốn nếu nhận ra (trong xưởng, trong quán, ngoài trời...), "
        "không chắc thì để chuỗi rỗng."
    )


def probe_media(path: str) -> dict:
    """duration + width/height qua ffprobe. Lỗi → raise (clip hỏng)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe lỗi: {r.stderr[-200:]}")
    data = json.loads(r.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0.3:
        raise RuntimeError("Clip quá ngắn hoặc không đọc được thời lượng.")
    return {
        "duration": round(duration, 2),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


def extract_frames(path: str, count: int, work: Path, dur: float) -> list[Path]:
    """Trích `count` frame chia đều thời lượng, scale nhỏ (720 cạnh dài) cho rẻ token."""
    frames: list[Path] = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        out = work / f"frame_{i:02d}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
             "-vf", "scale='if(gt(iw,ih),720,-2)':'if(gt(iw,ih),-2,720)'",
             "-q:v", "6", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 500:
            frames.append(out)
    if not frames:
        raise RuntimeError("Không trích được frame nào từ clip.")
    return frames


def analyze_source(path: str, brand_name: str = "", brand_context: str = "") -> dict:
    """Trả {description, tags, location_guess, duration, width, height}."""
    info = probe_media(path)
    with tempfile.TemporaryDirectory(prefix="analyze_") as tmp:
        frames = extract_frames(path, config.ANALYZE_FRAMES, Path(tmp), info["duration"])
        content: list[dict] = [{"type": "text", "text": _prompt(brand_name, brand_context)}]
        for f in frames:
            b64 = base64.b64encode(f.read_bytes()).decode("ascii")
            content.append(image_content(b64))
        result = chat_json(
            [{"role": "user", "content": content}],
            model=config.VISION_MODEL,
            temperature=0.3,
        )
    description = str(result.get("description") or "").strip()
    if not description:
        raise RuntimeError("Vision không trả mô tả.")
    tags = [str(t).strip() for t in (result.get("tags") or []) if str(t).strip()]
    print(f"[analyze] {Path(path).name}: {description[:80]}...", file=sys.stderr)
    return {
        "description": description,
        "tags": tags[:8],
        "location_guess": str(result.get("location_guess") or "").strip(),
        **info,
    }

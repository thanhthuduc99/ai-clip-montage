"""Phụ đề khớp giọng — copy từ dulich-pipeline (list_review_render + subtitle_agent).

Nguồn timing: Edge TTS ghi sẵn <vo>.words.json (word timestamps). Không có words
→ fallback chia theo câu tỉ lệ ký tự (_proportional_cues). Không phụ thuộc Whisper.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config

W, H = 1080, 1920
CAPTION_FONT = config.FONT_DIR / "BeVietnamPro-Bold-full.ttf"

_END_PUNCT = (".", "!", "?", "…", ":", ";")
_MID_PUNCT = (",",)


def _font(path: Path, size: int):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


# ── Word timing (Edge words.json) ────────────────────────────────────────────

def _word_timings(vo_path: str) -> list[dict] | None:
    wp = Path(vo_path).with_suffix(".words.json")
    if wp.exists():
        try:
            words = json.loads(wp.read_text(encoding="utf-8"))
            if words:
                return words
        except Exception:
            pass
    return None


def _valid_timings(words: list[dict], vo_text: str, dur: float) -> bool:
    if not words:
        return False
    first_start = float(words[0]["start"])
    last_end = float(words[-1]["end"])
    if first_start > 1.2:
        return False
    if (last_end / max(dur, 0.1)) < 0.75:
        return False
    if (last_end - first_start) / max(dur, 0.1) < 0.6:
        return False
    if vo_text:
        expect = max(1, len(vo_text.split()))
        if len(words) < 0.75 * expect:
            return False
    return True


# ── Gắn lại dấu câu từ kịch bản gốc (subtitle_agent) ─────────────────────────

def clean_word(w: str) -> str:
    return "".join(re.findall(r"[\w\d]+", w.lower(), re.UNICODE))


def align_words_with_punctuation(words: list[dict], full_speech_text: str) -> list[dict]:
    """Căn từ TTS (không dấu câu) với text gốc (đủ dấu câu); gom ký hiệu không đọc
    vào từ kề bên."""
    tokens = full_speech_text.split()
    aligned_words: list[dict] = []
    i = 0
    j = 0
    last_valid_timing = {"start": 0.0, "end": 0.0}
    pending_prefix: list[str] = []

    while i < len(tokens):
        tok = tokens[i]
        clean_tok = clean_word(tok)

        if clean_tok == "" and (j >= len(words) or tok != words[j]["word"]):
            if aligned_words:
                aligned_words[-1]["word"] += " " + tok
            else:
                pending_prefix.append(tok)
            i += 1
            continue

        if j >= len(words):
            aligned_words.append({
                "start": last_valid_timing["start"],
                "end": last_valid_timing["end"],
                "word": tok,
            })
            i += 1
            continue

        tw = words[j]["word"]
        clean_tw = clean_word(tw)
        is_match = False
        matched_i = i
        matched_j = j

        if (clean_tok == clean_tw and clean_tok != "") or (tok == tw):
            is_match = True
        else:
            found = False
            best_sum = 999
            for offset_i in range(15):
                for offset_j in range(15):
                    if offset_i == 0 and offset_j == 0:
                        continue
                    ti = i + offset_i
                    wj = j + offset_j
                    if ti < len(tokens) and wj < len(words):
                        c_t = clean_word(tokens[ti])
                        c_w = clean_word(words[wj]["word"])
                        if (c_t == c_w and c_t != "") or (tokens[ti] == words[wj]["word"]):
                            if offset_i + offset_j < best_sum:
                                best_sum = offset_i + offset_j
                                matched_i = ti
                                matched_j = wj
                                found = True
            if found:
                is_match = True
                for skip_i in range(i, matched_i):
                    aligned_words.append({
                        "start": words[j]["start"],
                        "end": words[j]["end"],
                        "word": tokens[skip_i],
                    })
                i = matched_i
                j = matched_j
                tok = tokens[i]

        timing = {"start": words[j]["start"], "end": words[j]["end"]}
        word_str = tok
        if pending_prefix:
            word_str = " ".join(pending_prefix + [tok])
            pending_prefix = []
        aligned_words.append({"start": timing["start"], "end": timing["end"], "word": word_str})
        last_valid_timing = timing
        i += 1
        j += 1

    return aligned_words


# ── Gom từ thành cue ─────────────────────────────────────────────────────────

def _group_words(words: list[dict], dur: float,
                 min_words: int = 3, max_words: int = 5,
                 pause: float = 0.30, glue: float = 0.08) -> list[tuple[str, float, float]]:
    """Nhóm từ thành cue 3-6 chữ, cắt sau dấu câu / khoảng nghỉ giọng."""
    cues, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 999.0
        tok = (w.get("word") or "").strip()
        end_p = tok.endswith(_END_PUNCT)
        mid_p = tok.endswith(_MID_PUNCT)
        cut = (
            nxt is None
            or end_p
            or (mid_p and len(cur) >= min_words)
            or (gap >= pause and len(cur) >= min_words)
            or (len(cur) >= max_words and gap >= glue)
        )
        over = len(cur) >= max_words + 3
        if cut or over:
            carry = []
            if over and not cut and nxt is not None:
                for k in range(len(cur) - 1, 0, -1):
                    if (cur[k]["start"] - cur[k - 1]["end"]) >= glue:
                        carry = cur[k:]
                        cur = cur[:k]
                        break
            text = " ".join(x["word"].strip() for x in cur if x["word"].strip())
            st = float(cur[0]["start"])
            en = min(float(cur[-1]["end"]) + 0.25, dur)
            nxt_start = float(carry[0]["start"]) if carry else (float(nxt["start"]) if nxt else None)
            if nxt_start is not None:
                en = min(en, nxt_start)
            if text:
                cues.append((text, st, max(en, st + 0.35)))
            cur = carry
    return cues


def _proportional_cues(vo_text: str, dur: float) -> list[tuple[str, float, float]]:
    """Fallback: chia câu → cue 3-5 chữ, phân thời gian theo tỉ lệ ký tự."""
    text = unicodedata.normalize("NFC", (vo_text or "").strip())
    if not text or dur <= 0:
        return []
    sents = [s.strip() for s in re.split(r"(?<=[\.\!\?…])\s+|\n+", text) if s.strip()]
    if not sents:
        sents = [text]
    total = sum(len(s) for s in sents) or 1
    cues: list[tuple[str, float, float]] = []
    t = 0.0
    for s in sents:
        seg = dur * (len(s) / total)
        s0, s1 = t, min(dur, t + seg)
        t = s1
        words = s.split()
        chunks, i = [], 0
        while i < len(words):
            chunks.append(words[i:i + 5])
            i += 5
        cw = sum(len(c) for c in chunks) or 1
        ct = s0
        for c in chunks:
            frac = len(c) / cw
            c0, c1 = ct, min(s1, ct + seg * frac)
            ct = c1
            cues.append((" ".join(c), c0, max(c1, c0 + 0.3)))
    return cues


def _no_overlap(cues: list[tuple[str, float, float]],
                gap: float = 0.06) -> list[tuple[str, float, float]]:
    """Cue trước phải kết thúc trước cue sau 1 khe nhỏ (between() tính cả 2 đầu mút)."""
    out = []
    for i, (txt, st, en) in enumerate(cues):
        if i + 1 < len(cues):
            en = min(en, cues[i + 1][1] - gap)
        out.append((txt, st, max(en, st + 0.2)))
    return out


def timed_cues(vo_path: str, vo_text: str, dur: float) -> list[tuple[str, float, float]]:
    """Cue (text, start, end): ưu tiên words.json của Edge, fallback chia theo câu."""
    words = _word_timings(vo_path)
    if words and not _valid_timings(words, vo_text, dur):
        words = None
    if words:
        try:
            if vo_text:
                words = align_words_with_punctuation(words, vo_text) or words
            out = _group_words(words, dur)
            if out:
                return _no_overlap(out)
        except Exception:
            pass
    return _no_overlap(_proportional_cues(vo_text, dur))


# ── Caption PNG ──────────────────────────────────────────────────────────────

def build_caption_png(text: str, out_path: str, max_w: int = 980) -> str:
    """1 dòng phụ đề → PNG trong suốt, chữ trắng viền đen, canh giữa ~80% H."""
    text = unicodedata.normalize("NFC", (text or "").strip())
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    size = 62
    while size >= 40:
        f = _font(CAPTION_FONT, size)
        b = d.textbbox((0, 0), text, font=f, stroke_width=6)
        if b[2] - b[0] <= max_w:
            break
        size -= 4
    f = _font(CAPTION_FONT, size)
    cx, y = W // 2, int(H * 0.80)
    d.text((cx, y), text, font=f, fill=(255, 255, 255, 255),
           stroke_width=6, stroke_fill=(0, 0, 0, 255), anchor="ms")
    canvas.save(out_path)
    return out_path


def build_hook_png(title: str, subtitle: str, out_path: str) -> str:
    """Khung hook đơn giản cho cảnh đầu: title to giữa trên + dòng phụ."""
    title = unicodedata.normalize("NFC", (title or "").strip())
    subtitle = unicodedata.normalize("NFC", (subtitle or "").strip())
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    size = 110
    while size >= 60:
        f = _font(CAPTION_FONT, size)
        b = d.textbbox((0, 0), title, font=f, stroke_width=8)
        if b[2] - b[0] <= 960:
            break
        size -= 6
    f = _font(CAPTION_FONT, size)
    cx, ty = W // 2, int(H * 0.16)
    d.text((cx, ty), title, font=f, fill=(255, 255, 255, 255),
           stroke_width=8, stroke_fill=(0, 0, 0, 255), anchor="ms")

    if subtitle:
        sf = _font(CAPTION_FONT, 48)
        # wrap dòng phụ nếu dài
        wordsplit = subtitle.split()
        lines, cur = [], ""
        for wd in wordsplit:
            trial = (cur + " " + wd).strip()
            if d.textbbox((0, 0), trial, font=sf, stroke_width=5)[2] <= 940:
                cur = trial
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        y = ty + 40
        for ln in lines[:3]:
            d.text((cx, y + 50), ln, font=sf, fill=(255, 235, 59, 255),
                   stroke_width=5, stroke_fill=(0, 0, 0, 255), anchor="ms")
            y += 62
    canvas.save(out_path)
    return out_path

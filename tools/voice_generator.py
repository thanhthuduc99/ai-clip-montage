"""
Voice Generator — Generates voiceover audio.
Providers:
  - Vbee.ai   (primary — Vietnamese TTS, mock mode nếu chưa có API key)
  - ElevenLabs (secondary — bilingual, instant voice clone)
  - Mock       (fallback — silent WAV, dùng khi không có API key nào)
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout as FileLockTimeout

# ── ElevenLabs (optional) ─────────────────────────────────────────────────────
try:
    from elevenlabs import save
    from elevenlabs.client import ElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    ELEVENLABS_AVAILABLE = False

# ── requests (for Vbee REST API) ──────────────────────────────────────────────
try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

OUTPUT_DIR = Path("./output/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Vbee.ai real voice codes (Realtime API) ──────────────────────────────────
VBEE_STANDARD_VOICES = {
    "hn_female_ngochuyen": "hn_female_ngochuyen_full_48k-fhg",   # HN nữ Ngọc Huyền
    "hn_female_maiphuong": "hn_female_maiphuong_vdts_48k-fhg",   # HN nữ Mai Phương
    "sg_female_tuongvy": "sg_female_tuongvy_call_48k-fhg",       # SG nữ Tường Vy
    "sg_female_lantrinh": "sg_female_lantrinh_48k-fhg",          # SG nữ Lan Trinh
    # accept already-full codes as-is
    "default": "hn_female_ngochuyen_full_48k-fhg",
}

VBEE_API_URL = "https://api.vbee.vn/v1/tts"  # Realtime (sync) API

# Public, verified voices from https://www.vivibe.app/voice-library.
# Short aliases are sent by the web UI; every ID can be overridden in .env.
VIVIBE_STANDARD_VOICES = {
    "thu_review": "orBfJ4Q68FyVbckjJgDvkj",
    "trinh_review": "uCMfUVPwStduZMyFC7iuQv",
    "my_review": "vcXEe1p3FxPfpswf3BhwbG",
    "adam_3": "5r2MVjMfzwsSDzTpaLjbY9",
}
VIVIBE_API_URL = "https://api.lucylab.io/json-rpc"


class VoiceGenerationError(RuntimeError):
    """An explicitly selected voice provider could not produce valid audio."""


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _vivibe_lock_path(api_key: str) -> Path:
    """Return one shared, non-secret lock path for every process using this key."""
    root = Path(__file__).resolve().parent.parent
    lock_dir = Path(
        os.getenv("VIVIBE_LOCK_DIR")
        or (root / "data" / "provider-locks")
    )
    lock_dir.mkdir(parents=True, exist_ok=True)
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
    return lock_dir / f"vivibe-{key_hash}.lock"


def _split_for_tts(text: str, limit: int = 280) -> list[str]:
    """Split text into <=limit-char chunks at sentence/space boundaries (Vbee sync caps ~300)."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    import re as _re
    sentences = _re.split(r"(?<=[.!?…])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > limit:  # a single very long sentence → hard split by space
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(s[:cut].strip()); s = s[cut:].strip()
        if len(cur) + len(s) + 1 <= limit:
            cur = (cur + " " + s).strip()
        else:
            if cur: chunks.append(cur)
            cur = s
    if cur: chunks.append(cur)
    return [c for c in chunks if c]

import re

def _split_sentences_vi(text: str, max_len: int = 90) -> list[str]:
    """Tách text thành câu ngắn để Vbee synth đúng (text dài Vbee hay rớt câu).
    Cắt sau . ! ? … và xuống dòng; câu vẫn quá dài thì cắt thêm ở dấu phẩy."""
    import re as _re
    text = (text or "").strip()
    if not text:
        return []
    raw = _re.split(r"(?<=[\.\!\?…])\s+|\n+", text)
    out: list[str] = []
    for s in raw:
        s = s.strip()
        if not s:
            continue
        if len(s) <= max_len:
            out.append(s)
            continue
        # câu dài → cắt tiếp theo dấu phẩy, gộp cho tới max_len
        cur = ""
        for part in _re.split(r"(?<=,)\s+", s):
            if cur and len(cur) + len(part) + 1 > max_len:
                out.append(cur.strip()); cur = part
            else:
                cur = (cur + " " + part).strip() if cur else part
        if cur.strip():
            out.append(cur.strip())
    # gộp mẩu quá ngắn (<8 ký tự) vào câu trước để tránh audio vụn
    merged: list[str] = []
    for s in out:
        if merged and len(s) < 8:
            merged[-1] = merged[-1] + " " + s
        else:
            merged.append(s)
    return merged


def _concat_mp3(files: list, out_path) -> bool:
    """Ghép nhiều mp3 thành 1 bằng ffmpeg (concat demuxer, re-encode cho đồng nhất)."""
    import subprocess, tempfile, os as _os
    from pathlib import Path as _P
    files = [str(f) for f in files if f and _P(str(f)).exists()]
    if not files:
        return False
    if len(files) == 1:
        _P(str(out_path)).write_bytes(_P(files[0]).read_bytes())
        return True
    lst = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for f in files:
            lst.write(f"file '{_P(f).resolve().as_posix()}'\n")
        lst.close()
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst.name,
             "-c:a", "libmp3lame", "-q:a", "4", str(out_path)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[Voice] ⚠ concat mp3 lỗi: {r.stderr[-300:]}", file=sys.stderr)
            return False
        return _P(str(out_path)).exists()
    finally:
        try:
            _os.unlink(lst.name)
        except Exception:
            pass


def clean_text_for_tts(text: str) -> str:
    """Remove emojis, special characters, and double spaces from text for clean TTS generation."""
    # Remove emojis and icons
    pattern = re.compile(
        r"[\U00010000-\U0010ffff]"  # Emoji range
        r"|[\u2600-\u27BF]"          # Misc symbols & dingbats
        r"|[\uE000-\uF8FF]"          # Private use area
        r"|[\u200D\uFE0F]"           # Zero-width joiner & variation selector
    )
    text = pattern.sub("", text)
    # Replace double spaces with single space
    text = re.sub(r"\s+", " ", text).strip()
    return text

class VoiceGenerator:
    """
    Unified voice generator.
    Chọn provider theo thứ tự: vbee → elevenlabs → mock
    """

    def __init__(self, provider: str = "vbee"):
        self.provider = provider
        self._el_client: Optional[ElevenLabs] = None
        self._vbee_key: str = os.getenv("VBEE_API_KEY", "")
        self._vbee_app_id: str = os.getenv("VBEE_APP_ID", "")
        self._vivibe_key: str = os.getenv("API_VIVIBE_KEY", "")
        self._vivibe_url: str = os.getenv("VIVIBE_API_URL", VIVIBE_API_URL)
        self._vivibe_voices = {
            "thu_review": os.getenv(
                "VIVIBE_VOICE_THU_REVIEW_ID",
                VIVIBE_STANDARD_VOICES["thu_review"],
            ),
            "trinh_review": os.getenv(
                "VIVIBE_VOICE_TRINH_REVIEW_ID",
                VIVIBE_STANDARD_VOICES["trinh_review"],
            ),
            "my_review": os.getenv(
                "VIVIBE_VOICE_MY_REVIEW_ID",
                VIVIBE_STANDARD_VOICES["my_review"],
            ),
            "adam_3": os.getenv(
                "VIVIBE_VOICE_ADAM_3_ID",
                VIVIBE_STANDARD_VOICES["adam_3"],
            ),
        }
        self._el_key: str = os.getenv("ELEVENLABS_API_KEY", "")

        if provider == "elevenlabs" and ELEVENLABS_AVAILABLE and self._el_key:
            self._el_client = ElevenLabs(api_key=self._el_key)

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_voice(
        self,
        text: str,
        voice_id: str = "default",
        output_name: str = "voice_output",
        language: str = "vi",
        speed: float = 1.0,
    ) -> str:
        """
        Generate TTS audio and save to output/audio/.
        Returns absolute path to generated audio file.
        """
        text = clean_text_for_tts(text)
        print(f"[Voice] generate_voice: provider={self.provider}, voice_id={voice_id}, speed={speed}", file=sys.stderr)
        print(f"[Voice] text (len={len(text)}): {text}", file=sys.stderr)

        # Try Vbee first
        if self.provider in ("vbee", "auto"):
            if self._vbee_key and self._vbee_app_id:
                path = self._vbee_generate(text, voice_id, output_name, speed)
                if path:
                    return path
                print("[Voice] Vbee thất bại — fallback sang Edge TTS.")
                path = self._edge_generate(text, voice_id, output_name, speed)
                if path:
                    return path
            else:
                print("[Voice] Thiếu VBEE_APP_ID/VBEE_API_KEY — fallback Edge TTS (free).")
                path = self._edge_generate(text, voice_id, output_name, speed)
                if path:
                    return path

        # Try Google gTTS
        if self.provider in ("gtts", "google"):
            path = self._gtts_generate(text, output_name, speed)
            if path:
                return path
            print("[Voice] gTTS thất bại — fallback sang Edge TTS.")
            path = self._edge_generate(text, voice_id, output_name, speed)
            if path:
                return path

        # Vivibe / LucyLab community voices
        if self.provider in ("vivibe", "lucylab"):
            if not self._vivibe_key:
                raise VoiceGenerationError(
                    "Thiếu API_VIVIBE_KEY; không thể tạo đúng giọng ViVibe đã chọn."
                )
            path = self._vivibe_generate(text, voice_id, output_name, speed)
            if path:
                return path
            raise VoiceGenerationError(
                "ViVibe không trả âm thanh; không dùng Edge TTS thay thế."
            )

        # Google Cloud TTS Chirp 3 HD — giọng Việt tự nhiên, tạo ~2-3s (nhanh hơn Vbee nhiều)
        if self.provider == "chirp":
            path = self._chirp_generate(text, voice_id, output_name, speed)
            if path:
                return path
            print("[Voice] Chirp thất bại — fallback sang Edge TTS.")
            path = self._edge_generate(text, voice_id, output_name, speed)
            if path:
                return path

        # Try OpenAI
        if self.provider == "openai":
            path = self._openai_generate(text, voice_id, output_name)
            if path:
                return path

        # Try Edge TTS explicitly
        if self.provider == "edge":
            path = self._edge_generate(text, voice_id, output_name, speed)
            if path:
                return path

        # Try ElevenLabs
        if self.provider in ("elevenlabs", "auto") and self._el_client:
            path = self._elevenlabs_generate(text, voice_id, output_name, speed)
            if path:
                return path

        # Fallback: edge-tts (free) first, then mock WAV
        print("[Voice] Thử tạo giọng nói với Microsoft Edge TTS làm fallback...")
        path = self._edge_generate(text, voice_id, output_name, speed)
        if path:
            return path

        # Last resort: silent mock WAV
        return self._mock_generate(output_name, duration_sec=max(5, len(text) // 10))

    def clone_voice(self, audio_samples: list[str], name: str) -> str:
        """
        Clone a voice from sample files.
        Only supported with ElevenLabs.
        Returns voice_id string.
        """
        if self._el_client:
            voice = self._el_client.clone(name=name, files=audio_samples)
            return voice.voice_id

        print(f"[Voice] Voice cloning not available (ElevenLabs key missing). Returning mock ID.")
        return f"mock_clone_{name.lower().replace(' ', '_')}"

    # ── Vbee.ai ───────────────────────────────────────────────────────────────

    def _vbee_generate(
        self,
        text: str,
        voice_id: str,
        output_name: str,
        speed: float = 1.0,
    ) -> Optional[str]:
        if not (self._vbee_key and self._vbee_app_id) or not REQUESTS_AVAILABLE:
            return None

        # Resolve voice code: accept a ready full code (dấu '-', đuôi 'fhg', hoặc code clone '..._vc'
        # / chứa 'zero_shot'/'advertise') — dùng nguyên; còn lại map short id.
        vid = (voice_id or "")
        if vid and ("-" in vid or vid.endswith("fhg") or vid.endswith("_vc")
                    or "zero_shot" in vid or "advertise" in vid):
            voice_code = vid
        else:
            voice_code = VBEE_STANDARD_VOICES.get(vid, VBEE_STANDARD_VOICES["default"])

        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        sp = max(0.5, min(1.5, float(speed)))

        # Vbee LỖI với text dài (rớt câu / chèn watermark "subscribe…"): tách theo câu,
        # synth từng câu (text ngắn luôn đúng) rồi ghép lại. Text ngắn → 1 request như cũ.
        parts = _split_sentences_vi(text)
        try:
            if len(parts) <= 1:
                data = self._vbee_once(parts[0] if parts else text, voice_code, sp)
                if not data:
                    return None
                output_path.write_bytes(data)
            else:
                seg_files = []
                tmp = OUTPUT_DIR / f"{output_name}_parts"
                tmp.mkdir(parents=True, exist_ok=True)
                for i, s in enumerate(parts):
                    d = self._vbee_once(s, voice_code, sp)
                    if not d:
                        print(f"[Voice] ⚠ Vbee rớt câu {i}: {s[:40]!r}", file=sys.stderr)
                        return None
                    fp = tmp / f"{i:02d}.mp3"
                    fp.write_bytes(d)
                    seg_files.append(fp)
                if not _concat_mp3(seg_files, output_path):
                    return None
                import shutil as _sh
                _sh.rmtree(tmp, ignore_errors=True)
            print(f"[Voice] ✓ Vbee TTS saved: {output_path} (voice={voice_code}, {len(parts)} câu)", file=sys.stderr)
            return str(output_path.resolve())
        except Exception as e:
            print(f"[Voice] ⚠ Vbee lỗi: {e}", file=sys.stderr)
            return None

    def _vbee_once(self, text: str, voice_code: str, sp: float) -> Optional[bytes]:
        """1 request Vbee cho 1 đoạn NGẮN (1 câu) → trả bytes mp3. Poll tới SUCCESS."""
        api = "https://vbee.vn/api/v1/tts"   # endpoint chính thức (api.vbee.vn cũ trả HTML)
        headers = {"Authorization": f"Bearer {self._vbee_key}", "Content-Type": "application/json"}
        import time as _time
        r = _requests.post(api, headers=headers, timeout=60, json={
            "app_id": self._vbee_app_id,
            "input_text": text,
            "voice_code": voice_code,
            "speed_rate": str(sp),
            "audio_type": "mp3",
            "response_type": "indirect",
            "callback_url": "https://example.com/vbee-cb",
        })
        if r.status_code not in (200, 201):
            print(f"[Voice] ⚠ Vbee submit {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        j = r.json()
        res = j.get("result") or {}
        rid = res.get("request_id") or j.get("request_id")
        link = res.get("audio_link") or j.get("audio_link")
        if not rid and not link:
            print(f"[Voice] ⚠ Vbee không trả request_id: {str(j)[:200]}", file=sys.stderr)
            return None
        if not link:
            for _ in range(45):
                _time.sleep(2)
                g = _requests.get(f"{api}/{rid}", headers=headers, timeout=30).json()
                gr = g.get("result") or {}
                st = (gr.get("status") or "").upper()
                if st == "SUCCESS":
                    link = gr.get("audio_link"); break
                if st in ("FAILED", "ERROR"):
                    print(f"[Voice] ⚠ Vbee FAILED: {str(g)[:200]}", file=sys.stderr); return None
        if not link:
            print("[Voice] ⚠ Vbee timeout chờ audio.", file=sys.stderr); return None
        a = _requests.get(link, timeout=60); a.raise_for_status()
        if len(a.content) < 500:
            return None
        return a.content

    # ── Vivibe / LucyLab JSON-RPC ───────────────────────────────────────────

    def _vivibe_generate(
        self,
        text: str,
        voice_id: str,
        output_name: str,
        speed: float = 1.0,
    ) -> str:
        if not self._vivibe_key or not REQUESTS_AVAILABLE:
            raise VoiceGenerationError(
                "Thiếu API_VIVIBE_KEY hoặc thư viện requests cho ViVibe."
            )

        resolved_voice_id = self._vivibe_voices.get(
            voice_id or "thu_review",
            voice_id or self._vivibe_voices["thu_review"],
        )
        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        sp = max(0.5, min(2.0, float(speed)))
        lock_path = _vivibe_lock_path(self._vivibe_key)
        wait_seconds = _env_float(
            "VIVIBE_LOCK_TIMEOUT_SECONDS", 900.0, 1.0, 3600.0
        )
        lock = FileLock(str(lock_path))
        wait_started = __import__("time").monotonic()
        print(
            f"[Voice] ViVibe chờ khóa dùng chung (voice={resolved_voice_id})",
            file=sys.stderr,
        )
        try:
            with lock.acquire(timeout=wait_seconds, poll_interval=0.25):
                waited = __import__("time").monotonic() - wait_started
                print(
                    f"[Voice] ViVibe đã tới lượt sau {waited:.1f}s "
                    f"(voice={resolved_voice_id})",
                    file=sys.stderr,
                )
                data = self._vivibe_once(text, resolved_voice_id, sp)
                self._write_vivibe_mp3(data, output_path)
        except FileLockTimeout as exc:
            raise VoiceGenerationError(
                f"ViVibe timeout chờ lượt sau {wait_seconds:.0f} giây."
            ) from exc
        print(
            f"[Voice] ✓ Vivibe TTS saved: {output_path} "
            f"(voice={resolved_voice_id})",
            file=sys.stderr,
        )
        return str(output_path.resolve())

    @staticmethod
    def _write_vivibe_mp3(data: bytes, output_path: Path) -> None:
        """Atomically normalize ViVibe audio (currently WAV) to a real MP3."""
        if not data or len(data) < 500:
            raise VoiceGenerationError("ViVibe trả file âm thanh rỗng hoặc quá nhỏ.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = ""
        encoded_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="vivibe-raw-",
                suffix=".audio",
                dir=output_path.parent,
                delete=False,
            ) as raw_file:
                raw_file.write(data)
                raw_path = raw_file.name
            with tempfile.NamedTemporaryFile(
                prefix=f".{output_path.stem}-",
                suffix=".mp3",
                dir=output_path.parent,
                delete=False,
            ) as encoded_file:
                encoded_path = encoded_file.name
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    raw_path,
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    encoded_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            encoded = Path(encoded_path)
            if result.returncode != 0 or not encoded.is_file() or encoded.stat().st_size < 500:
                raise VoiceGenerationError(
                    "Không chuyển được âm thanh ViVibe sang MP3: "
                    + (result.stderr or "file đầu ra không hợp lệ")[-300:]
                )
            os.replace(encoded_path, output_path)
            encoded_path = ""
        except FileNotFoundError as exc:
            raise VoiceGenerationError(
                "Thiếu FFmpeg nên không thể chuẩn hóa âm thanh ViVibe sang MP3."
            ) from exc
        finally:
            for temporary in (raw_path, encoded_path):
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    def _vivibe_once(
        self,
        text: str,
        voice_id: str,
        speed: float,
    ) -> bytes:
        """Generate one ViVibe export while the caller holds the shared key lock."""
        import time as _time

        headers = {
            "Authorization": f"Bearer {self._vivibe_key}",
            "X-API-Key": self._vivibe_key,
            "Content-Type": "application/json",
        }
        busy_seconds = _env_float(
            "VIVIBE_BUSY_RETRY_SECONDS", 180.0, 0.0, 900.0
        )
        busy_deadline = _time.monotonic() + busy_seconds
        while True:
            response = _requests.post(
                self._vivibe_url,
                headers=headers,
                timeout=120,
                json={
                    "method": "ttsLongText",
                    "input": {
                        "text": text,
                        "userVoiceId": voice_id,
                        "speed": speed,
                    },
                },
            )
            if response.status_code not in (200, 201):
                raise VoiceGenerationError(
                    f"ViVibe submit lỗi HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )

            payload = response.json()
            error = payload.get("error")
            if not error:
                break
            message = (
                str(error.get("message") or error)
                if isinstance(error, dict)
                else str(error)
            )
            if (
                "export in progress" in message.lower()
                and _time.monotonic() < busy_deadline
            ):
                remaining = busy_deadline - _time.monotonic()
                sleep_seconds = min(
                    _env_float(
                        "VIVIBE_POLL_INTERVAL_SECONDS", 2.0, 0.5, 10.0
                    ),
                    max(0.0, remaining),
                )
                print(
                    "[Voice] ViVibe còn export bên ngoài app; "
                    f"đợi {sleep_seconds:.1f}s rồi thử lại.",
                    file=sys.stderr,
                )
                if sleep_seconds:
                    _time.sleep(sleep_seconds)
                continue
            raise VoiceGenerationError(f"ViVibe API lỗi: {message[:240]}")

        submit_result = payload.get("result") or {}
        audio_url = submit_result.get("url")
        export_id = submit_result.get("projectExportId")
        if not audio_url and not export_id:
            raise VoiceGenerationError(
                "ViVibe không trả projectExportId: " + str(payload)[:200]
            )

        timeout_seconds = _env_float(
            "VIVIBE_EXPORT_TIMEOUT_SECONDS", 180.0, 15.0, 600.0
        )
        poll_seconds = _env_float(
            "VIVIBE_POLL_INTERVAL_SECONDS", 2.0, 0.5, 10.0
        )
        deadline = _time.monotonic() + timeout_seconds
        while not audio_url and _time.monotonic() < deadline:
            status_response = _requests.post(
                self._vivibe_url,
                headers=headers,
                timeout=30,
                json={
                    "method": "getExportStatus",
                    "input": {"projectExportId": export_id},
                },
            )
            if status_response.status_code not in (200, 201):
                raise VoiceGenerationError(
                    f"ViVibe status lỗi HTTP {status_response.status_code}: "
                    f"{status_response.text[:200]}"
                )

            status_payload = status_response.json()
            if status_payload.get("error"):
                raise VoiceGenerationError(
                    "ViVibe status lỗi: "
                    + str(status_payload["error"])[:200]
                )

            status = status_payload.get("result") or {}
            state = str(status.get("state", "")).lower()
            audio_url = (
                status.get("url")
                or status.get("mp3Url")
                or status.get("audioUrl")
            )
            if audio_url:
                break
            if state in ("failed", "canceled", "cancelled", "error"):
                raise VoiceGenerationError(
                    f"ViVibe export {state}: "
                    f"{str(status.get('error', ''))[:200]}"
                )
            _time.sleep(poll_seconds)

        if not audio_url:
            raise VoiceGenerationError(
                f"ViVibe timeout chờ audio ({timeout_seconds:.0f}s)."
            )

        audio = _requests.get(audio_url, timeout=120)
        audio.raise_for_status()
        if len(audio.content) < 500:
            raise VoiceGenerationError("ViVibe trả file âm thanh quá nhỏ.")
        return audio.content

    # ── ElevenLabs ───────────────────────────────────────────────────────────

    def _elevenlabs_generate(
        self,
        text: str,
        voice_id: str,
        output_name: str,
        speed: float = 1.0,
    ) -> Optional[str]:
        if not self._el_client:
            return None

        if not voice_id or voice_id in ("default", "custom"):
            voice_id = "1rqNHUqUbBGpY3OyzPMI"

        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        try:
            audio = self._el_client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id="eleven_turbo_v2_5",
            )
            save(audio, str(output_path))

            # Post-process speed if needed using FFmpeg atempo filter
            if speed != 1.0:
                temp_path = output_path.with_name(f"{output_name}_temp.mp3")
                if output_path.exists():
                    output_path.rename(temp_path)
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", str(temp_path),
                        "-filter:a", f"atempo={speed}",
                        "-vn",
                        str(output_path)
                    ]
                    import subprocess
                    subprocess.run(cmd, capture_output=True, check=True)
                    if temp_path.exists():
                        temp_path.unlink()

            print(f"[Voice] ✓ ElevenLabs TTS saved: {output_path}")
            return str(output_path)
        except Exception as e:
            print(f"[Voice] ⚠ ElevenLabs lỗi: {e}. Fallback sang mock.")
            return None

    # ── Google Cloud TTS Chirp 3 HD (giọng Việt tự nhiên, nhanh) ─────────────
    def _chirp_generate(self, text: str, voice_id: str, output_name: str,
                        speed: float = 1.0) -> Optional[str]:
        import requests, base64
        key = os.getenv("GOOGLE_TTS_KEY") or os.getenv("GEMINI_KEY")
        if not key:
            print("[Voice] Thiếu GOOGLE_TTS_KEY/GEMINI_KEY cho Chirp.", file=sys.stderr)
            return None
        # voice_id: tên ngắn (Aoede/Puck...) hoặc full "vi-VN-Chirp3-HD-Aoede"
        vname = voice_id if voice_id.startswith("vi-VN") else f"vi-VN-Chirp3-HD-{voice_id or 'Aoede'}"
        try:
            r = requests.post(
                f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}",
                json={"input": {"text": text},
                      "voice": {"languageCode": "vi-VN", "name": vname},
                      "audioConfig": {"audioEncoding": "MP3",
                                      "speakingRate": max(0.5, min(2.0, speed))}},
                timeout=120)
            if r.status_code != 200:
                print(f"[Voice] Chirp {r.status_code}: {r.text[:200]}", file=sys.stderr)
                return None
            output_path = OUTPUT_DIR / f"{output_name}.mp3"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(base64.b64decode(r.json()["audioContent"]))
            print(f"[Voice] ✓ Chirp {vname} → {output_path.name}", file=sys.stderr)
            return str(output_path.resolve())
        except Exception as e:
            print(f"[Voice] Chirp lỗi: {e}", file=sys.stderr)
            return None

    # ── Google gTTS (free, tiếng Việt) ───────────────────────────────────────
    def _gtts_generate(self, text: str, output_name: str, speed: float = 1.0) -> Optional[str]:
        try:
            from gtts import gTTS
        except ImportError:
            print("[Voice] gTTS chưa cài.", file=sys.stderr)
            return None
        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        try:
            raw = OUTPUT_DIR / f"{output_name}_raw.mp3"
            gTTS(text=text, lang="vi").save(str(raw))
            if speed and abs(speed - 1.0) > 0.02:
                import subprocess
                r = subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-filter:a", f"atempo={max(0.5,min(2.0,speed))}",
                                    "-vn", str(output_path)], capture_output=True)
                try: raw.unlink()
                except Exception: pass
                if r.returncode != 0 or not output_path.exists():
                    return None
            else:
                raw.rename(output_path)
            print(f"[Voice] ✓ gTTS (Google) saved: {output_path}", file=sys.stderr)
            return str(output_path.resolve())
        except Exception as e:
            print(f"[Voice] ⚠ gTTS lỗi: {e}", file=sys.stderr)
            return None

    # ── Microsoft Edge TTS (Free) ───────────────────────────────────────────

    def _edge_generate(
        self,
        text: str,
        voice_id: str,
        output_name: str,
        speed: float = 1.0,
    ) -> Optional[str]:
        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        try:
            import edge_tts
        except ImportError:
            print("[Voice] edge-tts package not installed! Can't generate voice.")
            return None

        # Resolve voice mapping
        edge_voice = "vi-VN-HoaiMyNeural"
        v_id_lower = voice_id.lower()
        if any(k in v_id_lower for k in ["namminh", "minhtuan", "ducanh", "male"]):
            edge_voice = "vi-VN-NamMinhNeural"
        elif any(k in v_id_lower for k in ["hoaimy", "lananh", "ngocmai", "female"]):
            edge_voice = "vi-VN-HoaiMyNeural"
        else:
            edge_voice = "vi-VN-HoaiMyNeural"

        # Rate representation in edge-tts: e.g. "+10%", "-5%"
        rate_percent = int((speed - 1.0) * 100)
        rate_str = f"{rate_percent:+d}%"

        async def run_tts():
            import time
            last_err = None
            for attempt in range(3):
                try:
                    communicate = edge_tts.Communicate(text, edge_voice, rate=rate_str, boundary="WordBoundary")
                    words = []
                    
                    with open(output_path, "wb") as fp:
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":
                                fp.write(chunk["data"])
                            elif chunk["type"] == "WordBoundary":
                                start_sec = chunk["offset"] / 10000000.0
                                dur_sec = chunk["duration"] / 10000000.0
                                words.append({
                                    "start": start_sec,
                                    "end": start_sec + dur_sec,
                                    "word": chunk["text"]
                                })
                    if len(words) > 0:
                        return words
                    raise Exception("No words timings generated.")
                except Exception as e:
                    last_err = e
                    print(f"[Voice] Edge TTS attempt {attempt + 1} failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1.0)
            raise last_err

        # Execute async task synchronously
        import asyncio
        import concurrent.futures
        import json

        # Loop checking
        try:
            loop = asyncio.get_running_loop()
            is_running = True
        except RuntimeError:
            is_running = False

        try:
            if is_running:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, run_tts())
                    words = future.result()
            else:
                words = asyncio.run(run_tts())

            # Write word timings JSON file
            words_path = output_path.with_suffix(".words.json")
            words_path.write_text(json.dumps(words, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[Voice] ✓ Edge TTS saved: {output_path} and timing details: {words_path}")
            return str(output_path.resolve())

        except Exception as e:
            print(f"[Voice] ⚠ Edge TTS error: {e}")
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            return None

    # ── OpenAI TTS ────────────────────────────────────────────────────────────

    def _openai_generate(
        self,
        text: str,
        voice_id: str,
        output_name: str,
    ) -> Optional[str]:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            print("[Voice] OPENAI_API_KEY không có — skip OpenAI, dùng mock.")
            return None

        # Standard OpenAI voices: alloy, echo, fable, onyx, nova, shimmer
        # Default to onyx/alloy for male and nova/shimmer for female
        voice = "alloy"
        v_id_lower = voice_id.lower()
        if any(v in v_id_lower for v in ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]):
            for v in ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]:
                if v in v_id_lower:
                    voice = v
                    break
        else:
            if "lananh" in v_id_lower or "female" in v_id_lower:
                voice = "nova"
            elif "ngocmai" in v_id_lower:
                voice = "shimmer"
            elif "minhtuan" in v_id_lower or "male" in v_id_lower:
                voice = "onyx"
            elif "ducanh" in v_id_lower:
                voice = "echo"
            else:
                voice = "alloy"

        output_path = OUTPUT_DIR / f"{output_name}.mp3"
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            response = client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text
            )
            response.write_to_file(str(output_path))
            print(f"[Voice] ✓ OpenAI TTS saved: {output_path} (voice={voice})")
            return str(output_path)
        except Exception as e:
            print(f"[Voice] ⚠ OpenAI TTS lỗi: {e}. Fallback.")
            return None

    # ── Mock (silent WAV) ────────────────────────────────────────────────────

    def _mock_generate(self, output_name: str, duration_sec: int = 10) -> str:
        """Generate a silent WAV file as placeholder (no external API needed)."""
        output_path = OUTPUT_DIR / f"{output_name}.wav"
        sample_rate = 24000
        n_channels = 1
        sample_width = 2  # 16-bit

        n_frames = sample_rate * duration_sec
        try:
            with wave.open(str(output_path), "wb") as wf:
                wf.setnchannels(n_channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(sample_rate)
                wf.writeframes(b"\x00" * n_frames * sample_width)
            print(f"[Voice] ✓ Mock silent audio ({duration_sec}s): {output_path}")
        except Exception as e:
            print(f"[Voice] ⚠ Không tạo được mock audio: {e}")
            # Fallback: empty file
            output_path.write_bytes(b"")

        return str(output_path)

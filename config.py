"""App config — load .env cạnh file này, không đụng .env của workspace cha."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    env_file = ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass

PORT = int(os.getenv("PORT", "7799"))

DATA_DIR = ROOT / "data"
SOURCES_DIR = DATA_DIR / "sources"
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"
SFX_DIR = ASSETS_DIR / "sfx"
MUSIC_DIR = ASSETS_DIR / "music"
FONT_DIR = ASSETS_DIR / "fonts"

DB_PATH = DATA_DIR / "app.sqlite3"

# OpenRouter
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = os.getenv("VISION_MODEL", "google/gemini-2.5-flash")
TEXT_MODEL = os.getenv("TEXT_MODEL", "openai/gpt-4o-mini")

# Voice. `wps` là số từ mỗi giây đo thật trên một câu tiếng Việt 19 từ ở speed 1.0.
# Con số này quyết định kịch bản viết bao nhiêu từ cho vừa thời lượng đặt, nên
# sai là video hụt hoặc tràn. Đo lại nếu đổi giọng.
# Mặc định Edge vì free và không cần key. Vivibe là dịch vụ trả phí, cần API_VIVIBE_KEY.
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "edge")
VOICE_ID = os.getenv("VOICE_ID", "")

VOICE_CHOICES = [
    {"id": "edge:", "label": "Edge — nữ, miễn phí, phụ đề khớp từng chữ",
     "provider": "edge", "voice": "", "wps": 2.66, "words": True},
    {"id": "vivibe:thu_review", "label": "Thu — nữ, giọng review",
     "provider": "vivibe", "voice": "thu_review", "wps": 4.38, "words": True},
    {"id": "vivibe:trinh_review", "label": "Trinh — nữ, nhanh gọn",
     "provider": "vivibe", "voice": "trinh_review", "wps": 4.75, "words": True},
    {"id": "vivibe:my_review", "label": "My — nữ, nhanh nhất",
     "provider": "vivibe", "voice": "my_review", "wps": 5.62, "words": True},
    {"id": "vivibe:adam_3", "label": "Adam — nam, chậm rãi",
     "provider": "vivibe", "voice": "adam_3", "wps": 3.05, "words": True},
]
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "edge:")


def voice_choice(voice_key: str) -> dict:
    """Trả bản ghi giọng; không khớp thì rơi về giọng mặc định."""
    key = (voice_key or "").strip() or DEFAULT_VOICE
    for c in VOICE_CHOICES:
        if c["id"] == key:
            return c
    for c in VOICE_CHOICES:
        if c["id"] == DEFAULT_VOICE:
            return c
    return VOICE_CHOICES[0]

# Render defaults
TRANSITION = os.getenv("TRANSITION", "fade")          # fade|slideleft|slideright|...
TRANSITION_DURATION = float(os.getenv("TRANSITION_DURATION", "0.4"))
BGM_VOLUME = float(os.getenv("BGM_VOLUME", "0.15"))
SFX_VOLUME = float(os.getenv("SFX_VOLUME", "0.55"))
ANALYZE_FRAMES = int(os.getenv("ANALYZE_FRAMES", "6"))

for d in (DATA_DIR, SOURCES_DIR, OUTPUT_DIR, SFX_DIR, MUSIC_DIR):
    d.mkdir(parents=True, exist_ok=True)


def openrouter_key() -> str:
    return os.getenv("OPENROUTER_KEY") or os.getenv("OPENROUTER_API_KEY") or ""


def vivibe_key() -> str:
    return os.getenv("API_VIVIBE_KEY") or os.getenv("VIVIBE_API_KEY") or ""

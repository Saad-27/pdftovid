
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LOCAL_INPUT_MODE = os.getenv("LOCAL_INPUT_MODE", "").lower() in ("1", "true", "yes")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://lecture:lecture@localhost:5432/lecture_video",
)

JOBS_DIR = Path(os.getenv("JOBS_DIR", "/tmp/lecture_video_jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)


MAX_PDF_BYTES = 25 * 1024 * 1024  
MAX_PDF_PAGES = 53

JOB_TTL_HOURS = 24

AVAILABLE_VOICES = [
    {"id": "af_bella",  "name": "Bella (US female)"},
    {"id": "af_nicole", "name": "Nicole (US female)"},
    {"id": "am_adam",   "name": "Adam (US male)"},
    {"id": "bf_emma",   "name": "Emma (UK female)"},
    {"id": "bm_george", "name": "George (UK male)"},
]
VOICE_IDS = {v["id"] for v in AVAILABLE_VOICES}

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "")
R2_INPUT_BUCKET = os.getenv("R2_INPUT_BUCKET", "")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")


ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]
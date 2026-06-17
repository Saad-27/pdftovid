
from __future__ import annotations

import logging


import fitz  
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import db
import keystore
import r2
from config import (
    ALLOWED_ORIGINS,
    AVAILABLE_VOICES,
    JOBS_DIR,
    LOCAL_INPUT_MODE,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    R2_INPUT_BUCKET,
    VOICE_IDS,
)

class _RedactApiKey(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "sk-ant-" in msg:
            record.msg = msg.split("sk-ant-")[0] + "sk-ant-[REDACTED]"
            record.args = ()
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger().addFilter(_RedactApiKey())
log = logging.getLogger("api")


app = FastAPI(title="Lecture Video API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/voices")
def voices() -> list[dict[str, str]]:
    return AVAILABLE_VOICES


@app.post("/api/jobs")
async def create_job(
    pdf_file: UploadFile = File(...),
    api_key: str = Form(...),
    voice: str = Form(...),
) -> dict[str, str]:

    if voice not in VOICE_IDS:
        raise HTTPException(400, {"code": "INVALID_VOICE", "message": "Unknown voice."})

    if not api_key or not api_key.strip():
        raise HTTPException(400, {"code": "MISSING_API_KEY", "message": "API key is required."})


    if not api_key.startswith("sk-ant-"):
        log.warning("API key is not Anthropic.")


    pdf_bytes = await pdf_file.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(400, {
            "code": "PDF_TOO_LARGE",
            "message": "PDF is too large. Please upload a file under 25 MB.",
        })

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise HTTPException(400, {
            "code": "INVALID_PDF",
            "message": "We couldn't read this file. Please upload a valid PDF.",
        })

    if doc.is_encrypted:
        raise HTTPException(400, {
            "code": "PDF_ENCRYPTED",
            "message": "This PDF is password-protected. Please remove the password and try again.",
        })

    page_count = doc.page_count
    doc.close()

    if page_count > MAX_PDF_PAGES:
        raise HTTPException(400, {
            "code": "PDF_TOO_MANY_PAGES",
            "message": f"PDF is too long. Please upload a file with {MAX_PDF_PAGES} or fewer pages.",
        })


    job_id = db.create_job(
        filename=pdf_file.filename or "upload.pdf",
        voice=voice,
        page_count=page_count,
    )


    try:
        keystore.put_key(job_id, api_key)
        if LOCAL_INPUT_MODE:
            job_dir = JOBS_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "input.pdf").write_bytes(pdf_bytes)
        else:
            r2.upload_bytes(R2_INPUT_BUCKET, f"{job_id}.pdf", pdf_bytes, "application/pdf")
    except Exception:
        log.exception("Failed to stage inputs for job %s", job_id)
        db.fail_job(job_id, "UPLOAD_FAILED", "We couldn't start your job. Please try again.")
        try:
            keystore.delete_key(job_id)  # don't leave the key behind if the PDF upload failed
        except Exception:
            pass
        raise HTTPException(503, {
            "code": "UPLOAD_FAILED",
            "message": "We couldn't start your job. Please try again.",
        })

    log.info("Created job %s (%d pages, voice=%s)", job_id, page_count, voice)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Job not found."})

    queue_position = db.queue_position(job_id)

    return {
        "job_id": str(job["id"]),
        "state": job["state"],
        "current_stage": job["current_stage"],
        "progress_percent": job["progress_percent"],
        "queue_position": queue_position,
        "filename": job["filename"],
        "voice": job["voice"],
        "page_count": job["page_count"],
        "video_url": job["video_url"],
        "video_size_bytes": job["video_size_bytes"],
        "error_code": job["error_code"],
        "error_message": job["error_message"],
        "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        "expires_at": job["expires_at"].isoformat() if job["expires_at"] else None,
    }




@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    raise HTTPException(501, {"code": "NOT_IMPLEMENTED", "message": "Cancellation coming soon."})
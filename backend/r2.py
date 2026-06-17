"""
Cloudflare R2 helpers (S3-compatible), shared by backend and worker.
boto3 >= 1.36 adds an upload checksum R2 rejects; the two Config flags
below disable it (Cloudflare's documented mitigation).
"""
from __future__ import annotations

import io
from pathlib import Path

import boto3
from botocore.config import Config as _BotoConfig

import config


class R2Error(Exception):
    pass


def _client():
    missing = [
        n for n in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        if not getattr(config, n, "")
    ]
    if missing:
        raise R2Error(f"R2 is not configured (missing: {', '.join(missing)}).")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=_BotoConfig(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def upload_bytes(bucket: str, key: str, data: bytes, content_type: str) -> None:
    _client().upload_fileobj(io.BytesIO(data), bucket, key,
                             ExtraArgs={"ContentType": content_type})


def upload_file(bucket: str, key: str, local_path: str, content_type: str) -> None:
    _client().upload_file(local_path, bucket, key,
                          ExtraArgs={"ContentType": content_type})


def download_file(bucket: str, key: str, local_path: "Path | str") -> None:
    _client().download_file(bucket, key, str(local_path))


def delete(bucket: str, key: str) -> None:
    _client().delete_object(Bucket=bucket, Key=key)
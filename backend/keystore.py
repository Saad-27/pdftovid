
from __future__ import annotations

from typing import Optional

from upstash_redis import Redis

import config

_TTL_SECONDS = 60 * 60 


def _redis() -> Redis:
    if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
        raise RuntimeError("Upstash Redis is not configured.")
    return Redis(url=config.UPSTASH_REDIS_REST_URL, token=config.UPSTASH_REDIS_REST_TOKEN)


def put_key(job_id: str, api_key: str) -> None:
    _redis().set(f"key:{job_id}", api_key, ex=_TTL_SECONDS)


def pop_key(job_id: str) -> Optional[str]:
    r = _redis()
    name = f"key:{job_id}"
    value = r.get(name)
    if value is not None:
        r.delete(name)
    return value


def delete_key(job_id: str) -> None:
    _redis().delete(f"key:{job_id}")

def touch_key(job_id: str) -> None:
    _redis().expire(f"key:{job_id}", _TTL_SECONDS)
# PixelRouter - Load Balancer Service
# Responsibility: Route jobs to least-loaded processor.
#                 Poll processor metrics before routing.
#                 Request autoscaling when all processors are overloaded.

from fastapi import FastAPI, HTTPException
import httpx
import redis
import os

from router import (
    processor_id_from_url,
    select_processor,
)

app = FastAPI(
    title="PixelRouter - Load Balancer",
    description="CPU-aware job routing across processor instances",
    version="0.1.0"
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
PROCESSOR_URLS = [
    url.strip()
    for url in os.getenv(
        "PROCESSOR_URLS",
        "http://processor-1:8002,http://processor-2:8003"
    ).split(",")
    if url.strip()
]
MAX_CPU_THRESHOLD = int(os.getenv("MAX_CPU_THRESHOLD", "80"))

r = redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/")
async def root():
    return {
        "service": "load-balancer",
        "version": "0.1.0",
        "processors_registered": len(PROCESSOR_URLS)
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "load-balancer"}


async def refresh_processor_metrics():
    """
    Ask each processor for fresh metrics before routing.
    Processor /metrics also writes those values to Redis with a short TTL.
    """
    async with httpx.AsyncClient(timeout=2.0) as client:
        for processor_url in PROCESSOR_URLS:
            try:
                response = await client.get(f"{processor_url}/metrics")
                response.raise_for_status()
            except httpx.HTTPError:
                continue


@app.get("/route")
async def get_best_processor():
    """
    Returns the URL of the least-loaded live processor.
    Routing logic: lowest pending_jobs first, CPU% as tiebreaker.
    Pending count is not incremented here; it should be updated only
    after the selected processor actually accepts/claims the job.
    """
    await refresh_processor_metrics()

    try:
        processor_url = select_processor(PROCESSOR_URLS, r)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    processor_id = processor_id_from_url(processor_url)

    return {
        "processor_url": processor_url,
        "processor_id": processor_id,
        "reason": "selected by lowest pending_jobs, then lowest CPU usage"
    }


@app.get("/processors/status")
async def processors_status():
    """
    Returns current metrics for all registered processors.
    Reads from Redis metrics keys written by each processor.
    """
    statuses = []
    for url in PROCESSOR_URLS:
        processor_id = processor_id_from_url(url)
        cpu = r.get(f"metrics:{processor_id}:cpu") or "unknown"
        pending = r.get(f"metrics:{processor_id}:pending") or "0"
        statuses.append({
            "processor_id": processor_id,
            "url": url,
            "cpu_percent": cpu,
            "pending_jobs": pending
        })
    return {
        "processors": statuses,
        "max_cpu_threshold": MAX_CPU_THRESHOLD
    }

"""
Standalone Oxygen 11 article scheduler worker.
Run with:
    python -m app.workers.article_publisher
"""
import asyncio
from app.services.article_publisher import run_scheduler_loop

if __name__ == "__main__":
    asyncio.run(run_scheduler_loop())

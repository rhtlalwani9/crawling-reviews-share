"""Entry point: python -m crawling_reviews.api"""
import uvicorn

from ..config import config
from .app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=config.port, log_config=None)

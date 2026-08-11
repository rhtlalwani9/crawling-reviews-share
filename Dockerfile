# syntax=docker/dockerfile:1
#
# Application image. Depends on the base built from base.Dockerfile:
#
#   docker build --platform=linux/amd64 -f base.Dockerfile -t review-crawler-base:1 .
#   docker build --platform=linux/amd64 -t review-crawler:1 .
#
# Or just: docker compose up --build
ARG BASE_IMAGE=review-crawler-base:1
FROM ${BASE_IMAGE}

USER root
WORKDIR /app

# Dependencies first, so editing application code does not invalidate the pip layer.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# No packaging step: the app runs from source. PYTHONPATH=src (set below) is the whole mechanism.
COPY src/ ./src/
COPY tests/ ./tests/
COPY pytest.ini ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Profiles are runtime state, never baked in. The directory is created empty and owned by the app
# user so a bind-mounted volume can be chowned into place; see docker-compose.yml.
RUN mkdir -p /app/src/crawling_reviews/profiles/_data \
    && chown -R app:app /app

USER app

ENV PORT=8080 \
    LOG_LEVEL=INFO \
    BROWSER_HEADLESS=false \
    PROFILE_POOL_SIZE=2 \
    PYTHONPATH=/app/src

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

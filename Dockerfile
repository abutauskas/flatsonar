# Flatsonar server: the JSON API, the website and the crawler in one image.
#
#   docker build -t flatsonar .
#   docker run -p 8000:8000 -v flatsonar-data:/data -e DATABASE_URL=sqlite:////data/flatsonar.db flatsonar
#   docker run --rm -v flatsonar-data:/data -e DATABASE_URL=sqlite:////data/flatsonar.db flatsonar \
#       python -m flatsonar_server.crawler.run --source all
#
# Cloud Run / any PaaS: the server listens on $PORT (default 8000).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY core /app/core
COPY server /app/server
RUN pip install -e /app/core -e "/app/server[postgres]"

ENV PORT=8000 DATABASE_URL=sqlite:////data/flatsonar.db CRAWL_CACHE_DIR=/data/crawl-cache
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "uvicorn flatsonar_server.main:app --host 0.0.0.0 --port ${PORT}"]

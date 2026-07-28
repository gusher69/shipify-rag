# Shipify AI Platform — container image for the Admin Web UI / LINE webhook.
# Database is Supabase-hosted (Postgres + pgvector) — this image only runs
# the Python application; it does not bundle a database.
FROM python:3.13-slim

WORKDIR /app

# System deps needed by pdfplumber/pymupdf/pandas/markitdown at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# .env is provided at runtime via --env-file / docker-compose env_file,
# never baked into the image.
EXPOSE 8001

# Default command runs the Admin Web UI. Override the command to run
# line_bot.webhook:app instead (see docker-compose.yml).
CMD ["uvicorn", "admin.routes:app", "--host", "0.0.0.0", "--port", "8001"]

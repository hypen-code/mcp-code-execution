FROM python:3.13-slim

# Install build tools needed for some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[llm]"

COPY src/ src/
COPY config/ config/

# Create data and compiled directories
RUN mkdir -p /app/data /app/compiled

ENV MFP_COMPILED_OUTPUT_DIR=/app/compiled
ENV MFP_CACHE_DB_PATH=/app/data/cache.db

EXPOSE 8000

CMD ["mfp", "serve", "--transport", "http"]

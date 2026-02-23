FROM python:3.13-slim

# Install only minimal required packages
RUN pip install --no-cache-dir \
    httpx==0.28.* \
    pydantic==2.* \
    orjson==3.*

# Create non-root user for security
RUN useradd -r -u 1000 -s /bin/false executor

# Set workspace
WORKDIR /workspace

# Copy entrypoint
COPY entrypoint.py /workspace/entrypoint.py

# Lock down permissions
RUN chmod 444 /workspace/entrypoint.py

USER executor

ENTRYPOINT ["python", "/workspace/entrypoint.py"]

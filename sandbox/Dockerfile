FROM python:3.13-slim

# Install sandbox dependencies from requirements file
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

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

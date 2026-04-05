FROM python:3.13-slim

# Install all sandbox Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Pre-install cmdstan for Prophet (downloads prebuilt Stan binaries ~100 MB)
RUN python -c "import cmdstanpy; cmdstanpy.install_cmdstan()" \
    && python -c "from prophet import Prophet"

# Create non-root user for security
RUN useradd -r -u 1000 -s /bin/false executor

# Set workspace
WORKDIR /workspace

# Copy entrypoint
COPY entrypoint.py /workspace/entrypoint.py

# Lock down permissions
RUN chmod 444 /workspace/entrypoint.py

USER executor

ENTRYPOINT ["python", "-X", "jit", "/workspace/entrypoint.py"]

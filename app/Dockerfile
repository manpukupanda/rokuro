# ==============================================================
# Dockerfile for FastAPI container
#
# Base image: python:3.11-slim
#   - Uses a lightweight Debian-based image
#
# Installed components:
#   - pip packages as listed in requirements.txt
#
# Startup command:
#   - `entrypoint.sh` starts uvicorn
# ==============================================================

FROM python:3.11-slim

# Disable stdout buffering to stream build logs immediately
ENV PYTHONUNBUFFERED=1

# Update system packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Set working directory for the application
WORKDIR /app

# Copy and install dependencies first
# (Install before copying source to leverage Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Copy entrypoint script and make it executable
# Convert CRLF to LF using dos2unix (helps when repo was cloned on Windows)
COPY entrypoint.sh /entrypoint.sh
RUN dos2unix /entrypoint.sh && chmod +x /entrypoint.sh

# `entrypoint.sh` will run migrations (if any) and then start uvicorn
CMD ["/entrypoint.sh"]

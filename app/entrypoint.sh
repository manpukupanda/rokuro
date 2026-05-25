#!/bin/sh
# entrypoint.sh - Entrypoint script for container startup

echo "Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port 6960

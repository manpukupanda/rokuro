import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("ROKURO_DB_PATH", "/var/lib/rokuro/rokuro.db")
GORO_API_BASE_URL = os.getenv("GORO_API_BASE_URL", "http://goro:5600")
GORO_PLAYBACK_BASE_URL = os.getenv("GORO_PLAYBACK_BASE_URL", "http://localhost:5600")
GORO_API_KEY = os.getenv("GORO_API_KEY", "")
SESSION_SECRET = os.getenv("ROKURO_SESSION_SECRET", "")
INITIAL_ADMIN_ACCOUNT = "admin"
INITIAL_ADMIN_PASSWORD = os.getenv("ROKURO_INITIAL_ADMIN_PASSWORD", "")

if not GORO_API_KEY:
    raise RuntimeError("GORO_API_KEY must be set")
if not SESSION_SECRET:
    raise RuntimeError("ROKURO_SESSION_SECRET must be set")
if not INITIAL_ADMIN_PASSWORD:
    raise RuntimeError("ROKURO_INITIAL_ADMIN_PASSWORD must be set")

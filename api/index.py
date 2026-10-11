"""Vercel entry point for the live web demo (the app itself is in web/demo_app.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.demo_app import app  # noqa: E402,F401

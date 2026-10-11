"""Run the stand-in ERP:  python -m erp --port 8100"""
import argparse
import os

import uvicorn

from gateway.envfile import load_env_file

from .app import create_app

load_env_file()

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8100)
parser.add_argument("--db", default=os.environ.get("ERP_DB", "erp.db"))
args = parser.parse_args()
uvicorn.run(
    create_app(args.db, os.environ.get("ERP_READ_KEY", "change-me-erp-read-key"),
               os.environ.get("ERP_WRITE_KEY", "change-me-erp-write-key")),
    host="127.0.0.1", port=args.port,
)

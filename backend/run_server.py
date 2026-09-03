from __future__ import annotations

import argparse

import uvicorn

from app.config import config
from app.main import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JobPostings server")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    args = parser.parse_args()
    config.host = args.host
    config.port = args.port
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()

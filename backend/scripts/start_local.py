from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upgrade the configured database, then start the local MapGo API."
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--reload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, uvicorn_args = _parser().parse_known_args(argv)
    migration_command = [sys.executable, "-m", "alembic", "upgrade", "head"]

    print("[MapGo] Upgrading the configured database to the latest Alembic revision...")
    migration = subprocess.run(migration_command, cwd=ROOT, check=False)
    if migration.returncode != 0:
        print(
            "[MapGo] Database migration failed; the API was not started.",
            file=sys.stderr,
        )
        return migration.returncode

    server_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app.main:app",
        "--app-dir",
        str(ROOT),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.reload:
        server_command.append("--reload")
    server_command.extend(uvicorn_args)

    print(f"[MapGo] Database is current. Starting API on http://{args.host}:{args.port}")
    try:
        return subprocess.call(server_command, cwd=ROOT)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

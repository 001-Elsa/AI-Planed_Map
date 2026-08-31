from pathlib import Path
from unittest.mock import Mock

from backend.scripts import start_local


def test_start_local_migrates_before_starting_uvicorn(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    start = Mock(return_value=0)
    monkeypatch.setattr(start_local.subprocess, "run", run)
    monkeypatch.setattr(start_local.subprocess, "call", start)

    exit_code = start_local.main(["--host", "127.0.0.1", "--port", "3100", "--reload"])

    assert exit_code == 0
    run.assert_called_once_with(
        [start_local.sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=start_local.ROOT,
        check=False,
    )
    start.assert_called_once_with(
        [
            start_local.sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--app-dir",
            str(start_local.ROOT),
            "--host",
            "127.0.0.1",
            "--port",
            "3100",
            "--reload",
        ],
        cwd=start_local.ROOT,
    )


def test_start_local_does_not_start_api_when_migration_fails(monkeypatch):
    run = Mock(return_value=Mock(returncode=7))
    start = Mock()
    monkeypatch.setattr(start_local.subprocess, "run", run)
    monkeypatch.setattr(start_local.subprocess, "call", start)

    assert start_local.main([]) == 7
    start.assert_not_called()


def test_start_local_forwards_additional_uvicorn_arguments(monkeypatch):
    monkeypatch.setattr(
        start_local.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0)),
    )
    start = Mock(return_value=0)
    monkeypatch.setattr(start_local.subprocess, "call", start)

    assert start_local.main(["--log-level", "debug"]) == 0

    command = start.call_args.args[0]
    assert command[-2:] == ["--log-level", "debug"]
    assert start.call_args.kwargs["cwd"] == Path(start_local.ROOT)

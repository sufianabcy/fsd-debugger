"""Shared test setup."""

import tempfile

import pytest


@pytest.fixture(autouse=True)
def private_temp_folder(tmp_path, monkeypatch):
    """Send every session file, in this process and in fsd subprocesses, to the test's own folder."""
    temp = tmp_path / "os-temp"
    temp.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp

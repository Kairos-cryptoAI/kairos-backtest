"""Operational retries cannot suppress immutable-data failures."""

from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from zipfile import BadZipFile, ZipFile

import pytest

from kairos_backtest.aggtrades import AggTradeIntegrityError
from scripts.recover_quarter_hour import next_group, prepare_archive


def test_failed_preparation_never_invokes_collector(tmp_path, monkeypatch):
    from scripts import recover_quarter_hour as recovery

    commands = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(recovery, "next_group", lambda _: (68, (("BNBUSDT", "2022-02"),)))

    def fail(*args, **kwargs):
        raise AggTradeIntegrityError("mismatch")

    monkeypatch.setattr(recovery, "prepare_archive", fail)
    with pytest.raises(AggTradeIntegrityError):
        recovery.main(["--ledger", str(tmp_path / "ledger"), "--cache-dir", str(tmp_path)])
    assert len(commands) == 1
    assert commands[0][-1] == "--verify"


def test_collector_must_advance_exact_prepared_prefix(tmp_path, monkeypatch):
    from scripts import recover_quarter_hour as recovery

    commands = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(recovery, "next_group", lambda _: (68, (("BNBUSDT", "2022-02"),)))
    monkeypatch.setattr(recovery, "prepare_archive", lambda *args: None)
    with pytest.raises(RuntimeError, match="prefix"):
        recovery.main(["--ledger", str(tmp_path / "ledger"), "--cache-dir", str(tmp_path)])
    assert commands[-1][-4:] == ["--workers", "1", "--max-new-batches", "1"]


@pytest.mark.parametrize("error", [TimeoutError(), URLError("read failed")])
def test_retry_transport_is_bounded(error):
    calls, waits = [], []

    def load(*args):
        calls.append(args)
        raise error

    with pytest.raises(type(error)):
        prepare_archive(SimpleNamespace(load=load), "BTCUSDT", "2022-02", sleep=waits.append)
    assert len(calls) == 3
    assert waits == [10, 20]


@pytest.mark.parametrize(
    "error",
    [
        AggTradeIntegrityError("SHA mismatch"),
        BadZipFile("CRC"),
        HTTPError("https://example.invalid", 404, "missing", None, None),
    ],
)
def test_integrity_and_missing_archive_are_not_retried(error):
    calls = []

    def load(*args):
        calls.append(args)
        raise error

    with pytest.raises(type(error)):
        prepare_archive(SimpleNamespace(load=load), "BTCUSDT", "2022-02", sleep=lambda _: pytest.fail())
    assert len(calls) == 1


def test_success_after_timeout_checks_crc(tmp_path):
    path = tmp_path / "archive.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("archive.csv", "fixture")
    calls = []

    def load(*args):
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError()
        return SimpleNamespace(path=path, archive_sha256="fixture")

    prepare_archive(SimpleNamespace(load=load), "BTCUSDT", "2022-02", sleep=lambda _: None)
    assert len(calls) == 2


def test_missing_ledger_is_not_created(tmp_path):
    import sqlite3

    path = tmp_path / "absent.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        next_group(path)
    assert not path.exists()


def test_readonly_prefix_selection(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    monkeypatch.setattr(
        "scripts.recover_quarter_hour.expected_sequence",
        lambda: (("BTCUSDT", "2022-01"), ("ETHUSDT", "2022-01"), ("BTCUSDT", "2022-02")),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE archive_batch(sequence INTEGER, symbol TEXT, period TEXT)")
        connection.execute("INSERT INTO archive_batch VALUES(0, 'BTCUSDT', '2022-01')")
    before = Path(path).read_bytes()
    assert next_group(path) == (1, (("ETHUSDT", "2022-01"),))
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE archive_batch SET sequence=7")
    with pytest.raises(ValueError, match="prefix"):
        next_group(path)

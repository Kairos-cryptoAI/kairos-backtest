from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from kairos_backtest.factor_data import (
    ARCHIVE_HOST,
    ArchiveTarget,
    FactorKind,
    _checksum,
    download_target,
    expected_targets,
    parse_funding,
    parse_leverage,
    parse_premium,
)


def _zip(filename: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, text)
    return buffer.getvalue()


def _sidecar(payload: bytes, filename: str) -> bytes:
    return f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode()


def test_expected_factor_inventory_is_fixed_and_safe():
    targets = expected_targets()

    assert len(targets) == 4_415
    assert len({target.relative_path for target in targets}) == len(targets)
    assert sum(target.kind is FactorKind.FUNDING for target in targets) == 305
    assert sum(target.kind is FactorKind.PREMIUM for target in targets) == 305
    assert sum(target.kind is FactorKind.LEVERAGE for target in targets) == 3_805
    assert all(target.url.startswith(ARCHIVE_HOST) for target in targets)

    with pytest.raises(ValueError, match="invalid fixed"):
        ArchiveTarget(
            FactorKind.FUNDING,
            "BTCUSDT",
            "escape.zip",
            f"{ARCHIVE_HOST}/data/futures/um/monthly/fundingRate/BTCUSDT/escape.zip",
            Path("..") / "escape.zip",
        )


def test_official_checksum_binds_payload_and_filename():
    payload = b"factor-data"
    assert (
        _checksum(payload, _sidecar(payload, "sample.zip"), "sample.zip")
        == hashlib.sha256(payload).hexdigest()
    )

    with pytest.raises(ValueError, match="mismatch"):
        _checksum(b"changed", _sidecar(payload, "sample.zip"), "sample.zip")
    with pytest.raises(ValueError, match="malformed"):
        _checksum(payload, _sidecar(payload, "other.zip"), "sample.zip")


def test_factor_parsers_accept_signed_premium_and_reject_schema_drift():
    funding_name = "BTCUSDT-fundingRate-2024-01.zip"
    funding = _zip(
        funding_name.removesuffix(".zip") + ".csv",
        "calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,-0.000125\n",
    )
    parsed_funding = parse_funding(funding, "BTCUSDT", funding_name)
    assert parsed_funding[0].rate == -0.000125
    assert parsed_funding[0].interval_hours == 8

    premium_name = "BTCUSDT-1h-2024-01.zip"
    premium = _zip(
        premium_name.removesuffix(".zip") + ".csv",
        ("1704067200000,-0.0010,-0.0005,-0.0015,-0.0008,0,1704070799999,0,720,0,0,0\n"),
    )
    parsed_premium = parse_premium(premium, "BTCUSDT", premium_name)
    assert parsed_premium[0].close == -0.0008

    leverage_name = "BTCUSDT-metrics-2024-07-01.zip"
    leverage = _zip(
        leverage_name.removesuffix(".zip") + ".csv",
        (
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2024-07-01 00:00:00,BTCUSDT,100,200,1.2,1.3,1.1,0.9\n"
        ),
    )
    parsed_leverage = parse_leverage(leverage, "BTCUSDT", leverage_name)
    assert parsed_leverage[0].open_interest_value == 200
    assert parsed_leverage[0].taker_long_short_volume_ratio == 0.9

    bad = _zip("bad.csv", "changed,schema\n1,2\n")
    with pytest.raises(ValueError, match="schema"):
        parse_funding(bad, "BTCUSDT", "bad.zip")


def test_download_is_atomic_and_revalidates_cached_bytes(tmp_path, monkeypatch):
    filename = "BTCUSDT-fundingRate-2024-01.zip"
    payload = _zip(
        filename.removesuffix(".zip") + ".csv",
        "calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.0001\n",
    )
    checksum = _sidecar(payload, filename)
    target = ArchiveTarget(
        FactorKind.FUNDING,
        "BTCUSDT",
        filename,
        f"{ARCHIVE_HOST}/data/futures/um/monthly/fundingRate/BTCUSDT/{filename}",
        Path("fundingRate") / "BTCUSDT" / filename,
    )

    def fake_fetch(url: str, *, retries: int) -> bytes:
        assert retries == 2
        return checksum if url.endswith(".CHECKSUM") else payload

    monkeypatch.setattr("kairos_backtest.factor_data._fetch", fake_fetch)
    first = download_target(tmp_path, target, retries=2)
    second = download_target(tmp_path, target, retries=2)

    assert first.cache_status == "downloaded"
    assert second.cache_status == "verified_cache"
    assert not list(tmp_path.rglob("*.tmp"))

    (tmp_path / target.relative_path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="mismatch"):
        download_target(tmp_path, target, retries=2)

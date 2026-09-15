"""Parsing tests for the GMP client. No Home Assistant needed: plain pytest.

The fixtures are real response shapes: the hourly rows are from a live account,
the net-metered row is from the greenmountainpower PyPI project's issue #2, and
the credits shape is what the GMP portal's net-metering widget reads.
"""

import importlib.util
import pathlib
import sys
from datetime import date, datetime

import pytest

_API = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "gmp" / "api.py"
_spec = importlib.util.spec_from_file_location("gmp_api", _API)
api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = api
_spec.loader.exec_module(api)

TZ = api.TIMEZONE


def test_hourly_rows_are_labelled_by_end_of_hour_in_eastern_time():
    payload = {
        "intervals": [
            {
                "values": [
                    {
                        "date": "2026-09-07T01:00:00Z",
                        "totalEnergyUsed": 0.63,
                        "consumedTotal": 0.63,
                        "consumed": 0.63,
                        "returnedGeneration": 0.0,
                    },
                    {
                        "date": "2026-09-07T02:00:00Z",
                        "totalEnergyUsed": 0.56,
                        "consumedTotal": 0.56,
                        "consumed": 0.56,
                        "returnedGeneration": 0.0,
                    },
                ]
            }
        ]
    }
    reads = api.parse_usage(payload, api.HOURLY)
    assert [r.start for r in reads] == [
        datetime(2026, 9, 7, 0, 0, tzinfo=TZ),
        datetime(2026, 9, 7, 1, 0, tzinfo=TZ),
    ]
    assert reads[0].consumed == 0.63
    assert reads[0].returned == 0.0
    assert reads[0].generation is None
    # Eastern daylight time in September, so the UTC instant is four hours on.
    assert reads[0].start.utcoffset().total_seconds() == -4 * 3600


def test_daily_rows_are_labelled_by_start_of_day():
    payload = {"intervals": [{"values": [{"date": "2026-09-07T00:00:00Z", "consumed": 12.5}]}]}
    reads = api.parse_usage(payload, api.DAILY)
    assert reads[0].start == datetime(2026, 9, 7, 0, 0, tzinfo=TZ)
    assert reads[0].consumed == 12.5


def test_net_metered_row_keeps_import_export_and_generation_separate():
    payload = {
        "intervals": [
            {
                "values": [
                    {
                        "date": "2022-12-10T15:00:00Z",
                        "consumedTotal": 0,
                        "consumed": 0,
                        "generation": 3.15,
                        "returnedGeneration": 1.87,
                        "temperature": 22.55,
                    }
                ]
            }
        ]
    }
    (read,) = api.parse_usage(payload, api.HOURLY)
    assert read.start == datetime(2022, 12, 10, 14, 0, tzinfo=TZ)
    assert (read.consumed, read.returned, read.generation) == (0.0, 1.87, 3.15)


def test_rows_without_dates_are_skipped_and_output_is_sorted():
    payload = {
        "intervals": [
            {
                "values": [
                    {"date": "2026-09-07T03:00:00Z", "consumed": 3},
                    {"consumed": 99},
                    {"date": "2026-09-07T01:00:00Z", "consumed": 1},
                ]
            }
        ]
    }
    reads = api.parse_usage(payload, api.HOURLY)
    assert [r.consumed for r in reads] == [1.0, 3.0]


def test_merge_prefers_hourly_from_its_first_day_onward():
    daily = [
        api.UsageRead(datetime(2026, 9, d, tzinfo=TZ), float(d), 0.0, None) for d in range(1, 8)
    ]
    hourly = [api.UsageRead(datetime(2026, 9, 6, h, tzinfo=TZ), 0.1, 0.0, None) for h in range(24)]
    merged = api.merge_reads(daily, hourly)
    assert [r.start.day for r in merged[:5]] == [1, 2, 3, 4, 5]
    assert merged[5:] == hourly
    assert api.merge_reads(daily, []) == daily


def test_server_date_is_local_midnight_with_offset():
    assert api.server_date(date(2026, 9, 7)) == "2026-09-07T00:00:00-04:00"
    assert api.server_date(date(2026, 1, 15)) == "2026-01-15T00:00:00-05:00"


def test_credits_parse_and_sort_by_expiration_and_404_becomes_empty():
    payload = {
        "accountNumber": "123",
        "expiringCredits": [
            {
                "creditDate": "2026-07-01T00:00:00Z",
                "expirationDate": "2027-07-01T00:00:00Z",
                "amount": -12.5,
            },
            {
                "creditDate": "2026-06-01T00:00:00Z",
                "expirationDate": "2027-06-01T00:00:00Z",
                "amount": -3.0,
            },
            {"creditDate": "bad"},
        ],
    }
    credits = api.parse_credits(payload)
    assert [c.amount for c in credits] == [-3.0, -12.5]
    assert credits[0].expiration_date == date(2027, 6, 1)
    assert api.parse_credits([]) == []


def test_bills_land_on_the_period_they_cover():
    periods = [
        {"startDate": "2026-07-05T00:00:00Z", "endDate": "2026-08-04T00:00:00Z"},
        {"startDate": "2026-08-05T00:00:00Z", "endDate": "2026-09-03T00:00:00Z"},
    ]
    transactions = [
        {"type": "Pay Segment", "date": "2026-08-20T00:00:00Z", "payoffAmount": -140.0},
        {"type": "Bill Segment", "date": "2026-09-05T00:00:00Z", "payoffAmount": 151.2},
        {"type": "Bill Segment", "date": "2026-08-06T00:00:00Z", "payoffAmount": 140.0},
        # Older than the periods feed reaches: falls back to the previous
        # bill's usage end plus a day, and the very first to its own usage end.
        {"type": "Bill Segment", "date": "2026-07-07T00:00:00Z", "payoffAmount": 130.0},
        {"type": "Bill Segment", "date": "2026-06-06T00:00:00Z", "payoffAmount": 120.0},
    ]
    bills = api.parse_bills(transactions, periods)
    assert [(b.period_start.date(), b.amount) for b in bills] == [
        (date(2026, 6, 4), 120.0),
        (date(2026, 6, 5), 130.0),
        (date(2026, 7, 5), 140.0),
        (date(2026, 8, 5), 151.2),
    ]
    assert bills[-1].bill_date == date(2026, 9, 5)
    assert all(b.period_start.tzinfo is TZ for b in bills)


def test_two_bill_segments_in_one_period_are_summed():
    periods = [{"startDate": "2026-08-05T00:00:00Z", "endDate": "2026-09-03T00:00:00Z"}]
    transactions = [
        {"type": "Bill Segment", "date": "2026-09-05T00:00:00Z", "payoffAmount": 100.0},
        {"type": "Bill Segment", "date": "2026-09-05T00:00:00Z", "payoffAmount": -20.0},
    ]
    (bill,) = api.parse_bills(transactions, periods)
    assert bill.amount == 80.0


def test_bills_survive_missing_periods_feed():
    transactions = [{"type": "Bill Segment", "date": "2026-09-05T00:00:00Z", "payoffAmount": 10.0}]
    (bill,) = api.parse_bills(transactions, None)
    assert bill.period_start.date() == date(2026, 9, 3)
    assert api.parse_bills([], []) == []


def test_accounts_merge_login_list_with_details():
    user = {
        "customData": {
            "energyAccounts": [
                {"accountNumber": 12345, "isPrimary": True},
                {"accountNumber": "67890"},
                {"nickname": "no number"},
            ]
        }
    }
    listed = [
        {"accountNumber": "12345", "nickname": "Home", "solarNetMeter": True},
        {"accountNumber": "67890", "address": {"line1": "1 Main St"}},
    ]
    accounts = api.parse_accounts(user, listed)
    assert [(a.number, a.nickname, a.address, a.net_metered) for a in accounts] == [
        ("12345", "Home", None, True),
        ("67890", None, "1 Main St", False),
    ]
    assert accounts[0].label == "12345 (Home)"
    assert api.parse_accounts({}, []) == []


@pytest.mark.parametrize("raw", ["2026-09-07T01:00:00Z", "2026-09-07T01:00:00"])
def test_parse_local_treats_z_as_eastern(raw):
    assert api.parse_local(raw) == datetime(2026, 9, 7, 1, 0, tzinfo=TZ)

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
    merged = api.merge_reads(daily, api.DAILY, hourly)
    assert [r.start.day for r in merged[:5]] == [1, 2, 3, 4, 5]
    assert merged[5:] == hourly
    assert api.merge_reads(daily, api.DAILY, []) == daily


def test_merge_drops_a_month_that_overlaps_the_first_daily_read():
    monthly = [
        api.UsageRead(datetime(2026, m, 1, tzinfo=TZ), 100.0, 0.0, None) for m in (6, 7, 8, 9)
    ]
    daily = [api.UsageRead(datetime(2026, 8, 20, tzinfo=TZ), 5.0, 0.0, None)]
    merged = api.merge_reads(monthly, api.MONTHLY, daily)
    assert [r.start.month for r in merged] == [6, 7, 8]
    assert merged[-1] is daily[0]


def test_interval_end_uses_wall_clock_periods():
    assert api.interval_end(datetime(2026, 9, 7, 23, tzinfo=TZ), api.HOURLY) == datetime(
        2026, 9, 8, 0, tzinfo=TZ
    )
    # The day DST ends is 25 hours long; it still ends at the next midnight.
    assert api.interval_end(datetime(2026, 11, 1, tzinfo=TZ), api.DAILY) == datetime(
        2026, 11, 2, tzinfo=TZ
    )
    assert api.interval_end(datetime(2026, 12, 1, tzinfo=TZ), api.MONTHLY) == datetime(
        2027, 1, 1, tzinfo=TZ
    )
    assert api.interval_end(datetime(2026, 2, 1, tzinfo=TZ), api.MONTHLY) == datetime(
        2026, 3, 1, tzinfo=TZ
    )
    with pytest.raises(ValueError):
        api.interval_end(datetime(2026, 1, 1, tzinfo=TZ), "weekly")


def test_monthly_rows_are_labelled_by_start_of_month():
    payload = {"intervals": [{"values": [{"date": "2025-06-01T00:00:00Z", "consumed": 812.0}]}]}
    (read,) = api.parse_usage(payload, api.MONTHLY)
    assert read.start == datetime(2025, 6, 1, tzinfo=TZ)


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


def test_periods_parse_sorted_and_skip_junk():
    spans = api.parse_periods(
        [
            {"startDate": "2026-08-05T00:00:00Z", "endDate": "2026-09-03T00:00:00Z"},
            {"startDate": "2026-07-05T00:00:00Z", "endDate": "2026-08-04T00:00:00Z"},
            {"startDate": "nope"},
        ]
    )
    assert spans == [
        (date(2026, 7, 5), date(2026, 8, 4)),
        (date(2026, 8, 5), date(2026, 9, 3)),
    ]
    assert api.parse_periods(None) == []


def test_current_period_is_the_billed_one_when_today_falls_inside():
    spans = [(date(2026, 7, 24), date(2026, 8, 23))]
    assert api.current_period(spans, date(2026, 8, 10)) == (date(2026, 7, 24), date(2026, 8, 23))


def test_current_period_is_projected_forward_from_the_last_billed_one():
    spans = [(date(2026, 6, 24), date(2026, 7, 23)), (date(2026, 7, 24), date(2026, 8, 23))]
    # One cycle on: same 31-day length, starting the day after the last end.
    assert api.current_period(spans, date(2026, 9, 14)) == (date(2026, 8, 24), date(2026, 9, 23))
    # Several cycles on, if the integration was away for a while.
    assert api.current_period(spans, date(2026, 11, 1)) == (date(2026, 10, 25), date(2026, 11, 24))


def test_current_period_falls_back_to_the_calendar_month():
    assert api.current_period([], date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert api.current_period([], date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_total_energy_used_is_read_as_on_site_use():
    payload = {
        "intervals": [
            {
                "values": [
                    {
                        "date": "2026-09-12T15:00:00Z",
                        "consumed": 0.42,
                        "generation": 3.15,
                        "returnedGeneration": 1.87,
                        "totalEnergyUsed": 1.70,
                    }
                ]
            }
        ]
    }
    (read,) = api.parse_usage(payload, api.HOURLY)
    assert read.used == 1.70
    # GMP's own identity, which its figure satisfies to the cent.
    assert round(read.consumed + read.generation - read.returned, 2) == read.used


def test_a_row_without_total_energy_used_has_none():
    payload = {"intervals": [{"values": [{"date": "2026-08-25T00:00:00Z", "consumed": 12.0}]}]}
    (read,) = api.parse_usage(payload, api.DAILY)
    assert read.used is None


def test_trailing_generation_without_export_is_held_back():
    posted = [
        api.UsageRead(datetime(2026, 9, 16, h, tzinfo=TZ), 0.1, 0.5, 2.0, 1.6) for h in range(9, 11)
    ]
    provisional = [
        api.UsageRead(datetime(2026, 9, 16, h, tzinfo=TZ), 0.1, 0.0, 5.0, 5.1)
        for h in range(11, 14)
    ]
    reads = posted + provisional
    kept = api.trim_provisional(reads)
    assert [r.start.hour for r in kept] == [9, 10]


def test_a_fully_self_consumed_hour_mid_series_is_kept():
    reads = [
        api.UsageRead(datetime(2026, 9, 16, 9, tzinfo=TZ), 0.1, 0.5, 2.0, 1.6),
        # Everything the array made went into the house: real, not provisional.
        api.UsageRead(datetime(2026, 9, 16, 10, tzinfo=TZ), 0.1, 0.0, 2.0, 2.1),
        api.UsageRead(datetime(2026, 9, 16, 11, tzinfo=TZ), 0.1, 0.9, 3.0, 2.2),
    ]
    assert api.trim_provisional(reads) == reads


def test_trim_leaves_a_series_with_no_generation_alone():
    reads = [api.UsageRead(datetime(2026, 8, d, tzinfo=TZ), 12.0, 0.0, None) for d in range(1, 5)]
    assert api.trim_provisional(reads) == reads


class TestBackfillStart:
    """The fetch cursor across series that fill at different times."""

    def test_all_empty_asks_for_full_history(self):
        assert api.backfill_start(None, None, None, None) is None

    def test_settled_account_uses_oldest_cursor(self):
        assert api.backfill_start(300.0, 200.0, 250.0, 275.0) == 200.0

    def test_account_without_generation_meter_ignores_absent_series(self):
        # Generation and site never fill here, and must not force a refetch.
        assert api.backfill_start(300.0, 200.0, None, None) == 200.0

    def test_series_added_by_upgrade_backfills_from_the_start(self):
        # The regression: site consumption arrived after the others were
        # already current, and inherited their cursor, so every historical
        # row was dropped as "already stored".
        assert api.backfill_start(300.0, 300.0, 300.0, None) is None

    def test_missing_generation_alongside_stored_site_is_a_real_gap(self):
        assert api.backfill_start(300.0, 300.0, None, 300.0) is None


# Two consecutive bills, verbatim from the portal's bill-detail report. The
# rate schedule is in every column header, as GMP sends it.
_R = "(Rate: E01 Residential)"
LINE_ITEMS = [
    {
        "Bill Date": "2026-07-24",
        "Bill Quantity": 1212.0,
        "Bill Amount": 311.19,
        f"Current Energy/Major Storm Adjustor{_R}_amt": 22.23,
        f"Current Energy/Major Storm Adjustor{_R}_qty": 0.0,
        f"Customer Charge{_R}_amt": 19.41,
        f"Customer Charge{_R}_qty": 31.0,
        f"Electric Assistance Program Fee{_R}_amt": 1.5,
        f"Electric Assistance Program Fee{_R}_qty": 0.0,
        f"Energy Efficiency Charge{_R}_amt": 12.68,
        f"Energy Efficiency Charge{_R}_qty": 0.0,
        f"Extreme Storm Restoration Fund{_R}_amt": 1.96,
        f"Extreme Storm Restoration Fund{_R}_qty": 0.0,
        f"KWH{_R}_amt": 253.41,
        f"KWH{_R}_qty": 1181.0,
    },
    {
        "Bill Date": "2026-08-25",
        "Bill Quantity": 2024.0,
        "Bill Amount": 510.03,
        f"Current Energy/Major Storm Adjustor{_R}_amt": 36.47,
        f"Current Energy/Major Storm Adjustor{_R}_qty": 0.0,
        f"Customer Charge{_R}_amt": 20.03,
        f"Customer Charge{_R}_qty": 32.0,
        f"Electric Assistance Program Fee{_R}_amt": 1.5,
        f"Electric Assistance Program Fee{_R}_qty": 0.0,
        f"Energy Efficiency Charge{_R}_amt": 21.39,
        f"Energy Efficiency Charge{_R}_qty": 0.0,
        f"Extreme Storm Restoration Fund{_R}_amt": 3.22,
        f"Extreme Storm Restoration Fund{_R}_qty": 0.0,
        f"KWH{_R}_amt": 427.42,
        f"KWH{_R}_qty": 1992.0,
    },
]


class TestParseRates:
    """Rates come from the bill; nothing else in the API states a price."""

    def test_reads_the_newest_bill(self):
        rates = api.parse_rates(LINE_ITEMS)
        assert rates.bill_date == date(2026, 8, 25)

    def test_energy_rate_includes_the_riders(self):
        # The headline KWH line alone is 427.42/1992 = 0.2146. The three
        # usage-scaled riders put the real marginal cost 14% above that.
        rates = api.parse_rates(LINE_ITEMS)
        assert rates.energy == pytest.approx(0.245231, abs=1e-6)

    def test_customer_charge_is_per_day_not_per_month(self):
        rates = api.parse_rates(LINE_ITEMS)
        assert rates.customer == pytest.approx(0.625938, abs=1e-6)

    def test_flat_fee_is_the_line_that_did_not_move(self):
        # The assistance-program fee is 1.50 on both bills while usage jumped
        # 1181 -> 1992 kWh, so it is per-bill, not per-kWh.
        assert api.parse_rates(LINE_ITEMS).fixed == pytest.approx(1.5)

    def test_the_parts_reconstruct_the_bill(self):
        rates = api.parse_rates(LINE_ITEMS)
        total = rates.energy * 1992 + rates.customer * 32 + rates.fixed
        assert total == pytest.approx(510.03, abs=0.005)

    def test_single_bill_treats_riders_as_usage_based(self):
        rates = api.parse_rates(LINE_ITEMS[-1:])
        assert rates.fixed == 0.0
        assert rates.energy == pytest.approx((510.03 - 20.03) / 1992, abs=1e-6)

    def test_no_bills_is_not_an_error(self):
        assert api.parse_rates([]) is None
        assert api.parse_rates(None) is None

    def test_row_without_usage_is_not_rateable(self):
        assert api.parse_rates([{"Bill Date": "2026-08-25", "Bill Amount": 20.0}]) is None

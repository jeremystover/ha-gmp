"""Fetch GMP data and write it into the recorder as statistics.

GMP publishes meter reads hours to a day after the fact, so live sensors would
be wrong by construction. Like Home Assistant's Opower integration, usage and
cost are written as external long-term statistics that the Energy dashboard
reads directly, and only slow-moving figures (credit balance, account balance,
last bill) are exposed as ordinary sensors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    DAILY,
    HOURLY,
    MONTHLY,
    TIMEZONE,
    Bill,
    Credit,
    GmpAuthError,
    GmpClient,
    GmpConnectionError,
    GmpError,
    Rates,
    UsageRead,
    backfill_start,
    current_period,
    history_truncated,
    site_use,
    interval_end,
    merge_reads,
    trim_provisional,
)
from .const import (
    BILL_BACKFILL_DAYS,
    BILL_REFETCH_DAYS,
    RATES_LOOKBACK_DAYS,
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY_ID,
    CONF_API_KEY_SECRET,
    DAILY_BACKFILL_DAYS,
    DAILY_CHUNK_DAYS,
    DOMAIN,
    HOURLY_BACKFILL_DAYS,
    HOURLY_CHUNK_DAYS,
    MONTHLY_BACKFILL_DAYS,
    MONTHLY_CHUNK_DAYS,
    UPDATE_INTERVAL_HOURS,
    USAGE_REFETCH_DAYS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeriodSummary:
    """The billing period in progress, tallied from the stored statistics."""

    start: date
    end: date
    days_total: int
    # Days for which GMP has published reads, counted from ``start``.
    days_with_data: int
    import_kwh: float
    export_kwh: float
    generation_kwh: float | None
    # Everything the property consumed, grid and solar together. GMP reports
    # it per interval; None on accounts with no generation meter.
    used_kwh: float | None

    @property
    def net_kwh(self) -> float:
        """Export minus import: positive means the month is banking credit."""
        return self.export_kwh - self.import_kwh

    def projected(self, value: float) -> float:
        """Scale a to-date figure to the whole period by the daily average."""
        if self.days_with_data <= 0:
            return 0.0
        return value / self.days_with_data * self.days_total


@dataclass
class GmpData:
    """What the sensors show between statistics runs."""

    credits: list[Credit]
    status: dict[str, Any]
    last_read: datetime | None
    last_bill: Bill | None
    has_generation: bool
    period: PeriodSummary
    rates: Rates | None


def statistic_id(account: str, kind: str) -> str:
    """``gmp:<account>_<kind>`` -- what to pick in the Energy dashboard.

    The recorder allows only lowercase letters, digits and underscores in a
    statistic id. GMP account numbers are digits, but don't assume.
    """
    slug = re.sub(r"[^a-z0-9_]", "_", account.lower())
    return f"{DOMAIN}:{slug}_{kind}"


class GmpCoordinator(DataUpdateCoordinator[GmpData]):
    """Twice a day: log in, extend the statistics, refresh the sensors."""

    config_entry: GmpConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GmpConfigEntry) -> None:
        self.account_number: str = entry.data[CONF_ACCOUNT_NUMBER]
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"GMP {self.account_number}",
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self.client = GmpClient(
            async_get_clientsession(hass),
            entry.data[CONF_API_KEY_ID],
            entry.data[CONF_API_KEY_SECRET],
        )

    # --- statistic ids ------------------------------------------------------

    def statistic_id(self, kind: str) -> str:
        """``gmp:<account>_<kind>`` -- what to pick in the Energy dashboard."""
        return statistic_id(self.account_number, kind)

    def _metadata(self, kind: str, label: str, *, energy: bool) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"GMP {self.account_number} {label}",
            source=DOMAIN,
            statistic_id=self.statistic_id(kind),
            unit_class=EnergyConverter.UNIT_CLASS if energy else None,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR if energy else None,
        )

    # --- update -------------------------------------------------------------

    async def _async_update_data(self) -> GmpData:
        try:
            last_read, has_generation = await self._async_insert_usage()
            periods = await self.client.async_get_billing_periods(self.account_number)
            last_bill = await self._async_insert_cost(periods)
            credits = await self.client.async_get_credits(self.account_number)
            status = await self.client.async_get_status(self.account_number)
            rates = await self._async_rates(last_bill)
            # The period sensors read back what the writes above queued, and
            # the recorder runs them on its own thread. Without waiting, a
            # refresh that rebuilds the series reports the state it replaced --
            # or nothing at all, on the refresh right after a migration clears
            # it -- until the next poll a day later.
            await get_instance(self.hass).async_block_till_done()
            period = await self._async_period_summary(periods, last_read, has_generation)
        except GmpAuthError as err:
            raise ConfigEntryAuthFailed from err
        except GmpConnectionError as err:
            raise UpdateFailed(f"Cannot reach GMP: {err}") from err
        except GmpError as err:
            raise UpdateFailed(str(err)) from err
        return GmpData(
            credits=credits,
            status=status,
            last_read=last_read,
            last_bill=last_bill,
            has_generation=has_generation,
            period=period,
            rates=rates,
        )

    async def _async_rates(self, last_bill: Bill | None) -> Rates | None:
        """Prices from the newest bill, re-read only when a new bill lands.

        A rate changes when GMP issues a bill and not otherwise, so polling for
        one twice a day would spend a request an hour to watch a number that
        moves monthly at most. The bill date we already have says when to look.
        """
        known = self.data.rates if self.data else None
        if last_bill is None:
            return known
        if known is not None and known.bill_date >= last_bill.bill_date:
            return known
        today = dt_util.now(TIMEZONE).date()
        return (
            await self.client.async_get_rates(
                self.account_number, today - timedelta(days=RATES_LOOKBACK_DAYS), today
            )
            or known
        )

    # --- statistics plumbing ------------------------------------------------

    async def _async_last(self, kind: str) -> tuple[float, float | None]:
        """Running sum and start timestamp of the newest stored row."""
        stat_id = self.statistic_id(kind)
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        rows = last.get(stat_id) if last else None
        if not rows:
            return 0.0, None
        return float(rows[0].get("sum") or 0.0), float(rows[0]["start"])

    @staticmethod
    def _rows(
        points: list[tuple[datetime, float]], base_sum: float, last_start: float | None
    ) -> list[StatisticData]:
        """Turn (start, value) points into rows continuing the stored sum.

        Anything at or before the newest stored row is dropped: a row already
        in the recorder is never rewritten.
        """
        total = base_sum
        rows: list[StatisticData] = []
        for start, value in points:
            if last_start is not None and start.timestamp() <= last_start:
                continue
            total += value
            rows.append(StatisticData(start=dt_util.as_utc(start), state=value, sum=total))
        return rows

    def _add(self, kind: str, label: str, rows: list[StatisticData], *, energy: bool) -> None:
        if not rows:
            return
        _LOGGER.debug("Adding %d rows to %s", len(rows), self.statistic_id(kind))
        async_add_external_statistics(self.hass, self._metadata(kind, label, energy=energy), rows)

    # --- usage --------------------------------------------------------------

    async def _async_insert_usage(self) -> tuple[datetime | None, bool]:
        base_consumption, last_consumption = await self._async_last("energy_consumption")
        base_return, last_return = await self._async_last("energy_return")
        base_generation, last_generation = await self._async_last("energy_generation")
        base_used, last_used = await self._async_last("energy_site")

        # A version of this integration that shared one cursor across series
        # started site consumption at whatever the others had already reached,
        # leaving it permanently short of its own history. Clearing it is the
        # only way back: the rebuild below then refills it from the start.
        if history_truncated(base_used, last_used, base_consumption, last_consumption):
            _LOGGER.warning(
                "Rebuilding %s: %.0f kWh stored against %.0f kWh of consumption",
                self.statistic_id("energy_site"),
                base_used,
                base_consumption,
            )
            get_instance(self.hass).async_clear_statistics([self.statistic_id("energy_site")])
            base_used, last_used = 0.0, None

        reads = await self._async_fetch_reads(
            backfill_start(last_consumption, last_return, last_generation, last_used)
        )
        if not reads:
            _LOGGER.debug("No usage reads returned")
            last = dt_util.utc_from_timestamp(last_consumption) if last_consumption else None
            return last, base_generation > 0

        self._add(
            "energy_consumption",
            "consumption",
            self._rows([(r.start, r.consumed) for r in reads], base_consumption, last_consumption),
            energy=True,
        )
        self._add(
            "energy_return",
            "return",
            self._rows([(r.start, r.returned) for r in reads], base_return, last_return),
            energy=True,
        )
        # Only accounts with a generation meter get these fields at all.
        has_generation = any(r.generation is not None for r in reads) or base_generation > 0
        if has_generation:
            self._add(
                "energy_generation",
                "generation",
                self._rows(
                    [(r.start, r.generation or 0.0) for r in reads],
                    base_generation,
                    last_generation,
                ),
                energy=True,
            )
            # GMP's own total for what the property used: grid import plus the
            # share of production the house consumed instead of exporting.
            self._add(
                "energy_site",
                "site consumption",
                self._rows(
                    [(r.start, site_use(r)) for r in reads],
                    base_used,
                    last_used,
                ),
                energy=True,
            )
        return reads[-1].start, has_generation

    async def _async_fetch_reads(self, last_start: float | None) -> list[UsageRead]:
        """Monthly reads for the long tail, daily for the last year, hourly recently."""
        today = dt_util.now(TIMEZONE).date()
        tomorrow = today + timedelta(days=1)
        hourly_floor = today - timedelta(days=HOURLY_BACKFILL_DAYS)
        daily_floor = today - timedelta(days=DAILY_BACKFILL_DAYS)
        if last_start is None:
            since = today - timedelta(days=MONTHLY_BACKFILL_DAYS)
        else:
            since = datetime.fromtimestamp(last_start, TIMEZONE).date() - timedelta(
                days=USAGE_REFETCH_DAYS
            )
        hourly_from = max(since, hourly_floor)
        daily_from = max(since, daily_floor)

        monthly: list[UsageRead] = []
        if since < daily_from:
            monthly = await self._async_fetch_chunked(
                MONTHLY, since.replace(day=1), daily_from, MONTHLY_CHUNK_DAYS
            )
        daily: list[UsageRead] = []
        if daily_from < hourly_from:
            daily = await self._async_fetch_chunked(
                DAILY, daily_from, hourly_from, DAILY_CHUNK_DAYS
            )
        hourly = await self._async_fetch_chunked(HOURLY, hourly_from, tomorrow, HOURLY_CHUNK_DAYS)
        return trim_provisional(merge_reads(monthly, MONTHLY, merge_reads(daily, DAILY, hourly)))

    async def _async_fetch_chunked(
        self, interval: str, start: date, end: date, chunk_days: int
    ) -> list[UsageRead]:
        """Fetch ``[start, end)`` in windows GMP is known to accept.

        The interval still in progress is left out: GMP revises it as the
        hour, day or month completes, and a stored row is never rewritten.
        """
        now = dt_util.now(TIMEZONE)
        reads: list[UsageRead] = []
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=chunk_days), end)
            chunk = await self.client.async_get_usage(
                self.account_number, interval, cursor, window_end
            )
            _LOGGER.debug("%s usage %s to %s: %d reads", interval, cursor, window_end, len(chunk))
            reads.extend(r for r in chunk if interval_end(r.start, interval) <= now)
            cursor = window_end
        reads.sort(key=lambda r: r.start)
        return reads

    # --- cost ---------------------------------------------------------------

    async def _async_insert_cost(self, periods: list[tuple[date, date]]) -> Bill | None:
        """Bills as a cost statistic at the start of the period each covers.

        GMP does not price individual hours, so this is billing-period
        resolution: the Energy dashboard's totals are right by the month, and a
        whole bill lands on one day within it. A negative bill (credits
        exceeding charges) goes to the compensation statistic instead.
        """
        base_cost, last_start = await self._async_last("energy_cost")
        base_compensation, _ = await self._async_last("energy_compensation")

        today = dt_util.now(TIMEZONE).date()
        if last_start is None:
            start = today - timedelta(days=BILL_BACKFILL_DAYS)
        else:
            start = datetime.fromtimestamp(last_start, TIMEZONE).date() - timedelta(
                days=BILL_REFETCH_DAYS
            )
        bills = await self.client.async_get_bills(
            self.account_number, start, today + timedelta(days=1), periods
        )
        if not bills:
            return None
        self._add(
            "energy_cost",
            "cost",
            self._rows(
                [(b.period_start, max(0.0, b.amount)) for b in bills], base_cost, last_start
            ),
            energy=False,
        )
        self._add(
            "energy_compensation",
            "compensation",
            self._rows(
                [(b.period_start, max(0.0, -b.amount)) for b in bills],
                base_compensation,
                last_start,
            ),
            energy=False,
        )
        return bills[-1]

    # --- billing period in progress -------------------------------------------

    async def _async_period_summary(
        self,
        periods: list[tuple[date, date]],
        last_read: datetime | None,
        has_generation: bool,
    ) -> PeriodSummary:
        """Tally the stored statistics from the start of the current period."""
        today = dt_util.now(TIMEZONE).date()
        start, end = current_period(periods, today)
        ids = {self.statistic_id("energy_consumption"), self.statistic_id("energy_return")}
        if has_generation:
            ids.add(self.statistic_id("energy_generation"))
            ids.add(self.statistic_id("energy_site"))
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(datetime.combine(start, datetime.min.time(), tzinfo=TIMEZONE)),
            None,
            ids,
            "day",
            None,
            {"change"},
        )

        def total(kind: str) -> float:
            rows = stats.get(self.statistic_id(kind), []) if stats else []
            return sum(float(row.get("change") or 0.0) for row in rows)

        last_day = last_read.astimezone(TIMEZONE).date() if last_read else None
        days_with_data = 0
        if last_day is not None and last_day >= start:
            days_with_data = (min(last_day, end) - start).days + 1
        return PeriodSummary(
            start=start,
            end=end,
            days_total=(end - start).days + 1,
            days_with_data=days_with_data,
            import_kwh=total("energy_consumption"),
            export_kwh=total("energy_return"),
            generation_kwh=total("energy_generation") if has_generation else None,
            used_kwh=total("energy_site") if has_generation else None,
        )


GmpConfigEntry = ConfigEntry[GmpCoordinator]

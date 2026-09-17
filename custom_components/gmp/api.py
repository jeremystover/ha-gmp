"""Async client for Green Mountain Power's customer API.

The endpoints were read off GMP's own web portal -- the Vue bundle behind
greenmountainpower.com/account -- but requests are signed with an API key GMP
issues on request, not with the portal's password grant. Nothing in this module
imports Home Assistant, so the parsing is testable with plain pytest.

Two things the API does that are not obvious from its responses:

* Every usage timestamp ends in "Z" but is Eastern wall-clock time, not UTC.
  The portal's own FixUsageDate() strips the Z and treats the value as local;
  so does parse_local() below.
* Hourly rows are labelled with the END of the hour ("01:00" is midnight to
  one). Daily and monthly rows are labelled with the start of the period.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.greenmountainpower.com/api/v2"
HEADERS = {"GMP-Source": "web", "Accept": "application/json"}

# GMP serves Vermont only.
TIMEZONE = ZoneInfo("America/New_York")

HOURLY = "hourly"
DAILY = "daily"
MONTHLY = "monthly"

BILL_TYPE = "Bill Segment"
# The line whose quantity is days, and the one whose quantity is kilowatt-hours.
CUSTOMER_CHARGE_LINE = "Customer Charge"
ENERGY_LINE = "KWH"
# A bill is dated about two days after the usage period it covers ends. The
# portal uses the same offset when it links a bill to its usage.
BILL_DATE_LAG = timedelta(days=2)


class GmpError(Exception):
    """Something went wrong talking to GMP."""


class GmpAuthError(GmpError):
    """GMP rejected the credentials or the token."""


class GmpConnectionError(GmpError):
    """GMP could not be reached."""


@dataclass(frozen=True)
class Account:
    """One GMP service account visible to the login."""

    number: str
    nickname: str | None
    address: str | None
    net_metered: bool

    @property
    def label(self) -> str:
        """Something a person can pick out of a list."""
        detail = self.nickname or self.address
        return f"{self.number} ({detail})" if detail else self.number


@dataclass(frozen=True)
class UsageRead:
    """Energy through the meter over one interval starting at ``start``."""

    start: datetime
    consumed: float
    returned: float
    generation: float | None
    # On-site use: consumed + generation - returned, as GMP computes it.
    # Absent on accounts with no generation meter.
    used: float | None = None


@dataclass(frozen=True)
class Credit:
    """One banked net-metering credit."""

    credit_date: date
    expiration_date: date
    amount: float


@dataclass(frozen=True)
class Bill:
    """One bill, attributed to the start of the period it covers."""

    bill_date: date
    period_start: datetime
    amount: float


@dataclass(frozen=True)
class Rates:
    """What the newest bill actually charged, split by what each part scales with.

    GMP bills one energy line plus several riders that all scale with usage, a
    customer charge billed per day rather than per month, and a flat per-bill
    fee. ``energy`` folds the riders in, so it is the whole marginal cost of a
    kilowatt-hour -- meaningfully higher than the headline energy line alone.
    """

    bill_date: date
    energy: float
    customer: float
    fixed: float


# --- parsing ---------------------------------------------------------------


def parse_local(raw: str) -> datetime:
    """Parse a GMP timestamp: "Z"-suffixed, but really Eastern wall-clock."""
    return datetime.fromisoformat(raw[:19]).replace(tzinfo=TIMEZONE)


def parse_date(raw: str) -> date:
    """Parse the date part of a GMP timestamp."""
    return date.fromisoformat(raw[:10])


def server_date(day: date) -> str:
    """Format a date the way the portal sends it: local midnight with offset."""
    return datetime.combine(day, time(), tzinfo=TIMEZONE).isoformat()


def local_midnight(day: date) -> datetime:
    """Midnight at the start of ``day`` in GMP's timezone."""
    return datetime.combine(day, time(), tzinfo=TIMEZONE)


def interval_end(start: datetime, interval: str) -> datetime:
    """When the interval that began at ``start`` is over.

    Wall-clock arithmetic on purpose: GMP's intervals are local hours, days
    and calendar months, so the end of a day is the next midnight even when
    a DST change makes that 23 or 25 hours away.
    """
    naive = start.replace(tzinfo=None)
    if interval == HOURLY:
        end = naive + timedelta(hours=1)
    elif interval == DAILY:
        end = naive + timedelta(days=1)
    elif interval == MONTHLY:
        end = (naive.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(f"Unknown interval {interval!r}")
    return end.replace(tzinfo=start.tzinfo)


def parse_usage(payload: dict[str, Any], interval: str) -> list[UsageRead]:
    """Turn a usage response into reads sorted by start time."""
    reads: list[UsageRead] = []
    for block in payload.get("intervals") or []:
        for value in block.get("values") or []:
            if not isinstance(value, dict) or "date" not in value:
                continue
            try:
                labelled = parse_local(value["date"])
            except ValueError:
                _LOGGER.debug("Skipping usage row with bad date: %s", value)
                continue
            start = labelled
            if interval == HOURLY:
                # Wall-clock arithmetic on purpose: the label is a local hour.
                naive = labelled.replace(tzinfo=None) - timedelta(hours=1)
                start = naive.replace(tzinfo=TIMEZONE)
            consumed = value.get("consumed")
            if consumed is None:
                consumed = value.get("consumedTotal")
            generation = value.get("generation")
            used = value.get("totalEnergyUsed")
            reads.append(
                UsageRead(
                    start=start,
                    consumed=float(consumed or 0.0),
                    returned=float(value.get("returnedGeneration") or 0.0),
                    generation=float(generation) if generation is not None else None,
                    used=float(used) if used is not None else None,
                )
            )
    reads.sort(key=lambda read: read.start)
    return reads


def merge_reads(
    coarse: list[UsageRead], coarse_interval: str, fine: list[UsageRead]
) -> list[UsageRead]:
    """Prefer finer reads wherever they exist.

    A coarse read (a month, a day) is kept only if it ends at or before the
    first fine read begins, so the two never overlap in the statistics.
    """
    if not fine:
        return list(coarse)
    first = fine[0].start
    return [r for r in coarse if interval_end(r.start, coarse_interval) <= first] + list(fine)


def trim_provisional(reads: list[UsageRead]) -> list[UsageRead]:
    """Drop trailing reads GMP has only half posted.

    The export channel posts later than the generation channel, so the newest
    intervals can show generation against a returned of zero -- which makes
    ``totalEnergyUsed`` far too high for those hours. Dropping them off the end
    costs nothing: a stored row is never rewritten, but a row never stored is
    picked up by the next run's refetch window once GMP has finished with it.

    Only the trailing run is dropped. The same shape earlier in the series is a
    real hour in which the house used everything the array made.
    """
    cut = len(reads)
    while cut > 0:
        read = reads[cut - 1]
        if (read.generation or 0.0) > 0.0 and read.returned == 0.0:
            cut -= 1
            continue
        break
    if cut != len(reads):
        _LOGGER.debug("Holding back %d partly posted reads", len(reads) - cut)
    return reads[:cut]


def parse_credits(payload: Any) -> list[Credit]:
    """Turn the generation/credits response into credits.

    GMP answers 404 rather than an empty list when there are no credits; the
    client turns that into ``[]`` before it gets here.
    """
    if not isinstance(payload, dict):
        return []
    credits: list[Credit] = []
    for item in payload.get("expiringCredits") or []:
        try:
            credits.append(
                Credit(
                    credit_date=parse_date(item["creditDate"]),
                    expiration_date=parse_date(item["expirationDate"]),
                    amount=float(item["amount"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("Skipping malformed credit: %s", item)
    credits.sort(key=lambda credit: credit.expiration_date)
    return credits


def parse_bills(transactions: Any, periods: Any) -> list[Bill]:
    """Attribute each bill to the start of the billing period it covers.

    The transactions feed says when a bill was issued and for how much; the
    billing-periods feed says which dates each period spans. A bill is matched
    to the period containing its usage end (bill date minus two days). When no
    period matches -- the periods feed only goes back so far -- the period is
    taken to start the day after the previous bill's usage ended, and the very
    first bill with nothing before it is pinned to its own usage end.
    """
    spans = parse_periods(periods)

    dated: list[tuple[date, float]] = []
    for txn in transactions or []:
        if not isinstance(txn, dict) or txn.get("type") != BILL_TYPE:
            continue
        try:
            dated.append((parse_date(txn["date"]), float(txn["payoffAmount"])))
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("Skipping malformed transaction: %s", txn)
    dated.sort()

    by_start: dict[datetime, tuple[date, float]] = {}
    previous_usage_end: date | None = None
    for bill_date, amount in dated:
        usage_end = bill_date - BILL_DATE_LAG
        # Strictly after the start: a period that begins on this bill's usage
        # end is the next one, sharing the meter-read day.
        start_day = next((s for s, e in spans if s < usage_end <= e), None)
        if start_day is None:
            start_day = (
                previous_usage_end + timedelta(days=1)
                if previous_usage_end is not None
                else usage_end
            )
        previous_usage_end = usage_end
        start = local_midnight(start_day)
        if start in by_start:
            # Two bill segments in one period: a correction. Sum them.
            by_start[start] = (bill_date, by_start[start][1] + amount)
        else:
            by_start[start] = (bill_date, amount)

    return [
        Bill(bill_date=bill_date, period_start=start, amount=amount)
        for start, (bill_date, amount) in sorted(by_start.items())
    ]


def parse_periods(periods: Any) -> list[tuple[date, date]]:
    """Billing periods as (start, end) date pairs, oldest first."""
    spans: list[tuple[date, date]] = []
    for period in periods or []:
        try:
            spans.append((parse_date(period["startDate"]), parse_date(period["endDate"])))
        except (KeyError, TypeError, ValueError):
            continue
    spans.sort()
    return spans


def current_period(spans: list[tuple[date, date]], today: date) -> tuple[date, date]:
    """The billing period containing ``today``.

    GMP only publishes a period once it has been billed, so the period in
    progress is usually not in the list. It is projected forward from the
    newest one, same length, until it covers today. With no periods at all,
    fall back to the calendar month.
    """
    for start, end in spans:
        if start <= today <= end:
            return start, end
    if spans:
        start, end = spans[-1]
        length = (end - start).days + 1
        while end < today:
            start, end = end + timedelta(days=1), end + timedelta(days=length)
        return start, end
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def parse_accounts(user: Any, listed: Any) -> list[Account]:
    """Combine the login's account list with the richer per-account details."""
    raw = ((user or {}).get("customData") or {}).get("energyAccounts") or []
    details = {
        str(item.get("accountNumber")): item
        for item in (listed or [])
        if isinstance(item, dict) and item.get("accountNumber")
    }
    accounts: list[Account] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("accountNumber"):
            continue
        number = str(item["accountNumber"])
        merged = {**item, **details.get(number, {})}
        address = merged.get("serviceAddress") or merged.get("address")
        if isinstance(address, dict):
            address = address.get("line1") or address.get("street") or None
        accounts.append(
            Account(
                number=number,
                nickname=merged.get("nickname") or None,
                address=str(address) if address else None,
                net_metered=bool(merged.get("solarNetMeter")),
            )
        )
    return accounts


# --- client ----------------------------------------------------------------


async def _message(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return resp.reason or str(resp.status)
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error_description") or body)
    return str(body)


class GmpClient:
    """Thin async wrapper over the handful of endpoints this integration uses."""

    def __init__(self, session: aiohttp.ClientSession, key_id: str, key_secret: str) -> None:
        self._session = session
        self._auth = aiohttp.BasicAuth(key_id, key_secret)

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        allow_404: bool = False,
    ) -> Any:
        try:
            async with self._session.get(
                f"{BASE_URL}{path}", params=params, headers=HEADERS, auth=self._auth
            ) as resp:
                if resp.status in (401, 403):
                    raise GmpAuthError(await _message(resp))
                if resp.status == 404 and allow_404:
                    # GMP answers 404 rather than [] when there are no records.
                    return []
                if resp.status >= 400:
                    raise GmpError(f"GET {path} failed: HTTP {resp.status} {await _message(resp)}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise GmpConnectionError(str(err)) from err

    async def _post(
        self,
        path: str,
        params: dict[str, str],
        body: dict[str, Any],
        *,
        allow_404: bool = False,
    ) -> Any:
        try:
            async with self._session.post(
                f"{BASE_URL}{path}",
                params=params,
                json=body,
                headers=HEADERS,
                auth=self._auth,
            ) as resp:
                if resp.status in (401, 403):
                    raise GmpAuthError(await _message(resp))
                if resp.status == 404 and allow_404:
                    return []
                if resp.status >= 400:
                    raise GmpError(f"POST {path} failed: HTTP {resp.status} {await _message(resp)}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise GmpConnectionError(str(err)) from err

    async def async_get_accounts(self) -> list[Account]:
        """List the service accounts the login can see."""
        user = await self._get("/users/current")
        numbers = [
            str(item["accountNumber"])
            for item in (((user or {}).get("customData") or {}).get("energyAccounts") or [])
            if isinstance(item, dict) and item.get("accountNumber")
        ]
        listed: Any = []
        if numbers:
            try:
                listed = await self._get("/accounts/list", params={"accounts": ",".join(numbers)})
            except GmpError as err:
                _LOGGER.debug("Account details unavailable: %s", err)
        return parse_accounts(user, listed)

    async def async_get_usage(
        self, account: str, interval: str, start: date, end: date
    ) -> list[UsageRead]:
        """Usage reads from ``start`` up to but excluding ``end``."""
        params = {
            "startDate": server_date(start),
            "endDate": server_date(end),
            "temp": "f",
        }
        payload = await self._get(f"/usage/{account}/{interval}", params, allow_404=True)
        if not isinstance(payload, dict):
            return []
        return parse_usage(payload, interval)

    async def async_get_credits(self, account: str) -> list[Credit]:
        """Banked net-metering credits, soonest to expire first."""
        payload = await self._get(f"/accounts/{account}/generation/credits", allow_404=True)
        return parse_credits(payload)

    async def async_get_status(self, account: str) -> dict[str, Any]:
        """Balance and standing of the account."""
        payload = await self._get(f"/accounts/{account}/status")
        return payload if isinstance(payload, dict) else {}

    async def async_get_billing_periods(self, account: str) -> list[tuple[date, date]]:
        """Billed periods, oldest first. Empty if GMP has none to offer."""
        try:
            payload = await self._get(f"/accounts/{account}/billing/periods", allow_404=True)
        except GmpError as err:
            _LOGGER.debug("Billing periods unavailable: %s", err)
            return []
        return parse_periods(payload.get("periods") if isinstance(payload, dict) else payload)

    async def async_get_rates(self, account: str, start: date, end: date) -> Rates | None:
        """What the newest bill in the window charged per kWh, per day and per bill.

        The portal's own bill-detail report, which is the only place GMP states
        a rate: the usage and billing endpoints give kilowatt-hours and totals
        but never a price.
        """
        try:
            payload = await self._post(
                f"/accounts/{account}/bills/line-items",
                {"transpose": "true"},
                {
                    "includeColumns": ["quantity", "amount"],
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "includeRates": [],
                },
                allow_404=True,
            )
        except GmpError as err:
            _LOGGER.debug("Bill line items unavailable: %s", err)
            return None
        return parse_rates(payload)

    async def async_get_bills(
        self, account: str, start: date, end: date, periods: list[tuple[date, date]]
    ) -> list[Bill]:
        """Bills issued from ``start`` to ``end``, attributed to their periods."""
        transactions = await self._get(
            f"/accounts/{account}/transactions",
            {"startDate": server_date(start), "endDate": server_date(end)},
            allow_404=True,
        )
        return parse_bills(
            transactions,
            [{"startDate": s.isoformat(), "endDate": e.isoformat()} for s, e in periods],
        )


def _line_amounts(row: dict[str, Any]) -> dict[str, float]:
    """Charge amounts by line name, dropping the rate schedule in the header."""
    amounts: dict[str, float] = {}
    for key, value in row.items():
        if not key.endswith("_amt"):
            continue
        try:
            amounts[key[:-4].split("(", 1)[0].strip()] = float(value or 0.0)
        except (TypeError, ValueError):
            continue
    return amounts


def _line_quantity(row: dict[str, Any], line: str) -> float:
    for key, value in row.items():
        if key.endswith("_qty") and key[:-4].split("(", 1)[0].strip() == line:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def parse_rates(rows: Any) -> Rates | None:
    """Rates from the newest bill's line items, oldest row first.

    A rider carries no quantity of its own, so what it scales with has to be
    read off two bills: an amount that repeats unchanged while usage moves is a
    flat fee, and anything else rides on the kilowatt-hours. With only one bill
    to look at, riders are treated as usage-based -- the common case, and wrong
    by cents rather than by the shape of the bill.
    """
    usable = [r for r in rows or [] if isinstance(r, dict) and r.get("Bill Date")]
    if not usable:
        return None
    newest = usable[-1]
    prior = usable[-2] if len(usable) > 1 else None

    amounts = _line_amounts(newest)
    kwh = _line_quantity(newest, ENERGY_LINE)
    days = _line_quantity(newest, CUSTOMER_CHARGE_LINE)
    customer_amt = amounts.pop(CUSTOMER_CHARGE_LINE, 0.0)
    # The bill total is a bare column, not one of the "<line>_amt" pairs.
    try:
        total = float(newest.get("Bill Amount") or 0.0)
    except (TypeError, ValueError):
        return None
    if kwh <= 0 or days <= 0 or total <= 0:
        _LOGGER.debug("Line items for %s are not rateable", newest.get("Bill Date"))
        return None

    fixed = 0.0
    if prior is not None:
        before = _line_amounts(prior)
        fixed = sum(
            amount
            for line, amount in amounts.items()
            if line != ENERGY_LINE and amount == before.get(line)
        )

    try:
        bill_date = parse_date(str(newest["Bill Date"]))
    except (TypeError, ValueError):
        return None
    return Rates(
        bill_date=bill_date,
        energy=(total - customer_amt - fixed) / kwh,
        customer=customer_amt / days,
        fixed=fixed,
    )


def backfill_start(
    consumption: float | None,
    returned: float | None,
    generation: float | None,
    used: float | None,
) -> float | None:
    """Oldest point any maintained series still needs, as a fetch cursor.

    Each series carries its own cursor, so a series introduced by an upgrade
    has none and must be filled from the beginning -- returning ``None`` asks
    for the full cascade rather than the refetch window.

    Generation and site consumption exist only on accounts with a generation
    meter. Both empty means no such meter rather than a gap; one of the two
    holding rows makes the other's emptiness a real gap.
    """
    starts = [consumption, returned]
    if generation is not None or used is not None:
        starts += [generation, used]
    if any(start is None for start in starts):
        return None
    return min(starts)

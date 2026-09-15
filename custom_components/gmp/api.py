"""Async client for Green Mountain Power's undocumented customer API.

Everything here was read off GMP's own web portal -- the Vue bundle behind
greenmountainpower.com/account -- which calls these same endpoints with the same
public client id. Nothing in this module imports Home Assistant, so the parsing
is testable with plain pytest.

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
TOKEN_URL = f"{BASE_URL}/applications/token?remember_me=false"
# The portal's public OAuth client id, shipped in its JavaScript bundle.
CLIENT_ID = "C95D19408B024BD4BEB42FA66F08BCEA"
HEADERS = {"GMP-Source": "web"}

# GMP serves Vermont only.
TIMEZONE = ZoneInfo("America/New_York")

HOURLY = "hourly"
DAILY = "daily"
MONTHLY = "monthly"
INTERVAL_SPAN = {HOURLY: timedelta(hours=1), DAILY: timedelta(days=1)}

BILL_TYPE = "Bill Segment"
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
            reads.append(
                UsageRead(
                    start=start,
                    consumed=float(consumed or 0.0),
                    returned=float(value.get("returnedGeneration") or 0.0),
                    generation=float(generation) if generation is not None else None,
                )
            )
    reads.sort(key=lambda read: read.start)
    return reads


def merge_reads(coarse: list[UsageRead], fine: list[UsageRead]) -> list[UsageRead]:
    """Prefer finer reads wherever they exist.

    Coarse (daily) reads are kept only for days before the first fine (hourly)
    read, so the two never overlap in the statistics.
    """
    if not fine:
        return list(coarse)
    cutoff = fine[0].start.replace(hour=0, minute=0, second=0, microsecond=0)
    return [read for read in coarse if read.start < cutoff] + list(fine)


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
    spans: list[tuple[date, date]] = []
    for period in periods or []:
        try:
            spans.append((parse_date(period["startDate"]), parse_date(period["endDate"])))
        except (KeyError, TypeError, ValueError):
            continue

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

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None

    async def async_login(self) -> None:
        """Exchange the credentials for a bearer token.

        The same password grant the portal's login form uses.
        """
        data = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
            "client_id": CLIENT_ID,
        }
        try:
            async with self._session.post(TOKEN_URL, data=data, headers=HEADERS) as resp:
                if resp.status in (400, 401, 403):
                    raise GmpAuthError(await _message(resp))
                if resp.status >= 400:
                    raise GmpConnectionError(f"Login failed: HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise GmpConnectionError(str(err)) from err
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise GmpAuthError("No access token in login response")
        self._token = token

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        allow_404: bool = False,
        _retry: bool = True,
    ) -> Any:
        if self._token is None:
            await self.async_login()
        headers = {**HEADERS, "Authorization": f"Bearer {self._token}"}
        try:
            async with self._session.get(
                f"{BASE_URL}{path}", params=params, headers=headers
            ) as resp:
                if resp.status == 401:
                    if _retry:
                        # Tokens expire; one fresh login is cheap.
                        self._token = None
                        return await self._get(path, params, allow_404=allow_404, _retry=False)
                    raise GmpAuthError(await _message(resp))
                if resp.status == 404 and allow_404:
                    # GMP answers 404 rather than [] when there are no records.
                    return []
                if resp.status >= 400:
                    raise GmpError(f"GET {path} failed: HTTP {resp.status} {await _message(resp)}")
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

    async def async_get_bills(self, account: str, start: date, end: date) -> list[Bill]:
        """Bills issued from ``start`` to ``end``, attributed to their periods."""
        transactions = await self._get(
            f"/accounts/{account}/transactions",
            {"startDate": server_date(start), "endDate": server_date(end)},
            allow_404=True,
        )
        periods: Any = {}
        try:
            periods = await self._get(f"/accounts/{account}/billing/periods")
        except GmpError as err:
            _LOGGER.debug("Billing periods unavailable, using bill dates: %s", err)
        period_list = periods.get("periods") if isinstance(periods, dict) else periods
        return parse_bills(transactions, period_list)

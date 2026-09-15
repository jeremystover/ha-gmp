# Green Mountain Power — Home Assistant integration

Imports usage, solar export, billed cost and net-metering credits from a
**Green Mountain Power** (Vermont) account into Home Assistant, the same way
the built-in Opower integration does for PG&E and other utilities.

It uses GMP's own customer API — the one behind greenmountainpower.com/account
and the GMP app. That API is undocumented; every endpoint here was read off the
portal's JavaScript and checked against a live account. See
[How it was worked out](#how-it-was-worked-out).

## What it creates

**Long-term statistics**, for the Energy dashboard. These are not sensors:
GMP publishes meter reads hours to a day after the fact, and a sensor would
misstate when the energy was used. The integration writes each read at the
hour it actually happened.

| Statistic | Unit | What it is |
| --- | --- | --- |
| `gmp:<account>_energy_consumption` | kWh | Energy taken from the grid |
| `gmp:<account>_energy_return` | kWh | Energy sent back to the grid |
| `gmp:<account>_energy_generation` | kWh | Gross solar production, only if GMP meters it on your account |
| `gmp:<account>_energy_cost` | USD | Billed amount, at the start of each billing period |
| `gmp:<account>_energy_compensation` | USD | A bill that came out negative (credits exceeded charges) |

**Sensors**, refreshed twice a day, on one device per account:

| Entity | What it is |
| --- | --- |
| **Net metering credits** | Dollar balance of banked credits; attributes list each credit and when it expires |
| **Account balance** | What GMP says is owed, with past-due flags as attributes |
| **Last bill** | Amount of the newest bill, with its date and period start |
| **Latest usage data** | Start of the newest hourly read GMP has published — how far behind the data is |

## Install

**Via HACS:** ⋮ → Custom repositories → add
`https://github.com/jeremystover/ha-gmp` as an **Integration** → download →
restart → Settings → Devices & services → Add integration → Green Mountain
Power.

Sign in with your greenmountainpower.com username and password. If the login
can see more than one service account you pick one; if GMP lists none, you
type the account number from your bill.

> The password is stored in Home Assistant's config entry, like any other
> cloud integration. If GMP rejects it later, Home Assistant asks for a new
> one rather than silently stopping.

### Energy dashboard

Settings → Dashboards → Energy → **Add consumption** under Electricity grid:

- **Consumed energy:** `GMP <account> consumption`
- **Use an entity tracking the total costs:** `GMP <account> cost`
- **Add return:** `GMP <account> return`, with `GMP <account> compensation`
  as its cost entity

If your account has the generation statistic, add it under **Solar panels**.
If you already track production from the inverter (SolisCloud, Enphase, …),
GMP's number is the utility meter's view of the same thing — useful for
reconciling a bill, redundant on the dashboard. Pick one.

The first refresh backfills **three years of daily** reads and **sixty days of
hourly** reads, plus three years of bills, so the dashboard has history from
the start. Later refreshes fetch hourly data from a few days before the newest
stored read.

## What to expect

- **Data is a day behind.** GMP posts reads with a delay; the integration
  checks every twelve hours. **Latest usage data** says where it currently
  stands.
- **Cost is per billing period, not per hour.** GMP's usage feed has no
  prices, so cost comes from actual bills, attributed to the first day of the
  period each covers. Monthly totals on the dashboard are right to the penny;
  daily views show the whole bill on one day. For a per-hour estimate use the
  dashboard's static-price option on the consumption statistic instead.
- **A stored read is never rewritten.** Reads are written once each interval
  has ended. If GMP later revises a read, the original stays.
- **Timestamps.** GMP labels every timestamp `Z` but the values are Eastern
  wall-clock time, and hourly rows are labelled by the *end* of the hour. Both
  are corrected, matching what the GMP portal itself does.

## How it was worked out

The `greenmountainpower` package on PyPI proved the login flow (a password
grant against `/api/v2/applications/token` with the portal's public client id)
but reads only `consumed` and has been unmaintained since 2023. Everything
else came from the portal's unminified Vue bundle:

| Endpoint | Used for |
| --- | --- |
| `GET /api/v2/usage/{account}/{hourly,daily,monthly}?startDate&endDate&temp=f` | `consumed`, `returnedGeneration`, `generation` per interval |
| `GET /api/v2/accounts/{account}/generation/credits` | `expiringCredits[]` with `creditDate`, `expirationDate`, `amount` |
| `GET /api/v2/accounts/{account}/transactions?startDate&endDate` | Bills (`type == "Bill Segment"`, `payoffAmount`) |
| `GET /api/v2/accounts/{account}/billing/periods` | Billing period spans |
| `GET /api/v2/accounts/{account}/status` | Balance and standing |
| `GET /api/v2/users/current` | Accounts behind the login (`customData.energyAccounts`) |

GMP answers 404, not an empty list, when an account has no credits or no
transactions in range; the client treats that as empty.

GMP will also issue an API key ID and secret on request to the Distributed
Resources team (DR@greenmountainpower.com), which the same usage endpoint
accepts as HTTP Basic auth. This integration does not use that yet.

## Development

The parsing is plain Python and tests run without Home Assistant:

```sh
pip install aiohttp pytest
pytest
```

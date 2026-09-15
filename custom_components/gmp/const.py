"""Constants for the Green Mountain Power integration."""

DOMAIN = "gmp"

CONF_ACCOUNT_NUMBER = "account_number"

# GMP posts meter data with a delay of hours to a day. Twice a day keeps the
# dashboard at most half a day further behind than GMP itself.
UPDATE_INTERVAL_HOURS = 12

# First-run backfill: monthly reads for the long tail, daily for the last
# year, hourly for the recent stretch -- the cascade Home Assistant's own
# Opower integration uses. Monthly reads come from billing, so they reach
# back before a meter that reports intervals was installed.
MONTHLY_BACKFILL_DAYS = 3 * 365
DAILY_BACKFILL_DAYS = 365
HOURLY_BACKFILL_DAYS = 60
BILL_BACKFILL_DAYS = 3 * 365

# Each later run re-requests a little history so a late-posted read is not
# missed. Rows already stored are never rewritten.
USAGE_REFETCH_DAYS = 3
BILL_REFETCH_DAYS = 45

# Request windows, sized to what the portal itself asks for: hourly and
# daily one billing period at a time, monthly a year at a time. GMP's limits
# are undocumented and a range it dislikes comes back empty, not as an error.
HOURLY_CHUNK_DAYS = 31
DAILY_CHUNK_DAYS = 31
MONTHLY_CHUNK_DAYS = 366

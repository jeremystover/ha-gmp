"""Constants for the Green Mountain Power integration."""

DOMAIN = "gmp"

CONF_ACCOUNT_NUMBER = "account_number"

# GMP posts meter data with a delay of hours to a day. Twice a day keeps the
# dashboard at most half a day further behind than GMP itself.
UPDATE_INTERVAL_HOURS = 12

# First-run backfill. Daily reads for the long tail, hourly for the recent
# stretch, matching the shape of Home Assistant's own Opower integration.
DAILY_BACKFILL_DAYS = 3 * 365
HOURLY_BACKFILL_DAYS = 60
BILL_BACKFILL_DAYS = 3 * 365

# Each later run re-requests a little history so a late-posted read is not
# missed. Rows already stored are never rewritten.
USAGE_REFETCH_DAYS = 3
BILL_REFETCH_DAYS = 45

# Request windows. GMP's limits are undocumented; the portal never asks for
# more than a billing period of hourly data, so stay in that neighbourhood.
HOURLY_CHUNK_DAYS = 31
DAILY_CHUNK_DAYS = 366

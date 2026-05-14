"""Tunables. Committed to git — not env-driven."""

# Pipeline batch size: companies processed per micro-pipeline iteration.
BATCH_SIZE = 20

# Maximum DMs sent per UTC day (across initial + follow-ups). Raise carefully.
DAILY_LIMIT = 100

# Follow-up cadence: send a follow-up every day for this many days if no reply.
# Sequence numbers: 1 = initial, 2..(FOLLOWUP_DAYS+1) = follow-ups.
FOLLOWUP_DAYS = 7
FOLLOWUP_INTERVAL_HOURS = 24

# Activity thresholds (days since last tweet).
ACTIVITY_ACTIVE_DAYS = 7
ACTIVITY_SEMI_DAYS = 30
ACTIVITY_DORMANT_DAYS = 90  # > this = dead (skip DM)

# Pacing between sends (seconds). Random.uniform(min, max).
SEND_DELAY_MIN = 180
SEND_DELAY_MAX = 360

# Search delays (DDG + handle verification).
SEARCH_DELAY_MIN = 2.0
SEARCH_DELAY_MAX = 5.0

# Idle sleep when orchestrator finds no work (seconds).
ORCHESTRATOR_IDLE_SLEEP = 1800

# How often (seconds) to run reply-check + follow-up queue inside the loop.
REPLY_CHECK_INTERVAL = 6 * 3600
FOLLOWUP_QUEUE_INTERVAL = 24 * 3600

# After verifying a handle in find_handles, click the Follow button on x.com.
FOLLOW_AFTER_VERIFY = True

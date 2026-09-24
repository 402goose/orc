# Bounded automatic label drafts

Garden settings may specify `max_drafts_per_day`, an integer from 1 to 100.
Started automatic jobs count against the UTC-day limit, including failed jobs.
The count is read from retained jobs and survives server restart. At the limit,
the queue pauses until the next UTC day or an explicit settings change. Pausing
never cancels an already running job; use its Stop control separately.

This is an attempt cap, not a monetary budget or provider billing measurement.
Retain job history for accounting. Manual requests are outside the automatic cap.
One active draft at a time and no automatic retry for an attempted decision remain
in effect. Malformed configured limits fail closed for automatic dispatch.

TENET-bound workspaces default to ten automatic drafts per day and cannot select
an unlimited queue. They require single-worker Codex drafts, human approval, and
an explicit operator-allowed non-ultra model/effort pair. The pair is frozen when
admitted; later policy changes may refuse queued work instead of substituting a
model. Approval and training are separate actions and are not enabled by this
setting. Binding does not itself enable garden: `enabled` remains opt-in.

Standalone workspaces remain uncapped when the new setting is omitted. The old
`daily_limit` property remains ignored for compatibility; it is not an alias for
this contract. Existing standalone council behavior is unchanged.

# Kandji → Freshservice Device Ownership Sync

A scheduled job that keeps an IT service desk's asset records (Freshservice)
in step with the MDM (Kandji, now Iru). Kandji is treated as the source of
truth for who has which laptop, and Freshservice is updated to match, without
anyone editing asset records by hand.

## The problem

Every laptop is enrolled in Kandji, and IT assigns each device to a user
there. Freshservice tracks the same devices as assets, but owners were copied
over by hand. The process broke down whenever a device changed hands. When
someone left, their laptop was wiped and re-enrolled in Kandji for the next
person, and Kandji showed the change immediately. Freshservice kept showing
the previous owner until someone remembered to fix it. Over time the asset
register drifted. Most devices were marked "In Stock" despite being in use,
and several showed the wrong person.

Freshservice has no built-in Kandji integration. There's an open feature
request, but nothing ships today.

## What it does

- **Matches by serial number.** Device names and MDM IDs change on
  re-enrollment, but the serial doesn't.
- **Sets the owner from Kandji's assigned user.** Each user is looked up in
  Freshservice by email, first as a requester and then as an agent. IT staff
  are agents, not requesters, so a requester-only lookup silently misses
  their devices.
- **Sets the status:** "In Use" when assigned to a person, "In Stock" when
  assigned to the IT spares account.
- **Changes only what's out of date.** Everything already correct is left
  alone.
- **Reports, never auto-fixes, anything ambiguous:**
  - a person who isn't in Freshservice yet;
  - a device with no asset record;
  - an asset whose device is gone from the MDM;
  - duplicate serials or email matches.
- **Sends a Slack DM after every run** with what changed, what needs a human,
  and which devices have stopped checking in to the MDM. On quiet runs it
  still says "All in sync", so a missing message means the job didn't run.
- **Runs every two weeks** from launchd on a Mac.

## Safety design

The job writes to a production asset register unattended, so it was built
around a few rules:

- **Dry run by default.** Nothing changes without `--apply`. Every stage was
  run as a preview first.
- **The MDM is never written to.** The Kandji client in the code only exposes
  GET requests, even though the API token can do more.
- **Minimal writes.** It only ever changes two fields on an asset: owner and
  status. It never creates, deletes or clears anything.
- **Read-back verification.** After each update the asset is re-fetched. Any
  field that doesn't match is reported as a failure rather than assumed done.
- **Safety cap.** If a run would change more owners than a configured limit,
  it stops before writing anything and alerts in Slack. A sudden spike usually
  means bad data or a matching bug, not a real wave of handoffs.
- **Bounded everything.** Request timeouts, capped retries on rate limits and
  5xx errors, and capped pagination.
- **Least privilege.** A dedicated Slack app with only `chat:write`, rather
  than reusing a broader onboarding bot.
- **`--only SERIAL` for staged rollout.** The first real write was a single
  device, verified independently, before the full run.

## How it was built

Built incrementally with Claude Code, one checkpoint at a time:

1. **Feasibility.** Checked each API's public docs. Then did live read-only
   checks for the questions the docs couldn't settle: whether assets can be
   searched by serial, the shape of the MDM's user object, and the
   requester-versus-agent lookup.
2. **Read-only compare.** Printed the plan and checked it against a manual
   baseline.
3. **Slack summary.**
4. **`--apply` on one device**, with read-back.
5. **Full run.** It fixed 7 wrong owners and 48 stale statuses across a
   ~55-laptop Mac and Windows fleet spanning two brands. A follow-up dry run
   showed everything in sync.
6. **Scheduling.** A launchd agent plus a wrapper that loads credentials,
   logs, gates the run to every two weeks, and alerts on crashes.

Python 3.9 standard library only, so there's nothing to install.

## Files

| File | Purpose |
|---|---|
| `sync.py` | The sync: compare, plan, apply, verify, Slack summary |
| `run.sh` | Scheduled entry point: loads keys, every-two-weeks gate, logging, crash alert |
| `com.example.kandji-freshservice-sync.plist` | launchd agent (weekly trigger; `run.sh` gates it to two weeks) |
| `config.example.json` | Settings and IDs. Copy to `config.json`. No secrets |

## Setup

1. Copy `config.example.json` to `config.json` and fill in:
   - your Freshservice domain;
   - the laptop asset type ID and its serial and state field names (from
     `GET /api/v2/asset_types/<id>/fields`);
   - your Slack member ID.
2. Export credentials in your shell profile: `KANDJI_API_URL`,
   `KANDJI_API_TOKEN` (device read), `FRESHSERVICE_API_KEY`, and
   `SLACK_DEVICE_SYNC_TOKEN` (a bot with `chat:write` only).
3. Preview: `python3 sync.py --no-slack`.
4. Try one device: `python3 sync.py --apply --only <serial>`.
5. Schedule: edit the paths in the plist, copy it to `~/Library/LaunchAgents/`,
   and run `launchctl bootstrap gui/$(id -u) <plist>`.

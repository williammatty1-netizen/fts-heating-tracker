FTS Renewable Heating Tracker

Watches the UK Find a Tender Service (FTS) for new/updated contract notices in Greater Manchester (Manchester, Trafford, Salford, Stockport) that mention heat pumps, district heating, HIUs, MVHR or MEP works, and pushes new matches to Telegram and/or Slack. Runs for free on a daily schedule via GitHub Actions.

Files
File	Purpose
fts_heating_tracker.py	The script itself
requirements.txt	Python dependencies
seen_notices.json	Auto-created dedupe state (do not edit by hand)
.github/workflows/fts-tracker.yml	Daily cron job definition
tests/test_tracker.py	Offline self-tests (no network calls)
1. Run it locally first (recommended)
bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry run: shows what it *would* send, doesn't call Telegram/Slack,
# doesn't write seen_notices.json
python fts_heating_tracker.py --dry-run

If that logs matches (or "No new matches" if there genuinely are none in the last ~26h), the core logic is working. Run the offline test suite too:

bash
pip install pytest
python -m pytest tests/ -q
2. Create a Telegram bot (free, ~2 minutes)
In Telegram, message @BotFather → /newbot → follow the prompts. BotFather gives you a bot token like 123456789:AAExample-Token.
Start a chat with your new bot (search its username, hit Start), or add it to a group/channel you own.
Get your chat ID:
Send any message to the bot first, then visit in a browser: https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
Find "chat":{"id":123456789, ...} in the JSON response — that number (it can be negative for groups) is your TELEGRAM_CHAT_ID.
Test it:
bash
export TELEGRAM_BOT_TOKEN="123456789:AAExample-Token"
export TELEGRAM_CHAT_ID="123456789"
python fts_heating_tracker.py --dry-run   # remove --dry-run once happy
2b. Or use a Slack Incoming Webhook instead (or as well)
Go to https://api.slack.com/apps → Create New App → From scratch → pick your workspace.
Under Incoming Webhooks, toggle it on → Add New Webhook to Workspace → choose the channel → copy the Webhook URL (https://hooks.slack.com/services/T000/B000/xxxxxxxx).
Test it:
bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T000/B000/xxxxxxxx"
python fts_heating_tracker.py --dry-run

You can set both Telegram and Slack env vars — the script will send to whichever ones are configured.

3. Put this in a GitHub repository
bash
git init
git add .
git commit -m "Add FTS renewable heating tracker"
git branch -M main
git remote add origin https://github.com/<you>/<your-repo>.git
git push -u origin main

A private repo is fine and free (GitHub gives free accounts 2,000 Actions minutes/month; this job runs in seconds, once a day, so you'll never come close). A public repo gets unlimited free Actions minutes.

4. Add your secrets to the repo

In GitHub: Settings → Secrets and variables → Actions → New repository secret, add whichever of these you're using:

TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
SLACK_WEBHOOK_URL

Never commit tokens/webhooks directly into the code or workflow file.

5. Enable the workflow

The workflow file .github/workflows/fts-tracker.yml is already set up to:

Run every day at 07:15 UTC (edit the cron: line to change this — crontab.guru helps build the expression; GitHub Actions cron is always in UTC, and schedules can be delayed by a few minutes at busy times, which is normal).
Also be runnable on demand from the Actions tab → select the workflow → Run workflow (handy for testing).
Commit the updated seen_notices.json back to the repo after each run, so matches are never re-notified on the next day's run. This works because the workflow is granted permissions: contents: write and uses the automatically-provided GITHUB_TOKEN to push — no extra setup needed.

Once the file is pushed to GitHub with a schedule: trigger, it starts running automatically — nothing else to do. You can watch runs under the repo's Actions tab.

Configuration reference

All of these are optional; sensible defaults are baked in.

Env var / flag	Default	Meaning
FTS_LOCATION_KEYWORDS / --location-keywords	Manchester,Trafford,Salford,Stockport	Comma-separated location keywords, matched as free text anywhere in the notice (any one must match)
FTS_POSTCODE_AREAS / --postcode-areas	CA,LA,FY,PR,BB,BL,OL,WN,L,M,WA,CH,CW,SK,ST,TF,LL	Comma-separated Royal Mail postcode AREA codes (the letters before the first digit, e.g. SK in SK3 0SD). Matched against each notice's actual postcode field(s), never free text — short codes like M, L, ST would be far too noisy to text-search for. A notice matches location if it matches either this or FTS_LOCATION_KEYWORDS.
FTS_HEATING_KEYWORDS / --heating-keywords	heat pump,district heat,HIU,MVHR,MEP	Comma-separated heating/MEP keywords (any one must match)
FTS_LOOKBACK_HOURS / --lookback-hours	26	How far back to query updatedFrom. Kept slightly over 24h so a daily cron never leaves a gap even if a run is late.
FTS_STATE_FILE / --state-file	seen_notices.json	Dedupe state file path
FTS_DRY_RUN / --dry-run	off	Log what would be sent without calling Telegram/Slack or touching state
LOG_LEVEL	INFO	Set to DEBUG for verbose request/pagination logs
How matching works

A notice is reported only if both of these hold:

Location — either (a) at least one FTS_LOCATION_KEYWORDS entry appears as free text in the notice's title, description, buyer name, buyer address, item descriptions, lot details, or delivery addresses, or (b) the notice's actual postcode (buyer address or a delivery address) falls in one of the FTS_POSTCODE_AREAS codes. Only (a) is ever text-searched — postcodes are matched properly against the postcode field itself, so a short area code like M can't accidentally match unrelated words.
Heating/MEP — at least one FTS_HEATING_KEYWORDS entry appears as free text.

Matching is case-insensitive and word-bounded (so MEP won't accidentally match inside an unrelated word). A notice already recorded in seen_notices.json (keyed by its OCDS ocid, i.e. the whole procurement process) is never re-sent, even if FTS returns it again on a later day because it was updated (e.g. moved from "tender" to "award" stage).

Notes & limitations
The Find a Tender API only covers notices above the UK's high-value thresholds (roughly £122k+ for local authority services/goods, higher for works) — smaller local contracts won't appear here. For lower-value opportunities you'd also want to poll Contracts Finder (https://www.contractsfinder.service.gov.uk/apidocumentation), which has a very similar OCDS API shape; the same filtering/notification code in this script would need only a new fetch function pointed at that endpoint.
FTS field completeness varies by notice (some lack value or tenderPeriod); the script displays "Not disclosed"/"Not specified" rather than failing.
If you rename/move the repo or the state file, you'll get one batch of "new" notifications the next run since the dedupe history starts fresh.

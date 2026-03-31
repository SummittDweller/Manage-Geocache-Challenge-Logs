# Manage Geocache Challenge Logs

A Flet desktop app that scans all of a geocacher's **Write Note** logs on
[geocaching.com](https://www.geocaching.com) and identifies those left on
**Challenge Caches** – helping users discover past challenge-cache finds they
may now qualify for.

The app is patterned after
[geocaching-review-flet-selenium](https://github.com/SummittDweller/geocaching-review-flet-selenium)
and uses Selenium to drive a real Firefox browser session.

---

## Features

- **Secure login** – enter your geocaching.com username and password; optional
  "remember password" setting persisted in Flet client storage.
- **Optional Firefox profile** – paste the path to an existing Firefox profile
  so your extensions and saved cookies carry over (leave blank to use a fresh
  session).
- **HTML-first full log scan** – scans the All Logs page in your authenticated
  Firefox session and applies the Write Note filter; API fallback is used only
  when profile/log APIs are available.
- **Mystery Challenge detection** – keeps only Write Note entries where the
  cache title contains *Challenge* and the cache appears to be a
  mystery/puzzle/question-mark cache.
- **Project-GC checker automation** – opens the checker from each challenge
  cache page, handles Project-GC and geocaching OAuth consent flow, runs the
  checker, and detects both success and failure outcomes.
- **Existing found-log short-circuit** – if the selected user already has a
  **Found It** log on a cache page, the app records that cache as
  `Write Note + Found It` and skips Project-GC checker execution.
- **CSV export** – writes results to
  `~/challenge_write_notes_YYYYMMDD_HHMMSS.csv` in your home directory.
  Columns now include checker outcome and generated example log text.
- **Durable autosave** – writes an in-progress CSV after each processed result
  row to reduce data loss if a scan is interrupted.
- **Real-time progress** – progress bar, in-app status text, terminal output,
  and a persistent logfile update as the scan runs.

---

## Installation

Run the included `run.sh` script which will:

1. Locate a suitable Python 3.10+ interpreter.
2. Create a `.venv` virtual environment (if one doesn't already exist).
3. Install all dependencies from `python-requirements.txt`.
4. Launch the Flet desktop application.

```bash
chmod +x run.sh
./run.sh
```

---

## Usage

1. **Launch** – run `./run.sh`.
2. **Enter credentials** – type your geocaching.com username and password.
   Optionally check *Remember password* and/or paste a Firefox profile path.
3. **Start** – click the **Start** button. Firefox will open and log you in
  automatically. Wait for the *"Logged in as …"* confirmation.
4. **Scan** – click **Scan My Logs**.  The app will page through all your Write
  Note logs, first checking each cache page for an existing Found It log from
  the specified user. If found, it records `Write Note + Found It` and moves
  on; otherwise it opens challenge checker pages and evaluates qualification
  state.
5. **Results** – when the scan finishes, a summary is shown and a CSV file is
  saved to your home directory. Open the CSV with any spreadsheet application
   to review the results.
6. **Log file** – detailed scan and startup logs are written to
  `manage_geocache_challenge_logs.log` in the project root.

---

## Environment Variables

The app loads `.env` automatically at startup.

| Variable | Description |
|--------|-------------|
| `GEOCACHING_USERNAME` | Optional default username |
| `GEOCACHING_PASSWORD` | Optional default password |
| `FIREFOX_PROFILE_PATH` | Optional Firefox profile path |
| `REMEMBER_GEOCACHING_PASSWORD` | Optional app preference flag |
| `GC_DEBUG_STOP_AFTER_MATCH_COUNT` | Debug stop threshold. `0` = no early stop (default), `N` = stop after `N` matched candidates and keep browser open |
| `GC_DEBUG_STOP_AFTER_FIRST_LOG` | Legacy fallback. `true/1` = stop after 1, `false/0` = no early stop |
| `GC_DEBUG_STOP_AFTER_FILTER_APPLIED` | Temporary selector-debug mode. `true/1` = stop immediately after Write Note filter is applied and leave browser open |

---

## CSV Output

| Column | Description |
|--------|-------------|
| `log_date` | Date the Write Note log was posted (YYYY-MM-DD) |
| `gc_code` | Geocache GC code (e.g. `GC12345`) |
| `cache_name` | Full name of the Challenge Cache |
| `cache_url` | Direct link to the cache page on geocaching.com |
| `log_url` | Direct link to your geocaching.com Write Note log entry (including modern `/live/log/GL...` links when detectable from the filtered logs page) |
| `checker_status` | Outcome: `SUCCESS!`, `Failed`, or `Write Note + Found It` |
| `checker_example_log` | Project-GC generated text suitable for copy/paste into a Found It log |

Results are sorted by `log_date` descending (newest first).

During a running scan, an autosave file is also maintained at
`challenge_write_notes_in_progress.csv` in the project root.

---

## Troubleshooting

### `OAuth consent page found but Accept button was not clicked`

- Meaning: The app reached geocaching OAuth consent but could not click the
  consent control.
- Action: Keep browser visible and confirm the button text (`Agree`, `Accept`,
  etc.). Re-run and share the new `CHECKER` lines if it persists.

### `Not on Project-GC host after auth flow: https://www.geocaching.com/oauth/...`

- Meaning: OAuth redirect back to Project-GC did not complete yet (or failed).
- Action: Wait briefly (redirect can take several seconds), or manually click
  consent once. If persistent, capture the exact consent page HTML/button.

### `Run checker button not found/clickable`

- Meaning: Checker page loaded but the run control is not interactable.
- Action: Verify you are on the challenge checker page and signed in. Re-run
  with `GC_DEBUG_STOP_AFTER_MATCH_COUNT=1` to inspect the page state.

### `Max execution time reached`

- Meaning: Project-GC checker backend timed out for that run.
- Action: The app retries once automatically. If it still fails, rerun later
  (queue/load related) or run manually for that cache.

### `Challenge check failure detected for ...`

- Meaning: Checker result indicates you currently do not qualify.
- Action: CSV `checker_status` will be `Failed` for that cache.

### `Example log textarea found no content`

- Meaning: Checker finished but generated example text was unavailable.
- Action: Open checker page manually and verify `cc_ExampleLog` is populated.
  Share the page variant if this happens repeatedly.

### `log_url` is blank

- Meaning: A geocaching Write Note link was not detectable in the logs page DOM
  block for that entry.
- Action: Use `cache_url` and/or checker URL for manual follow-up.

### `DEBUG_STOP | Stopping immediately after Write Note filter is applied`

- Meaning: Temporary filter-stop mode is enabled.
- Action: Set `GC_DEBUG_STOP_AFTER_FILTER_APPLIED=false` in `.env` to resume
  full processing.

### `CHECKER | ... connection reset by peer`

- Meaning: Transient network reset while loading checker/cache page.
- Action: The app retries page open operations automatically. Re-run if needed.

---

## Requirements

- Python 3.10+
- Firefox browser
- See `python-requirements.txt` for Python package dependencies.

---

## HTML Target Reference

See [HTML_TARGETS.md](HTML_TARGETS.md) for a consolidated list of selectors,
tags, and URL patterns the app currently uses during scan, auth, and checker
automation.

---

## Project Setup

Structured following the guidance at
[https://flet.dev/docs/getting-started/create-flet-app](https://flet.dev/docs/getting-started/create-flet-app).

```zsh
cd ~/GitHub
mkdir Manage-Geocache-Challenge-Logs
cd Manage-Geocache-Challenge-Logs
python3 -m venv .venv
source .venv/bin/activate
pip3 install 'flet[all]'
pip3 install selenium webdriver-manager python-dotenv
```

---

## Run the app

Desktop:

```bash
flet run src/main.py
```

Web (development only):

```bash
flet run --web src/main.py
```

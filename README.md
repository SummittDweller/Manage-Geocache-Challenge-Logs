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
- **Full log scan** – iterates through every Write Note log in your account
  using the geocaching.com internal JSON API, with an HTML-scraping fallback.
- **Challenge Cache detection** – flags any cache whose name contains the word
  *Challenge* (case-insensitive), which is the standard community convention.
- **CSV export** – writes results to
  `~/challenge_write_notes_YYYYMMDD_HHMMSS.csv` in your home directory.
  Columns: `log_date`, `gc_code`, `cache_name`, `cache_url`, `log_url`.
- **Real-time progress** – progress bar and status text update as the scan runs.

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
3. **Start** – click the **Start** button.  Firefox will open and log you in
   automatically.  Wait for the *"Logged in as …"* confirmation.
4. **Scan** – click **Scan My Logs**.  The app will page through all your Write
   Note logs and highlight those on Challenge Caches.
5. **Results** – when the scan finishes, a summary is shown and a CSV file is
   saved to your home directory.  Open the CSV with any spreadsheet application
   to review the results.

---

## CSV Output

| Column | Description |
|--------|-------------|
| `log_date` | Date the Write Note log was posted (YYYY-MM-DD) |
| `gc_code` | Geocache GC code (e.g. `GC12345`) |
| `cache_name` | Full name of the Challenge Cache |
| `cache_url` | Direct link to the cache page on geocaching.com |
| `log_url` | Direct link to the individual log entry |

Results are sorted by `log_date` descending (newest first).

---

## Requirements

- Python 3.10+
- Firefox browser
- See `python-requirements.txt` for Python package dependencies.

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

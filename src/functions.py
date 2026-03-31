# functions.py – core logic for Manage-Geocache-Challenge-Logs
import csv
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import flet as ft
from app_refs import (
    firefox_profile_path_ref,
    loading_status_ref,
    progress_bar_ref,
)
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

try:
    from webdriver_manager.firefox import GeckoDriverManager
except Exception:
    GeckoDriverManager = None

# ---------------------------------------------------------------------------
# Helper: dismiss cookie / consent banners
# ---------------------------------------------------------------------------
def _dismiss_cookie_banner(driver, timeout=5):
    decline_ids = [
        "CybotCookiebotDialogBodyButtonDecline",
        "onetrust-reject-all-handler",
    ]
    accept_ids = [
        "CybotCookiebotDialogBodyButtonAccept",
        "onetrust-accept-btn-handler",
    ]
    for btn_id in decline_ids + accept_ids:
        try:
            btn = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.ID, btn_id))
            )
            btn.click()
            return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helper: perform geocaching.com login
# ---------------------------------------------------------------------------
def _perform_geocaching_login(driver, username, password):
    try:
        user_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "UsernameOrEmail"))
        )
        user_field.clear()
        user_field.send_keys(username)
    except TimeoutException:
        # Primary selector timed out – try broader CSS fallbacks
        user_field = driver.find_element(
            By.CSS_SELECTOR,
            "input[name='UsernameOrEmail'], input[type='email'], input[id*='user']",
        )
        user_field.clear()
        user_field.send_keys(username)

    try:
        pw_field = driver.find_element(By.ID, "Password")
    except Exception:
        pw_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    pw_field.clear()
    pw_field.send_keys(password)

    try:
        login_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], input[type='submit']"))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", login_button)
        time.sleep(0.5)
        login_button.click()
    except Exception:
        pw_field.submit()


# ---------------------------------------------------------------------------
# Initialize the Selenium WebDriver and log in to geocaching.com
# ---------------------------------------------------------------------------
def initialize_driver(page, username=None, password=None):
    """
    Launch Firefox, log in to geocaching.com, and return the driver.
    Mirrors the pattern used by geocaching-review-flet-selenium.
    """

    def update_loading(msg, progress=None):
        if loading_status_ref.current:
            loading_status_ref.current.value = msg
            loading_status_ref.current.update()
        if progress is not None and progress_bar_ref.current:
            progress_bar_ref.current.value = progress
            progress_bar_ref.current.update()
        print(msg)

    update_loading("Setting up Firefox driver...", 0.05)

    options = FirefoxOptions()

    # Optionally load a Firefox profile (e.g. to preserve extensions/cookies)
    profile_path = ""
    if firefox_profile_path_ref.current:
        profile_path = (firefox_profile_path_ref.current.value or "").strip()
    if not profile_path:
        profile_path = (page.client_storage.get("firefox_profile_path") or "").strip()

    if profile_path and os.path.isdir(profile_path):
        options.add_argument("-profile")
        options.add_argument(profile_path)

    update_loading("Launching Firefox...", 0.10)

    service = None
    if GeckoDriverManager:
        try:
            service = FirefoxService(GeckoDriverManager().install())
        except Exception:
            service = None

    if service:
        driver = webdriver.Firefox(service=service, options=options)
    else:
        driver = webdriver.Firefox(options=options)

    driver.set_window_size(1280, 900)

    update_loading("Navigating to geocaching.com...", 0.20)

    # Navigate to geocaching.com and handle sign-in
    driver.get("https://www.geocaching.com/account/signin?returnUrl=%2F")
    time.sleep(2)

    _dismiss_cookie_banner(driver)

    update_loading("Logging in...", 0.35)

    _perform_geocaching_login(driver, username, password)

    # Wait for successful login (redirected away from sign-in page)
    try:
        WebDriverWait(driver, 30).until(
            lambda d: "signin" not in d.current_url
        )
    except TimeoutException:
        raise RuntimeError(
            "Login timed out – please verify your username and password."
        )

    update_loading("Login successful. Verifying account...", 0.60)

    # Detect logged-in username from the page
    detected_user = ""
    try:
        driver.get("https://www.geocaching.com/api/proxy/web/v1/users/me")
        time.sleep(1)
        body = driver.find_element(By.TAG_NAME, "body").text
        user_data = json.loads(body)
        detected_user = (
            user_data.get("username")
            or user_data.get("displayName")
            or user_data.get("referenceCode")
            or ""
        )
        driver._gc_user_data = user_data
    except Exception:
        driver._gc_user_data = {}
        detected_user = ""

    if detected_user and username and detected_user.lower() != username.lower():
        # Different user already logged in – log out and back in
        update_loading(f"Switching accounts (was {detected_user})...", 0.70)
        driver.get("https://www.geocaching.com/account/signout")
        time.sleep(2)
        driver.get("https://www.geocaching.com/account/signin?returnUrl=%2F")
        time.sleep(2)
        _dismiss_cookie_banner(driver)
        _perform_geocaching_login(driver, username, password)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: "signin" not in d.current_url
            )
        except TimeoutException:
            raise RuntimeError("Login timed out after account switch.")

        driver.get("https://www.geocaching.com/api/proxy/web/v1/users/me")
        time.sleep(1)
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            user_data = json.loads(body)
            detected_user = (
                user_data.get("username")
                or user_data.get("displayName")
                or user_data.get("referenceCode")
                or ""
            )
            driver._gc_user_data = user_data
        except Exception:
            driver._gc_user_data = {}

    driver._gc_active_user = detected_user or username or ""
    update_loading(f"Ready – logged in as {driver._gc_active_user}.", 1.0)

    return driver


# ---------------------------------------------------------------------------
# Scan: fetch all Write Note logs and filter for Challenge Caches
# ---------------------------------------------------------------------------
def scan_challenge_write_notes(driver, status_callback=None, progress_callback=None):
    """
    Scan all of the logged-in user's Write Note logs (logType 4) and return
    those where the cache name contains the word 'Challenge'.

    Args:
        driver: Selenium WebDriver (already logged in)
        status_callback: callable(msg: str, color=None) for status updates
        progress_callback: callable(value: float, label: str) for progress bar

    Returns:
        list of dicts with keys: log_date, gc_code, cache_name, cache_url, log_url
    """
    results = []

    def update_status(msg, color=None):
        if status_callback:
            status_callback(msg, color)
        print(msg)

    def update_progress(value, label=""):
        if progress_callback:
            progress_callback(value, label)

    # ---- get user reference code ----------------------------------------
    update_status("Fetching your geocaching profile...")
    user_data = getattr(driver, "_gc_user_data", {})
    user_ref_code = user_data.get("referenceCode", "")

    if not user_ref_code:
        try:
            driver.get("https://www.geocaching.com/api/proxy/web/v1/users/me")
            time.sleep(1)
            body = driver.find_element(By.TAG_NAME, "body").text
            user_data = json.loads(body)
            user_ref_code = user_data.get("referenceCode", "")
            driver._gc_user_data = user_data
        except Exception as exc:
            update_status(f"Could not retrieve user profile: {exc}", ft.Colors.RED)

    if user_ref_code:
        results = _scan_via_api(driver, user_ref_code, update_status, update_progress)
    else:
        update_status("Falling back to HTML scraping (no user reference code)...")
        results = _scan_via_html(driver, update_status, update_progress)

    return results


# ---------------------------------------------------------------------------
# Private: API-based scan
# ---------------------------------------------------------------------------
def _scan_via_api(driver, user_ref_code, update_status, update_progress):
    """Fetch Write Note logs via the geocaching.com internal JSON API."""
    results = []
    skip = 0
    take = 50
    total_checked = 0
    page_num = 0

    update_status(f"Starting API scan for user {user_ref_code}...")
    update_progress(0.0, "Starting...")

    while True:
        page_num += 1
        api_url = (
            f"https://www.geocaching.com/api/proxy/web/v1/users/"
            f"{user_ref_code}/geocachelogs"
            f"?logTypes=4&skip={skip}&take={take}"
        )

        try:
            driver.get(api_url)
            time.sleep(1)
            body = driver.find_element(By.TAG_NAME, "body").text
            logs = json.loads(body)
        except Exception as exc:
            update_status(f"API error on page {page_num}: {exc}", ft.Colors.YELLOW)
            break

        if not isinstance(logs, list) or len(logs) == 0:
            break

        for log in logs:
            total_checked += 1

            # Extract fields – handle both flat and nested structures
            log_date = (log.get("loggedDate") or log.get("entryDate") or "")[:10]
            gc_code = (
                log.get("geocacheCode")
                or (log.get("geocache") or {}).get("referenceCode")
                or (log.get("geocache") or {}).get("code")
                or ""
            )
            cache_name = (
                log.get("geocacheName")
                or (log.get("geocache") or {}).get("name")
                or (log.get("geocache") or {}).get("title")
                or ""
            )

            if gc_code:
                gc_code = gc_code.upper()

            cache_url = (
                f"https://www.geocaching.com/geocache/{gc_code}" if gc_code else ""
            )
            log_ref = log.get("referenceCode") or log.get("logReferenceCode") or ""
            log_url = (
                f"https://www.geocaching.com/seek/log.aspx?LUID={log_ref}"
                if log_ref
                else ""
            )

            # Challenge Cache detection: name contains "Challenge" (case-insensitive)
            if "challenge" in cache_name.lower():
                results.append(
                    {
                        "log_date": log_date,
                        "gc_code": gc_code,
                        "cache_name": cache_name,
                        "cache_url": cache_url,
                        "log_url": log_url,
                    }
                )

        update_status(
            f"Checked {total_checked} Write Note logs"
            f" | {len(results)} challenge cache logs found so far..."
        )
        # Advance progress by a fixed increment per page (capped at 0.95 until done)
        update_progress(
            min(0.05 + page_num * 0.08, 0.95),
            f"{total_checked} logs checked",
        )

        if len(logs) < take:
            break

        skip += take

    update_progress(1.0, f"Done – {total_checked} logs scanned")
    update_status(
        f"API scan complete. Checked {total_checked} Write Note logs,"
        f" found {len(results)} on Challenge Caches.",
        ft.Colors.GREEN,
    )
    return results


# ---------------------------------------------------------------------------
# Private: HTML scraping fallback
# ---------------------------------------------------------------------------
def _scan_via_html(driver, update_status, update_progress):
    """Scrape the user's logs page when the API is unavailable."""
    results = []
    page_num = 0
    total_checked = 0

    update_status("Navigating to logs page (HTML mode)...")
    update_progress(0.0, "Loading logs page...")

    # Try common URLs for user logs
    logs_urls = [
        "https://www.geocaching.com/my/logs.aspx",
        "https://www.geocaching.com/account/settings/geocachelogs",
    ]

    landed = False
    for url in logs_urls:
        try:
            driver.get(url)
            time.sleep(3)
            current = driver.current_url
            # Validate we are on the genuine geocaching.com domain (not a redirect
            # to a malicious host that merely contains "geocaching.com" as a path
            # segment or as a prefix of the actual hostname).
            parsed = urlparse(current)
            netloc = parsed.netloc.lower()
            on_gc = netloc == "geocaching.com" or netloc.endswith(".geocaching.com")
            if on_gc and "signin" not in parsed.path:
                landed = True
                break
        except Exception:
            pass

    if not landed:
        update_status("Could not navigate to logs page.", ft.Colors.RED)
        return results

    while True:
        page_num += 1
        update_status(f"Processing HTML page {page_num}...")

        # Try multiple CSS selectors for log list items
        log_entries = []
        for selector in [
            ".log-entry",
            ".ActivityLogItem",
            ".userActivityLog .item",
            "tr.datarow",
            ".log-list li",
            ".cache-log",
            "[data-log-type]",
        ]:
            log_entries = driver.find_elements(By.CSS_SELECTOR, selector)
            if log_entries:
                break

        if not log_entries:
            update_status("No more log entries found.")
            break

        for entry in log_entries:
            try:
                entry_text = entry.text
                # Only process Write Note entries
                if "write note" not in entry_text.lower():
                    continue

                total_checked += 1

                # Find the cache link (href contains /geocache/GCxxxxx)
                links = entry.find_elements(By.TAG_NAME, "a")
                for link in links:
                    href = link.get_attribute("href") or ""
                    gc_match = re.search(
                        r"/geocache/(GC[A-Z0-9]+)", href, re.IGNORECASE
                    )
                    if gc_match:
                        gc_code = gc_match.group(1).upper()
                        cache_name = link.text.strip()

                        # Extract date from the entry
                        date_text = ""
                        for date_sel in [
                            "time",
                            ".date",
                            ".log-date",
                            "[class*='date']",
                            "[datetime]",
                        ]:
                            date_els = entry.find_elements(By.CSS_SELECTOR, date_sel)
                            if date_els:
                                date_text = (
                                    date_els[0].get_attribute("datetime")
                                    or date_els[0].text
                                )
                                break

                        if "challenge" in cache_name.lower():
                            results.append(
                                {
                                    "log_date": date_text,
                                    "gc_code": gc_code,
                                    "cache_name": cache_name,
                                    "cache_url": f"https://www.geocaching.com/geocache/{gc_code}",
                                    "log_url": "",
                                }
                            )
                        break
            except Exception:
                continue

        update_status(
            f"Page {page_num}: checked {total_checked} Write Notes,"
            f" {len(results)} challenge logs found."
        )
        update_progress(min(0.1 + page_num * 0.05, 0.95), f"Page {page_num}")

        # Navigate to next page
        next_found = False
        for next_sel in [
            "a[rel='next']",
            ".pagination .next a",
            "a.next",
            "[aria-label='Next page']",
            ".pager-next a",
        ]:
            try:
                next_btn = driver.find_element(By.CSS_SELECTOR, next_sel)
                next_btn.click()
                time.sleep(2)
                next_found = True
                break
            except Exception:
                pass

        if not next_found:
            break

    update_progress(1.0, f"Done – {total_checked} Write Notes scanned")
    update_status(
        f"HTML scan complete. Checked {total_checked} Write Note logs,"
        f" found {len(results)} on Challenge Caches.",
        ft.Colors.GREEN,
    )
    return results


# ---------------------------------------------------------------------------
# Export results to a CSV file in the user's home directory
# ---------------------------------------------------------------------------
def export_to_csv(results, status_callback=None):
    """
    Write scan results to a CSV file in the user's home directory.

    Returns:
        Tuple of (success: bool, message: str, csv_path: str or None)
    """
    def update_status(msg, color=None):
        if status_callback:
            status_callback(msg, color)
        print(msg)

    if not results:
        update_status("No results to export.", ft.Colors.YELLOW)
        return False, "No results to export.", None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    home_dir = Path.home()
    csv_path = home_dir / f"challenge_write_notes_{timestamp}.csv"

    fieldnames = ["log_date", "gc_code", "cache_name", "cache_url", "log_url"]

    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            # Sort by log_date descending, then gc_code
            sorted_results = sorted(
                results,
                key=lambda r: (r.get("log_date") or "", r.get("gc_code") or ""),
                reverse=True,
            )
            for row in sorted_results:
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        msg = f"Exported {len(results)} rows to {csv_path}"
        update_status(msg, ft.Colors.GREEN)
        return True, msg, str(csv_path)

    except Exception as exc:
        msg = f"CSV export failed: {exc}"
        update_status(msg, ft.Colors.RED)
        return False, msg, None

# functions.py – core logic for Manage-Geocache-Challenge-Logs
import csv
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urljoin

import flet as ft
from dotenv import load_dotenv
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


_APP_LOGGER = None
_APP_LOG_PATH = Path(__file__).parent.parent / "manage_geocache_challenge_logs.log"
_IN_PROGRESS_CSV_PATH = Path(__file__).parent.parent / "challenge_write_notes_in_progress.csv"

# Load local .env (if present) so runtime toggles can be controlled without shell exports.
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

def _get_stop_after_match_count():
    """
    Determine optional early-stop threshold for debugging.

    Env vars:
      GC_DEBUG_STOP_AFTER_MATCH_COUNT: integer (0 = no stop)
      GC_DEBUG_STOP_AFTER_FIRST_LOG: legacy bool fallback (true => 1, false => 0)
    """
    raw_count = (os.getenv("GC_DEBUG_STOP_AFTER_MATCH_COUNT", "") or "").strip()
    if raw_count:
        try:
            return max(0, int(raw_count))
        except Exception:
            _log_message(
                f"CONFIG | Invalid GC_DEBUG_STOP_AFTER_MATCH_COUNT='{raw_count}', defaulting to 0",
                "warning",
            )
            return 0

    # Backward-compatible fallback for previous boolean toggle.
    legacy = (os.getenv("GC_DEBUG_STOP_AFTER_FIRST_LOG", "") or "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return 1
    if legacy in {"0", "false", "no", "off"}:
        return 0

    return 0


_DEBUG_STOP_AFTER_MATCH_COUNT = _get_stop_after_match_count()


def _get_app_logger():
    global _APP_LOGGER
    if _APP_LOGGER is not None:
        return _APP_LOGGER

    logger = logging.getLogger("manage_geocache_challenge_logs")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.FileHandler(_APP_LOG_PATH, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    _APP_LOGGER = logger
    return logger


def get_log_file_path():
    return str(_APP_LOG_PATH)


def _log_message(message, level="info"):
    text = str(message)
    print(text, flush=True)

    logger = _get_app_logger()
    log_method = getattr(logger, level, logger.info)
    log_method(text)


def _log_exception(context, exc):
    _log_message(f"{context}: {exc}", "error")
    _get_app_logger().error(traceback.format_exc())


def _read_json_body(driver):
    """Read and parse JSON currently rendered in the page body."""
    body = driver.find_element(By.TAG_NAME, "body").text
    return json.loads(body)


def _extract_api_error(payload):
    """Return a readable API error message when payload is an error object."""
    if not isinstance(payload, dict):
        return ""

    parts = []
    for key in ("errorMessage", "statusMessage"):
        value = (payload.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)

    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            for key in ("message", "detail"):
                value = (item.get(key) or "").strip()
                if value and value not in parts:
                    parts.append(value)
            if parts:
                break

    return " | ".join(parts)


def _is_account_not_found_error(payload):
    """True when API payload indicates an account-not-found server error."""
    msg = _extract_api_error(payload).lower()
    return "account not found" in msg


def _is_target_challenge_cache(cache_name, *details):
    """Match only challenge caches that are mystery/question-mark type."""
    name = (cache_name or "").strip()
    if "challenge" not in name.lower():
        return False

    combined = " ".join((detail or "") for detail in details).lower()
    type_markers = (
        "mystery",
        "mystery cache",
        "puzzle",
        "puzzle cache",
        "unknown",
        "unknown cache",
        "question mark",
        "question-mark",
        "questionmark",
        "3.gif",
        "3.png",
        "3.svg",
        "cache type 3",
        "wpttypes/3",
        "wpttypes/sm/3",
    )
    return any(marker in combined for marker in type_markers)


def _extract_html_log_candidates(driver, require_write_note=True):
    """Extract likely Write Note log containers from the All Logs page."""
    script = """
const root = document.querySelector('main') || document.body;
const hrefRegex = /\/geocache\/(GC[A-Z0-9]+)|[?&]wp=(GC[A-Z0-9]+)|cache_details\.aspx\?wp=(GC[A-Z0-9]+)/i;
const requireWriteNote = Boolean(arguments[0]);
const links = Array.from(root.querySelectorAll('a[href]')).filter((a) => hrefRegex.test(a.getAttribute('href') || ''));
const seen = new Set();
const results = [];

const findLogHref = (startNode) => {
    let node = startNode;
    for (let depth = 0; depth < 10 && node; depth += 1) {
        const logEl = node.querySelector(
            'a[href*="/seek/log.aspx"], a[href*="log.aspx?LUID" i], a[href*="log.aspx?luid" i]'
        );
        if (logEl) {
            return logEl.href || logEl.getAttribute('href') || '';
        }
        node = node.parentElement;
    }
    return '';
};

const findVisitLogHref = (startNode) => {
    let node = startNode;
    for (let depth = 0; depth < 10 && node; depth += 1) {
        const anchors = Array.from(node.querySelectorAll('a[href]'));
        for (const a of anchors) {
            const text = (a.textContent || '').trim().toLowerCase();
            if (text.includes('visit log')) {
                return a.href || a.getAttribute('href') || '';
            }
        }
        node = node.parentElement;
    }
    return '';
};

for (const link of links) {
    const href = link.href || '';
    const hrefMatch = href.match(hrefRegex);
    const gcCode = ((hrefMatch && (hrefMatch[1] || hrefMatch[2] || hrefMatch[3])) || '').toUpperCase();
    if (!gcCode) {
        continue;
    }

    const semanticContainer = link.closest('article,li,tr,[data-cy*="log"],[data-cy*="activity"],[class*="log"],[class*="activity"]');
    if (semanticContainer) {
        const text = (semanticContainer.textContent || '').trim();
        if ((!requireWriteNote || /write note/i.test(text)) && text.length <= 16000) {
            const title = (link.textContent || '').trim();
            const key = `${gcCode}|${title}|${text.slice(0, 160)}`;
            if (!seen.has(key)) {
                seen.add(key);
                const metaParts = [];
                for (const img of Array.from(semanticContainer.querySelectorAll('img'))) {
                    metaParts.push([
                        img.getAttribute('alt') || '',
                        img.getAttribute('title') || '',
                        img.getAttribute('src') || '',
                        img.getAttribute('class') || ''
                    ].join(' '));
                }
                const dateEl = semanticContainer.querySelector('time,[datetime],.date,.log-date,[class*="date"]');
                const logHref = findLogHref(semanticContainer);
                const visitLogHref = findVisitLogHref(semanticContainer);
                results.push({
                    href,
                    gcCode,
                    title,
                    text,
                    metadata: metaParts.join(' | '),
                    date: dateEl ? ((dateEl.getAttribute('datetime') || dateEl.textContent || '').trim()) : '',
                    logHref,
                    visitLogHref,
                });
            }
            continue;
        }
    }

    let node = link;
    for (let depth = 0; depth < 12 && node; depth += 1) {
        node = node.parentElement;
        if (!node) break;

        const text = (node.textContent || '').trim();
        if (!text || (requireWriteNote && !/write note/i.test(text)) || text.length > 16000) {
            continue;
        }

        const title = (link.textContent || '').trim();
        const key = `${gcCode}|${title}|${text.slice(0, 200)}`;
        if (seen.has(key)) {
            break;
        }
        seen.add(key);

        const metaParts = [];
        for (const img of Array.from(node.querySelectorAll('img'))) {
            metaParts.push([
                img.getAttribute('alt') || '',
                img.getAttribute('title') || '',
                img.getAttribute('src') || '',
                img.getAttribute('class') || ''
            ].join(' '));
        }
        for (const el of Array.from(node.querySelectorAll('[title],[aria-label],[data-cy]'))) {
            metaParts.push([
                el.getAttribute('title') || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('data-cy') || '',
                el.getAttribute('class') || ''
            ].join(' '));
        }

        const dateEl = node.querySelector('time,[datetime],.date,.log-date,[class*="date"]');
        const logHref = findLogHref(node);
        const visitLogHref = findVisitLogHref(node);
        results.push({
            href,
            gcCode,
            title,
            text,
            metadata: metaParts.join(' | '),
            date: dateEl ? ((dateEl.getAttribute('datetime') || dateEl.textContent || '').trim()) : '',
            logHref,
            visitLogHref,
        });
        break;
    }
}

if (results.length === 0) {
    const blocks = Array.from(root.querySelectorAll('article,li,tr,div')).slice(0, 2000);
    for (const block of blocks) {
        const text = (block.textContent || '').trim();
        if (requireWriteNote && !/write note/i.test(text)) continue;
        const codeMatch = text.match(/\bGC[A-Z0-9]{4,}\b/i);
        if (!codeMatch) continue;
        const gcCode = codeMatch[0].toUpperCase();
        const challengeMatch = text.match(/challenge[^\n\r]*/i);
        const title = (challengeMatch ? challengeMatch[0] : `Cache ${gcCode}`).trim();
        const key = `${gcCode}|${title}|${text.slice(0, 120)}`;
        if (seen.has(key)) continue;
        seen.add(key);
        results.push({
            href: `https://www.geocaching.com/geocache/${gcCode}`,
            gcCode,
            title,
            text,
            metadata: '',
            date: '',
            logHref: ''
            ,visitLogHref: ''
        });
    }
}

return results;
"""
    try:
        candidates = driver.execute_script(script, require_write_note) or []
    except Exception:
        return []

    if not isinstance(candidates, list):
        return []

    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _extract_filtered_challenge_candidates(driver):
        """Extract challenge unknown-cache candidates from filtered logs page."""
        script = """
const root = document.querySelector('main') || document.body;
const gcRegex = /\/geocache\/(GC[A-Z0-9]+)/i;
const imageLinks = Array.from(root.querySelectorAll('a.ImageLink[href*="/geocache/"]'));
const results = [];
const seen = new Set();

const findLogHref = (startNode) => {
    let node = startNode;
    for (let depth = 0; depth < 10 && node; depth += 1) {
        const logEl = node.querySelector(
            'a[href*="/seek/log.aspx"], a[href*="log.aspx?LUID" i], a[href*="log.aspx?luid" i]'
        );
        if (logEl) {
            return logEl.href || logEl.getAttribute('href') || '';
        }
        node = node.parentElement;
    }
    return '';
};

const findVisitLogHref = (startNode) => {
    let node = startNode;
    for (let depth = 0; depth < 10 && node; depth += 1) {
        const anchors = Array.from(node.querySelectorAll('a[href]'));
        for (const a of anchors) {
            const text = (a.textContent || '').trim().toLowerCase();
            if (text.includes('visit log')) {
                return a.href || a.getAttribute('href') || '';
            }
        }
        node = node.parentElement;
    }
    return '';
};

for (const imgLink of imageLinks) {
    const href = imgLink.getAttribute('href') || '';
    const fullHref = imgLink.href || href;
    const gcMatch = fullHref.match(gcRegex);
    if (!gcMatch) continue;
    const gcCode = gcMatch[1].toUpperCase();

    const img = imgLink.querySelector('img');
    if (!img) continue;
    const imgTitle = (img.getAttribute('title') || '').toLowerCase();
    const imgSrc = (img.getAttribute('src') || '').toLowerCase();
    const isUnknownType = imgTitle.includes('unknown') || imgSrc.includes('/wpttypes/sm/8.') || imgSrc.includes('/wpttypes/8.');
    if (!isUnknownType) continue;

    const container = imgLink.closest('tr,li,article,div') || imgLink.parentElement;
    if (!container) continue;

    const titleLinks = Array.from(container.querySelectorAll('a[href*="/geocache/"]')).filter((a) => !a.classList.contains('ImageLink'));
    let titleLink = null;
    for (const a of titleLinks) {
        const tHref = a.href || a.getAttribute('href') || '';
        const tMatch = tHref.match(gcRegex);
        if (!tMatch) continue;
        if (tMatch[1].toUpperCase() !== gcCode) continue;
        const text = (a.textContent || '').trim();
        if (text) {
            titleLink = a;
            break;
        }
    }
    if (!titleLink) continue;

    const title = (titleLink.textContent || '').trim();
    if (!title || !title.toLowerCase().includes('challenge')) continue;

    const key = `${gcCode}|${title}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const text = (container.textContent || '').trim();
    const dateEl = container.querySelector('time,[datetime],.date,.log-date,[class*="date"]');
    const date = dateEl ? ((dateEl.getAttribute('datetime') || dateEl.textContent || '').trim()) : '';
    const logHref = findLogHref(container);
    const visitLogHref = findVisitLogHref(container);

    results.push({
        href: titleLink.href || fullHref,
        gcCode,
        title,
        text,
        metadata: `${imgTitle} | ${imgSrc}`,
        date,
        logHref,
        visitLogHref,
    });
}

return results;
"""
        try:
                candidates = driver.execute_script(script) or []
        except Exception:
                return []

        if not isinstance(candidates, list):
                return []

        return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _normalize_geocaching_log_url(url):
    """Return a valid geocaching write-note log URL or empty string."""
    raw = (url or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()

        on_geocaching = host == "www.geocaching.com" or host == "geocaching.com" or host.endswith(".geocaching.com")
        if not on_geocaching:
            return ""

        # Prefer direct log endpoint; accept different query key variants.
        if "/seek/log.aspx" in path:
            return raw

        # Accept alternative casing/shape for log endpoint and normalize domain.
        if "/log.aspx" in path:
            suffix = f"?{parsed.query}" if parsed.query else ""
            return f"https://www.geocaching.com{parsed.path}{suffix}"
    except Exception:
        return ""

    return ""


def _write_in_progress_csv(rows):
    """Write current scan rows to an in-progress CSV in project root."""
    fieldnames = [
        "log_date",
        "gc_code",
        "cache_name",
        "cache_url",
        "log_url",
        "checker_status",
        "checker_example_log",
    ]
    try:
        with open(_IN_PROGRESS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
    except Exception as exc:
        _log_message(f"SCAN | Could not write in-progress CSV: {exc}", "warning")


def _extract_visit_log_links_by_gc_code(driver):
        """Build {GC_CODE: geocaching log URL} map from current logs page."""
        script = r"""
const root = document.querySelector('main') || document.body;
const gcRegex = /\/geocache\/(GC[A-Z0-9]+)|[?&]wp=(GC[A-Z0-9]+)/i;
const visitAnchors = Array.from(root.querySelectorAll('a[href]')).filter((a) => {
    const txt = (a.textContent || '').trim().toLowerCase();
    const href = (a.getAttribute('href') || '').toLowerCase();
    return txt.includes('visit log') || href.includes('/seek/log.aspx') || href.includes('log.aspx?luid');
});

const out = {};
for (const a of visitAnchors) {
    const logHref = a.href || a.getAttribute('href') || '';
    if (!logHref) continue;

    let node = a;
    let gcCode = '';
    for (let depth = 0; depth < 14 && node; depth += 1) {
        const geocacheLink = node.querySelector('a[href*="/geocache/"], a[href*="wp=GC" i]');
        if (geocacheLink) {
            const targetHref = geocacheLink.href || geocacheLink.getAttribute('href') || '';
            const match = targetHref.match(gcRegex);
            gcCode = ((match && (match[1] || match[2])) || '').toUpperCase();
            if (gcCode) break;
        }
        node = node.parentElement;
    }

    if (!gcCode) continue;
    if (!(gcCode in out)) {
        out[gcCode] = logHref;
    }
}

return out;
"""
        try:
                value = driver.execute_script(script) or {}
        except Exception:
                return {}

        if not isinstance(value, dict):
                return {}

        cleaned = {}
        for gc_code, url in value.items():
                code = (gc_code or "").strip().upper()
                log_url = _normalize_geocaching_log_url(url)
                if code and log_url:
                        cleaned[code] = log_url
        return cleaned


def _apply_write_note_filter(driver, update_status):
    """Best-effort: apply the All Logs filter so only Write Note entries are shown."""
    update_status("Attempting to apply 'Write Note' filter...")

    # Fast path: click the known Write Note link from the log type filter UI.
    exact_link_selectors = [
        "a[href='logs.aspx?s=1&lt=4']",
        "a[href='logs.aspx?s=1&amp;lt=4']",
        "a[href*='logs.aspx?s=1'][href*='lt=4']",
    ]
    for css in exact_link_selectors:
        try:
            link = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, css))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
            link.click()
            time.sleep(2)
            _log_message(f"HTML_FILTER | Applied Write Note filter via exact selector: {css}")
            update_status("Write Note filter applied.")
            return True
        except Exception:
            continue

    # Direct URL fallback for the same filter.
    try:
        current = driver.current_url or "https://www.geocaching.com/my/logs.aspx"
        filtered_url = urljoin(current, "logs.aspx?s=1&lt=4")
        driver.get(filtered_url)
        time.sleep(2)
        _log_message(f"HTML_FILTER | Applied Write Note filter via direct URL: {filtered_url}")
        update_status("Write Note filter applied via direct URL.")
        return True
    except Exception:
        pass

    def click_js_text(candidates):
        script = """
    const terms = arguments[0].map((t) => String(t || '').toLowerCase());
    const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],label,span,div'));
    for (const node of nodes) {
      const text = (node.textContent || '').trim().toLowerCase();
      if (!text || text.length > 120) continue;
      if (!terms.some((term) => text.includes(term))) continue;

      const clickable = node.closest('button,a,[role="button"],label') || node;
      try {
        clickable.scrollIntoView({block: 'center'});
        clickable.click();
        return true;
      } catch (e) {
        continue;
      }
    }
    return false;
    """
        try:
            return bool(driver.execute_script(script, list(candidates)))
        except Exception:
            return False

    # Open filter controls first.
    opened = False
    for css in [
        "button[aria-label*='Filter']",
        "button[title*='Filter']",
        "a[aria-label*='Filter']",
        "[data-cy*='filter'] button",
        "[data-cy*='filter']",
    ]:
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, css))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            btn.click()
            opened = True
            _log_message(f"HTML_FILTER | Opened filter using selector: {css}")
            break
        except Exception:
            continue

    if not opened:
        opened = click_js_text(["filter", "log type", "type"])
        if opened:
            _log_message("HTML_FILTER | Opened filter via text match")

    time.sleep(1)

    # Select Write Note option.
    selected = False
    for css in [
        "input[type='checkbox'][value='4']",
        "input[type='radio'][value='4']",
        "[data-cy*='write'] input[type='checkbox']",
        "[data-cy*='write'] input[type='radio']",
    ]:
        try:
            option = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, css))
            )
            if not option.is_selected():
                driver.execute_script("arguments[0].click();", option)
            selected = True
            _log_message(f"HTML_FILTER | Selected Write Note input via selector: {css}")
            break
        except Exception:
            continue

    if not selected:
        selected = click_js_text(["write note"])
        if selected:
            _log_message("HTML_FILTER | Selected Write Note via text match")

    time.sleep(1)

    # Apply/close filter panel if needed.
    applied = False
    for css in [
        "button[type='submit']",
        "button[aria-label*='Apply']",
        "button[title*='Apply']",
        "button[aria-label*='Done']",
        "button[title*='Done']",
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, css)
            txt = (btn.text or "").strip().lower()
            if txt and txt not in {"apply", "done", "ok", "update", "save"}:
                continue
            driver.execute_script("arguments[0].click();", btn)
            applied = True
            _log_message(f"HTML_FILTER | Confirmed filter via selector: {css}")
            break
        except Exception:
            continue

    if not applied:
        if click_js_text(["apply", "done", "ok", "update", "save"]):
            applied = True
            _log_message("HTML_FILTER | Confirmed filter via text match")

    time.sleep(2)

    if selected:
        update_status("Write Note filter applied (best effort).")
        return True

    update_status(
        "Could not confidently apply Write Note filter; continuing with full page scan.",
        ft.Colors.YELLOW,
    )
    return False


def _find_project_gc_checker_href(driver):
    """Return checker href from the current cache page when present."""
    script = """
const selectors = [
  'a img[title*="Project-GC Challenge checker"]',
  'a img[alt*="PGC Checker"]',
  'a img[src*="project-gc.com/Images/Checker"]',
  'a img[src*="project-gc.com/images/checker"]',
];

for (const selector of selectors) {
  const img = document.querySelector(selector);
  if (!img) continue;
  const anchor = img.closest('a');
  if (!anchor) continue;
  const href = anchor.getAttribute('href') || '';
  if (href) return href;
}
return '';
"""
    try:
        href = (driver.execute_script(script) or "").strip()
    except Exception:
        return ""

    if not href:
        return ""

    if href.startswith("//"):
        return f"https:{href}"

    return urljoin(driver.current_url, href)


def _authenticate_project_gc_if_needed(driver, update_status):
    """Attempt Project-GC login using the same credentials when prompted."""
    try:
        current = (driver.current_url or "").lower()
    except Exception:
        return

    _log_message(f"CHECKER | Auth flow current URL: {current}")

    def _is_project_gc_url(url):
        try:
            host = (urlparse(url or "").netloc or "").lower()
            return host == "project-gc.com" or host.endswith(".project-gc.com")
        except Exception:
            return False

    def _click_first_matching(selectors, log_label):
        for selector in selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, selector)
                if not matches:
                    continue
                _log_message(
                    f"CHECKER | Found {len(matches)} candidate(s) for {log_label} using {selector}"
                )
                target = matches[0]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
                driver.execute_script("arguments[0].click();", target)
                _log_message(f"CHECKER | Clicked {log_label} using selector: {selector}")
                return True
            except Exception:
                continue
        return False

    def _handle_oauth_consent_if_present():
        """Click Accept/Authorize on geocaching OAuth consent pages."""
        try:
            current_url = (driver.current_url or "").lower()
            page_text = (driver.page_source or "").lower()
        except Exception:
            return False

        looks_like_consent = (
            "/oauth/authorize" in current_url
            or "complete setup" in page_text
            or "approval_prompt" in current_url
        )
        if not looks_like_consent:
            return False

        _log_message("CHECKER | OAuth consent page detected; looking for Accept button")

        # Prefer explicit affirmative actions first to avoid clicking a deny button.
        clicked = _click_first_matching(
            [
                "input#uxAllowAccessButton",
                "button#uxAllowAccessButton",
                "input[name='uxAllowAccessButton']",
                "input[value='Agree']",
                "button[value='Agree']",
                "button#ctl00_ContentBody_btnYes",
                "input#ctl00_ContentBody_btnYes",
                "button[name*='accept' i]",
                "input[name*='accept' i]",
                "button[name*='allow' i]",
                "input[name*='allow' i]",
                "button[value*='accept' i]",
                "input[value*='accept' i]",
                "button[value*='agree' i]",
                "input[value*='agree' i]",
                "button[aria-label*='accept' i]",
                "button[aria-label*='agree' i]",
            ],
            "OAuth Accept",
        )
        if not clicked:
            try:
                clicked = bool(driver.execute_script(
                    """
const terms = ['agree', 'accept', 'authorize', 'allow', 'yes', 'complete setup'];
const nodes = Array.from(document.querySelectorAll('button,input[type="submit"],input[type="button"],a.btn'));
for (const node of nodes) {
  const text = ((node.innerText || node.value || node.getAttribute('aria-label') || '') + '').trim().toLowerCase();
  if (!text) continue;
  if (!terms.some((term) => text.includes(term))) continue;
  node.scrollIntoView({block: 'center'});
  node.click();
  return true;
}
return false;
"""
                ))
                if clicked:
                    _log_message("CHECKER | Clicked OAuth Accept via text fallback")
            except Exception:
                clicked = False

        if not clicked:
            _log_message("CHECKER | OAuth consent page found but Accept button was not clicked", "warning")
            return False

        time.sleep(2)
        try:
            # Redirect back to Project-GC can take several seconds after consent.
            WebDriverWait(driver, 30).until(
                lambda d: _is_project_gc_url(d.current_url or "")
            )
            _log_message(f"CHECKER | OAuth consent completed; redirected to {driver.current_url}")
            return True
        except Exception:
            _log_message(
                f"CHECKER | Accept clicked but no Project-GC redirect yet (current={driver.current_url})",
                "warning",
            )
            return True

    # First preference: explicit Project-GC Authenticate button (/oauth2.php).
    clicked_oauth = _click_first_matching(
        [
            "a.btn.btn-info.btn-lg[href*='/oauth2.php']",
            "a[href='/oauth2.php']",
            "a[href*='/oauth2.php']",
        ],
        "Project-GC OAuth authenticate",
    )
    if clicked_oauth:
        time.sleep(1.5)
        try:
            current = (driver.current_url or "").lower()
        except Exception:
            current = ""
        _log_message(f"CHECKER | URL after OAuth authenticate click: {current}")
        _handle_oauth_consent_if_present()
        try:
            current = (driver.current_url or "").lower()
        except Exception:
            current = ""

    needs_login = "/user/login" in current or "/account/signin" in current
    if not needs_login:
        try:
            clicked_login = _click_first_matching(
                [
                    "a[href*='/User/Login']",
                    "a.btn.btn-info[href*='Login']",
                ],
                "Project-GC login link",
            )
            if clicked_login:
                time.sleep(1)
                current = (driver.current_url or "").lower()
                _log_message(f"CHECKER | URL after login-link click: {current}")

                # Some pages show /User/Login first, then require a second
                # click on Authenticate (/oauth2.php).
                clicked_oauth = _click_first_matching(
                    [
                        "a.btn.btn-info.btn-lg[href*='/oauth2.php']",
                        "a[href='/oauth2.php']",
                        "a[href*='/oauth2.php']",
                    ],
                    "Project-GC OAuth authenticate",
                )
                if clicked_oauth:
                    time.sleep(1.5)
                    current = (driver.current_url or "").lower()
                    _log_message(f"CHECKER | URL after second OAuth click: {current}")
                    _handle_oauth_consent_if_present()
                    current = (driver.current_url or "").lower()

                needs_login = "/user/login" in current or "/account/signin" in current
        except Exception:
            needs_login = False

    if not needs_login:
        _log_message("CHECKER | No interactive login required for checker page")
        # Verify the resulting page is actually authenticated for Project-GC.
        if not _is_project_gc_url(driver.current_url or ""):
            _log_message(
                f"CHECKER | Not on Project-GC host after auth flow: {driver.current_url}",
                "warning",
            )
            return

        try:
            still_has_login = bool(
                driver.find_elements(By.CSS_SELECTOR, "a[href*='/User/Login'], a[href*='/oauth2.php']")
            )
        except Exception:
            still_has_login = False

        if still_has_login:
            _log_message("CHECKER | Project-GC page still shows login/auth links", "warning")
        return

    username = (getattr(driver, "_gc_username", "") or "").strip()
    password = getattr(driver, "_gc_password", "") or ""
    if not username or not password:
        update_status("Project-GC login requested but credentials are unavailable.", ft.Colors.YELLOW)
        _log_message("CHECKER | Missing credentials for Project-GC login", "warning")
        return

    update_status("Project-GC authentication required; attempting login...")

    user_selectors = [
        "#UsernameOrEmail",
        "input[id='UsernameOrEmail']",
        "input[name='Username']",
        "input[name='Email']",
        "input[name*='user' i]",
        "input[name*='email' i]",
        "input[type='text']",
        "input[type='email']",
    ]
    pass_selectors = [
        "#Password",
        "input[id='Password']",
        "input[name='Password']",
        "input[name*='pass' i]",
        "input[type='password']",
    ]

    user_field = None
    for selector in user_selectors:
        try:
            user_field = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            if user_field:
                break
        except Exception:
            continue

    pass_field = None
    for selector in pass_selectors:
        try:
            pass_field = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            if pass_field:
                break
        except Exception:
            continue

    if not user_field or not pass_field:
        _log_message("CHECKER | Could not locate Project-GC login form fields", "warning")
        update_status("Project-GC login form not detected.", ft.Colors.YELLOW)
        return

    user_field.clear()
    user_field.send_keys(username)
    pass_field.clear()
    pass_field.send_keys(password)

    submitted = False
    for selector in [
        "#SignIn",
        "button#SignIn",
        "button[type='submit']",
        "input[type='submit']",
        "button.btn-primary",
        "button.btn-info",
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].click();", btn)
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        try:
            pass_field.submit()
            submitted = True
        except Exception:
            pass

    if not submitted:
        _log_message("CHECKER | Failed to submit Project-GC login form", "warning")
        return

    try:
        WebDriverWait(driver, 10).until(
            lambda d: "/user/login" not in (d.current_url or "").lower()
            and "/account/signin" not in (d.current_url or "").lower()
        )
        _log_message("CHECKER | Project-GC authentication completed")
    except Exception:
        _log_message("CHECKER | Project-GC authentication may not have completed", "warning")


def _run_project_gc_checker_if_available(driver, cache_name):
    """Run Project-GC checker and detect success; retry once on max execution time."""
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""

    if "project-gc.com" not in current_url:
        _log_message("CHECKER | Skipping run-checker step (not on Project-GC host)", "warning")
        return False

    def _has_success_marker():
        # Use strict/visible checks to avoid false positives from hidden template nodes.
        try:
            return bool(driver.execute_script(
                """
const isVisible = (el) => !!el && !!(el.offsetParent || el.getClientRects().length);

const successImg = document.querySelector("img[title*='Success'][src*='check48']");
if (isVisible(successImg)) return true;

const fulfill = document.querySelector("p.cc_fulfillText");
if (isVisible(fulfill) && /fulfills challenge/i.test((fulfill.textContent || '').trim())) {
  return true;
}

return false;
"""
            ))
        except Exception:
            return False

    def _has_failure_marker():
        # Detect explicit negative result from Project-GC checker.
        try:
            return bool(driver.execute_script(
                """
const isVisible = (el) => !!el && !!(el.offsetParent || el.getClientRects().length);

const cancelImg = document.querySelector("img[alt='Cancel'][src*='cancel48']");
if (isVisible(cancelImg)) return true;

const unfulfilledText = document.querySelector("p.cc_fulfillText");
if (isVisible(unfulfilledText) && /does\s+not\s+fulfill\s+challenge/i.test((unfulfilledText.textContent || '').trim())) {
  return true;
}

const unfulfilledName = document.querySelector("#cc_unfulfilled_profileName, #cc_unfulfilled_cacheName");
if (isVisible(unfulfilledName)) return true;

return false;
"""
            ))
        except Exception:
            return False

    def _run_button_present():
        selectors = [
            "button#runChecker",
            "button[id='runChecker'][type='submit']",
            "button.btn.btn-primary#runChecker",
            "button[type='submit']#runChecker",
        ]
        for selector in selectors:
            try:
                for btn in driver.find_elements(By.CSS_SELECTOR, selector):
                    if btn.is_displayed():
                        return True
            except Exception:
                continue
        return False

    def _max_execution_reached():
        try:
            text = (driver.page_source or "").lower()
        except Exception:
            return False
        return "max execution time reached" in text

    def _click_run_checker():
        for selector in [
            "button#runChecker",
            "button[id='runChecker'][type='submit']",
            "button.btn.btn-primary#runChecker",
            "button[type='submit']#runChecker",
        ]:
            try:
                btn = WebDriverWait(driver, 6).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                driver.execute_script("arguments[0].click();", btn)
                _log_message(f"CHECKER | Clicked Run checker using selector: {selector}")
                return True
            except Exception:
                continue
        return False

    run_button_available = _run_button_present()
    if run_button_available:
        _log_message(f"CHECKER | Run checker button is present for {cache_name}; executing checker")
    elif _has_success_marker():
        _log_message(f"CHECKER | Success already present for {cache_name}; no run needed")
        return True
    elif _has_failure_marker():
        _log_message(f"CHECKER | Failure already present for {cache_name}; no run needed")
        return False
    else:
        _log_message("CHECKER | Run checker button not present and no success marker visible", "warning")
        return False

    for attempt in range(1, 3):
        clicked = _click_run_checker()
        if not clicked:
            _log_message("CHECKER | Run checker button not found/clickable", "warning")
            return False

        _log_message(f"CHECKER | Waiting for checker result (attempt {attempt}/2)")
        deadline = time.time() + 95
        last_heartbeat = 0

        while time.time() < deadline:
            if _has_success_marker():
                _log_message(f"CHECKER | Challenge check success detected for {cache_name}")
                return True

            if _has_failure_marker():
                _log_message(f"CHECKER | Challenge check failure detected for {cache_name}")
                return False

            if _max_execution_reached():
                _log_message(
                    f"CHECKER | Max execution time reached on attempt {attempt}/2",
                    "warning",
                )
                break

            now = time.time()
            if now - last_heartbeat >= 10:
                _log_message(
                    f"CHECKER | Still waiting for result (attempt {attempt}/2)",
                )
                last_heartbeat = now

            time.sleep(1.5)

        if attempt == 1:
            _log_message("CHECKER | Retrying Run checker once after incomplete/timeout result")

    _log_message("CHECKER | Checker run did not reach success state after retries", "warning")
    return False


def _extract_project_gc_example_log(driver):
    """Read Project-GC's generated example log text for copy/paste export."""
    selectors = [
        "textarea#cc_ExampleLog",
        "textarea[id='cc_ExampleLog']",
        "textarea[data-prefix]",
    ]

    def _normalize_checker_text(value):
        text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = text.strip()
        # Keep paragraph breaks but avoid huge blank runs.
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _read_textarea_value(node):
        candidates = [
            node.get_attribute("value"),
            node.get_attribute("textContent"),
            node.get_attribute("innerText"),
            node.text,
        ]
        for candidate in candidates:
            text = _normalize_checker_text(candidate)
            if text:
                return text
        return ""

    for selector in selectors:
        try:
            nodes = driver.find_elements(By.CSS_SELECTOR, selector)
            if not nodes:
                continue
            text = _read_textarea_value(nodes[0])
            if text:
                _log_message(
                    f"CHECKER | Captured example log text ({len(text)} chars) via {selector}"
                )
                return text
        except Exception:
            continue

    # Some checker pages populate the textarea asynchronously; wait briefly.
    end_time = time.time() + 20
    while time.time() < end_time:
        for selector in selectors:
            try:
                nodes = driver.find_elements(By.CSS_SELECTOR, selector)
                if not nodes:
                    continue
                text = _read_textarea_value(nodes[0])
                if text:
                    _log_message(
                        f"CHECKER | Captured example log text after wait ({len(text)} chars) via {selector}"
                    )
                    return text
            except Exception:
                continue

        try:
            text = driver.execute_script(
                """
const el = document.querySelector('textarea#cc_ExampleLog, textarea[id="cc_ExampleLog"], textarea[data-prefix]');
if (!el) return '';
return (el.value || el.textContent || el.innerText || '').trim();
"""
            )
            text = _normalize_checker_text(text)
            if text:
                _log_message(
                    f"CHECKER | Captured example log text via JS fallback ({len(text)} chars)"
                )
                return text
        except Exception:
            pass

        time.sleep(1)

    _log_message("CHECKER | Example log textarea found no content", "warning")

    return ""


def _open_checker_for_cache(driver, cache_url, cache_name, update_status, keep_checker_tab_open=False):
    """Open checker page and return checker URL plus generated Project-GC example log text."""
    checker_url = ""
    checker_example_log = ""
    checker_status = "Failed"
    source_handle = None
    temp_opened = False

    def _safe_get(url, label, retries=3):
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                driver.get(url)
                return True
            except Exception as exc:
                last_exc = exc
                _log_message(
                    f"CHECKER | {label} attempt {attempt}/{retries} failed: {exc}",
                    "warning",
                )
                time.sleep(min(attempt * 1.5, 5.0))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        source_handle = driver.current_window_handle
    except Exception:
        source_handle = None

    try:
        driver.switch_to.new_window("tab")
        temp_opened = True
        _safe_get(cache_url, "Open cache page")
        time.sleep(1.5)

        checker_href = _find_project_gc_checker_href(driver)
        if not checker_href:
            _log_message(f"CHECKER | No checker link found for {cache_name} ({cache_url})")
            return "", "", checker_status

        if checker_href.startswith("http://project-gc.com"):
            checker_href = "https://" + checker_href[len("http://") :]
        elif checker_href.startswith("http://www.project-gc.com"):
            checker_href = "https://" + checker_href[len("http://") :]

        _log_message(f"CHECKER | Opening checker for {cache_name}: {checker_href}")
        _safe_get(checker_href, "Open checker page")
        time.sleep(1.5)

        _authenticate_project_gc_if_needed(driver, update_status)
        run_success = _run_project_gc_checker_if_available(driver, cache_name)
        checker_status = "SUCCESS!" if run_success else "Failed"
        checker_example_log = _extract_project_gc_example_log(driver)
        checker_url = driver.current_url or checker_href
        return checker_url, checker_example_log, checker_status
    except Exception as exc:
        _log_exception(f"CHECKER | Failed while opening checker for {cache_name}", exc)
        return "", "", checker_status
    finally:
        try:
            if temp_opened and not keep_checker_tab_open:
                driver.close()
        except Exception:
            pass
        if source_handle and not keep_checker_tab_open:
            try:
                driver.switch_to.window(source_handle)
            except Exception:
                pass

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
        suffix = f" (progress={progress:.2f})" if progress is not None else ""
        _log_message(f"STARTUP | {msg}{suffix}")

    update_loading("Setting up Firefox driver...", 0.05)
    _log_message(f"STARTUP | Log file: {get_log_file_path()}")

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
        _log_message(f"STARTUP | Using Firefox profile: {profile_path}")
    else:
        _log_message("STARTUP | No Firefox profile supplied; using a fresh session")

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
    _log_message("STARTUP | Firefox launched successfully")

    driver.set_window_size(1280, 900)

    update_loading("Checking geocaching session...", 0.20)

    # Visit an auth-required page first. If this does not redirect to the
    # sign-in page, we can reuse the existing browser session.
    driver.get("https://www.geocaching.com/my/logs.aspx")
    time.sleep(2)

    _dismiss_cookie_banner(driver)

    if "signin" in driver.current_url:
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
    else:
        update_loading("Existing geocaching session detected.", 0.35)

    update_loading("Session ready. Preparing HTML-first scan mode...", 0.60)

    driver._gc_user_data = {}
    driver._gc_profile_api_error = "startup skipped"
    driver._gc_active_user = username or "existing session"
    driver._gc_username = username or ""
    driver._gc_password = password or ""
    _log_message(f"STARTUP | Active user label set to: {driver._gc_active_user}")
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
        list of dicts with keys: log_date, gc_code, cache_name, cache_url, log_url,
        checker_status, checker_example_log
    """
    results = []

    def update_status(msg, color=None):
        if status_callback:
            status_callback(msg, color)
        _log_message(f"SCAN | {msg}")

    def update_progress(value, label=""):
        if progress_callback:
            progress_callback(value, label)
        if label:
            _log_message(f"SCAN_PROGRESS | {value:.2f} | {label}")
        else:
            _log_message(f"SCAN_PROGRESS | {value:.2f}")

    update_status("Starting HTML-first scan...")
    # Initialize/clear the autosave CSV so progress is durable even if run aborts.
    _write_in_progress_csv([])
    user_data = getattr(driver, "_gc_user_data", {})
    user_ref_code = user_data.get("referenceCode", "")
    profile_api_error = getattr(driver, "_gc_profile_api_error", "")

    html_results = _scan_via_html(driver, update_status, update_progress)
    if html_results:
        _log_message(f"SCAN | HTML-first scan returned {len(html_results)} results")
        return html_results

    if profile_api_error and not user_ref_code:
        update_status(
            f"Profile API unavailable ({profile_api_error}). Staying in HTML mode.",
            ft.Colors.YELLOW,
        )
        return html_results

    if not user_ref_code:
        try:
            driver.get("https://www.geocaching.com/api/proxy/web/v1/users/me")
            time.sleep(1)
            user_data = _read_json_body(driver)
            api_error = _extract_api_error(user_data)
            if api_error:
                if _is_account_not_found_error(user_data):
                    update_status(
                        "Profile API returned 'account not found'. "
                        "Try restarting without a Firefox profile and signing in again.",
                        ft.Colors.RED,
                    )
                update_status(
                    f"Profile API returned an error ({api_error}).",
                    ft.Colors.YELLOW,
                )
            else:
                user_ref_code = user_data.get("referenceCode", "")
                driver._gc_user_data = user_data
        except Exception as exc:
            update_status(f"Could not retrieve user profile: {exc}", ft.Colors.RED)

    if user_ref_code:
        _log_message(f"SCAN | Attempting API fallback with user ref code {user_ref_code}")
        try:
            results = _scan_via_api(
                driver,
                user_ref_code,
                update_status,
                update_progress,
            )
        except RuntimeError as exc:
            update_status(str(exc), ft.Colors.YELLOW)
            update_status("HTML-first scan already completed; API fallback skipped.", ft.Colors.YELLOW)
            results = html_results
    else:
        results = html_results

    _log_message(f"SCAN | Final result count: {len(results)}")
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
        _log_message(f"API_SCAN | Page {page_num} | GET {api_url}")

        try:
            driver.get(api_url)
            time.sleep(1)
            logs = _read_json_body(driver)
        except Exception as exc:
            if page_num == 1:
                raise RuntimeError(f"Could not read logs API response: {exc}")
            update_status(f"API error on page {page_num}: {exc}", ft.Colors.YELLOW)
            break

        if isinstance(logs, dict):
            api_error = _extract_api_error(logs)
            if api_error:
                raise RuntimeError(f"Logs API returned an error: {api_error}")
            if page_num == 1:
                raise RuntimeError("Logs API returned an unexpected payload.")
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
            cache_type = (
                str(log.get("geocacheType") or "")
                + " "
                + str(log.get("geocacheTypeName") or "")
                + " "
                + str((log.get("geocache") or {}).get("type") or "")
                + " "
                + str((log.get("geocache") or {}).get("typeName") or "")
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

            if _is_target_challenge_cache(cache_name, cache_type, json.dumps(log)):
                results.append(
                    {
                        "log_date": log_date,
                        "gc_code": gc_code,
                        "cache_name": cache_name,
                        "cache_url": cache_url,
                        "log_url": log_url,
                        "checker_status": "Failed",
                        "checker_example_log": "",
                    }
                )
                _write_in_progress_csv(results)

        update_status(
            f"Checked {total_checked} Write Note logs"
            f" | {len(results)} mystery challenge logs found so far..."
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
        f" found {len(results)} on mystery Challenge Caches.",
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
    visited_urls = set()
    stop_after_matches = _DEBUG_STOP_AFTER_MATCH_COUNT

    if stop_after_matches > 0:
        _log_message(
            "HTML_SCAN | Debug stop mode enabled: "
            f"will stop after {stop_after_matches} processed match(es) and keep browser tab open"
        )

    update_status("Navigating to All Logs page (HTML mode)...")
    update_progress(0.0, "Loading logs page...")

    # Try common URLs for user logs
    logs_urls = [
        "https://www.geocaching.com/my/logs.aspx",
        "https://www.geocaching.com/account/settings/geocachelogs",
    ]

    landed = False
    for url in logs_urls:
        try:
            _log_message(f"HTML_SCAN | Trying logs URL: {url}")
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
                _log_message(f"HTML_SCAN | Landed on logs page: {current}")
                break
        except Exception:
            pass

    if not landed:
        update_status("Could not navigate to logs page.", ft.Colors.RED)
        return results

    update_progress(0.03, "Applying Write Note filter")
    _apply_write_note_filter(driver, update_status)

    while True:
        page_num += 1
        current_url = driver.current_url
        if current_url in visited_urls:
            update_status("Reached a page that was already scanned. Stopping.")
            break
        visited_urls.add(current_url)
        _log_message(f"HTML_SCAN | Page {page_num} | URL {current_url}")
        filtered_mode = "lt=4" in (current_url or "")
        _log_message(
            f"HTML_SCAN | Page {page_num} | Filtered mode: {filtered_mode}"
        )

        update_status(f"Processing All Logs page {page_num}...")
        update_progress(min(0.05 + page_num * 0.03, 0.9), f"Reading page {page_num}")

        candidates = []
        if filtered_mode:
            candidates = _extract_filtered_challenge_candidates(driver)
            _log_message(
                "HTML_SCAN | "
                f"Page {page_num} | Filtered challenge candidates found: {len(candidates)}"
            )

        if not candidates:
            candidates = _extract_html_log_candidates(
                driver,
                require_write_note=not filtered_mode,
            )
        _log_message(f"HTML_SCAN | Page {page_num} | Candidate blocks found: {len(candidates)}")
        if not candidates:
            update_status(
                "No Write Note candidates were detected on this page. "
                "The page layout likely differs from expected selectors.",
                ft.Colors.YELLOW,
            )
            break

        visit_log_map = _extract_visit_log_links_by_gc_code(driver)
        _log_message(
            f"HTML_SCAN | Page {page_num} | Visit log links mapped: {len(visit_log_map)}"
        )

        page_checked = 0
        page_matches = 0

        for candidate in candidates:
            try:
                entry_text = (candidate.get("text") or "").strip()
                if (not filtered_mode) and ("write note" not in entry_text.lower()):
                    continue

                cache_url = (candidate.get("href") or "").strip()
                cache_name = (candidate.get("title") or "").strip()
                metadata = (candidate.get("metadata") or "").strip()
                if not cache_url or not cache_name:
                    continue

                gc_code = (candidate.get("gcCode") or "").strip().upper()
                if not gc_code:
                    gc_match = re.search(
                        r"/geocache/(GC[A-Z0-9]+)|[?&]wp=(GC[A-Z0-9]+)",
                        cache_url,
                        re.IGNORECASE,
                    )
                    if gc_match:
                        gc_code = (gc_match.group(1) or gc_match.group(2) or "").upper()
                if not gc_code:
                    continue

                total_checked += 1
                page_checked += 1
                date_text = (candidate.get("date") or "").strip()
                geocaching_log_url = _normalize_geocaching_log_url(
                    candidate.get("visitLogHref") or candidate.get("logHref") or ""
                )
                if not geocaching_log_url:
                    geocaching_log_url = visit_log_map.get(gc_code, "")

                if _is_target_challenge_cache(cache_name, entry_text, metadata):
                    row = {
                        "log_date": date_text,
                        "gc_code": gc_code,
                        "cache_name": cache_name,
                        "cache_url": cache_url,
                        "log_url": geocaching_log_url,
                        "checker_status": "Failed",
                        "checker_example_log": "",
                    }
                    results.append(row)
                    _write_in_progress_csv(results)

                    update_status(
                        f"Opening cache page for checker: {gc_code} {cache_name[:60]}..."
                    )
                    checker_url, checker_example_log, checker_status = _open_checker_for_cache(
                        driver,
                        cache_url,
                        cache_name,
                        update_status,
                        keep_checker_tab_open=(stop_after_matches > 0 and len(results) + 1 >= stop_after_matches),
                    )
                    _log_message(
                        f"HTML_SCAN | Match | {gc_code} | {cache_name} | {date_text or 'no-date'}"
                    )
                    row["checker_status"] = checker_status
                    row["checker_example_log"] = checker_example_log
                    _write_in_progress_csv(results)
                    page_matches += 1

                    if stop_after_matches > 0 and len(results) >= stop_after_matches:
                        _log_message(
                            "HTML_SCAN | DEBUG_STOP | "
                            f"Processed {len(results)} match(es); leaving browser open on checker tab"
                        )
                        update_progress(1.0, f"Temporary stop after {len(results)} log(s) (debug)")
                        update_status(
                            f"Temporary debug stop after {len(results)} log(s). Browser left open on checker tab.",
                            ft.Colors.YELLOW,
                        )
                        return results
            except Exception:
                continue

        update_status(
            f"Page {page_num}: checked {page_checked} Write Note entries,"
            f" {page_matches} mystery challenge matches on this page,"
            f" {len(results)} total found."
        )
        update_progress(
            min(0.1 + page_num * 0.06, 0.95),
            f"Page {page_num}: {total_checked} logs checked",
        )

        # Navigate to next page
        next_found = False
        for next_sel in [
            "a[rel='next']",
            ".pagination .next a",
            "a.next",
            "[aria-label='Next page']",
            ".pager-next a",
            "a[title='Next']",
        ]:
            try:
                next_btn = driver.find_element(By.CSS_SELECTOR, next_sel)
                _log_message(f"HTML_SCAN | Page {page_num} | Clicking next via selector: {next_sel}")
                next_btn.click()
                time.sleep(3)
                next_found = True
                break
            except Exception:
                pass

        if not next_found:
            _log_message(f"HTML_SCAN | Page {page_num} | No next-page control found")
            break

    update_progress(1.0, f"Done – {total_checked} Write Notes scanned")
    update_status(
        f"HTML scan complete. Checked {total_checked} Write Note logs,"
        f" found {len(results)} on mystery Challenge Caches.",
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
        _log_message(f"EXPORT | {msg}")

    if not results:
        update_status("No results to export.", ft.Colors.YELLOW)
        return False, "No results to export.", None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    home_dir = Path.home()
    csv_path = home_dir / f"challenge_write_notes_{timestamp}.csv"

    fieldnames = [
        "log_date",
        "gc_code",
        "cache_name",
        "cache_url",
        "log_url",
        "checker_status",
        "checker_example_log",
    ]

    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
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

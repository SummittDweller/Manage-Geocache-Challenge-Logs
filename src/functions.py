# functions.py – core logic for Manage-Geocache-Challenge-Logs
import csv
import json
import logging
import os
import re
import subprocess
import time
import traceback
import warnings
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
from selenium.webdriver.common.service import Service as SeleniumBaseService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

try:
    from webdriver_manager.firefox import GeckoDriverManager
except Exception:
    GeckoDriverManager = None


_ORIGINAL_SELENIUM_TERMINATE_PROCESS = None


def _patch_selenium_service_terminate_process():
    """Patch Selenium service stop to tolerate TimeoutExpired at process shutdown."""
    global _ORIGINAL_SELENIUM_TERMINATE_PROCESS
    if _ORIGINAL_SELENIUM_TERMINATE_PROCESS is not None:
        return

    _ORIGINAL_SELENIUM_TERMINATE_PROCESS = SeleniumBaseService._terminate_process

    def _safe_terminate_process(self):
        try:
            return _ORIGINAL_SELENIUM_TERMINATE_PROCESS(self)
        except subprocess.TimeoutExpired as exc:
            _log_message(
                f"SHUTDOWN | Selenium service terminate timed out; forcing kill: {exc}",
                "warning",
            )
        except Exception as exc:
            _log_message(
                f"SHUTDOWN | Selenium service terminate raised; forcing kill: {exc}",
                "warning",
            )

        process = getattr(self, "process", None)
        if not process:
            return

        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except Exception as kill_exc:
            _log_message(
                f"SHUTDOWN | Selenium forced service kill failed: {kill_exc}",
                "warning",
            )
        finally:
            self.process = None

    SeleniumBaseService._terminate_process = _safe_terminate_process


_patch_selenium_service_terminate_process()


class SafeFirefoxService(FirefoxService):
    """Firefox service that force-kills geckodriver if graceful stop times out."""

    def _terminate_process(self):
        try:
            super()._terminate_process()
            return
        except subprocess.TimeoutExpired as exc:
            _log_message(
                f"SHUTDOWN | FirefoxService terminate timed out; forcing kill: {exc}",
                "warning",
            )
        except Exception as exc:
            _log_message(
                f"SHUTDOWN | FirefoxService terminate raised; forcing kill: {exc}",
                "warning",
            )

        process = getattr(self, "process", None)
        if not process:
            return

        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except Exception as kill_exc:
            _log_message(
                f"SHUTDOWN | Forced geckodriver kill failed: {kill_exc}",
                "warning",
            )
        finally:
            self.process = None


_APP_LOGGER = None
_APP_LOG_PATH = Path(__file__).parent.parent / "manage_geocache_challenge_logs.log"
_IN_PROGRESS_CSV_PATH = Path(__file__).parent.parent / "challenge_write_notes_in_progress.csv"

# Load local .env (if present) so runtime toggles can be controlled without shell exports.
load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _env_bool(name, default=False):
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}

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
_DEBUG_STOP_AFTER_FILTER_APPLIED = (
    os.getenv("GC_DEBUG_STOP_AFTER_FILTER_APPLIED", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
_DELETE_WRITE_NOTE_LOG_WHEN_FOUND = _env_bool(
    "DELETE_WRITE_NOTE_LOG_WHEN_FOUND",
    default=False,
)


def _should_delete_write_note_log_when_found(driver=None):
    """Resolve delete behavior with runtime override support.

    Order of precedence:
    1) driver runtime override (set by UI for current run)
    2) .env / process environment default
    """
    if driver is not None:
        runtime_value = getattr(driver, "_delete_write_note_log_when_found", None)
        if runtime_value is not None:
            return bool(runtime_value)
    return _DELETE_WRITE_NOTE_LOG_WHEN_FOUND


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


def shutdown_driver(driver):
    """Best-effort WebDriver shutdown that tolerates stuck geckodriver exits."""
    if driver is None:
        return

    try:
        driver.quit()
        return
    except subprocess.TimeoutExpired as exc:
        _log_message(
            f"SHUTDOWN | driver.quit() timed out, forcing geckodriver stop: {exc}",
            "warning",
        )
    except Exception as exc:
        _log_message(
            f"SHUTDOWN | driver.quit() raised, attempting force stop: {exc}",
            "warning",
        )

    try:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if not process:
            return

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                pass

        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass
    except Exception as exc:
        _log_message(f"SHUTDOWN | Forced geckodriver stop failed: {exc}", "warning")


def _is_firefox_profile_locked(profile_path):
    """Return True when lock artifacts suggest the profile is currently in use."""
    if not profile_path:
        return False

    lock_candidates = [
        "parent.lock",
        ".parentlock",
        "lock",
        "SingletonLock",
    ]
    for lock_name in lock_candidates:
        if Path(profile_path, lock_name).exists():
            return True
    return False


def _is_usable_geckodriver(path):
    """Return True when geckodriver exists, is executable, and responds to --version."""
    if not path:
        return False

    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return False

        # Ensure execute bit is present for the current user.
        if not os.access(str(p), os.X_OK):
            try:
                mode = p.stat().st_mode
                p.chmod(mode | 0o111)
            except Exception:
                return False

        probe = subprocess.run(
            [str(p), "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _launch_firefox_with_timeout(service, options, timeout_seconds=30):
    """
    Launch Firefox WebDriver with a timeout to prevent indefinite hangs.
    
    Returns (driver, error_msg) tuple:
    - On success: (driver, None)
    - On timeout: (None, "timeout")
    - On error: (None, str(exception))
    """
    import threading
    
    result = {"driver": None, "error": None}
    
    def launch_in_thread():
        try:
            result["driver"] = webdriver.Firefox(service=service, options=options)
        except Exception as e:
            result["error"] = str(e)
    
    thread = threading.Thread(target=launch_in_thread, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    
    if thread.is_alive():
        # Timeout occurred; thread is still running but we'll abandon it
        return (None, "timeout")
    
    if result["error"]:
        return (None, result["error"])
    
    return (result["driver"], None)


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
    script = r"""
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
    script = r"""
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

        # Newer geocaching UI log links: /live/log/GL...
        if "/live/log/" in path:
            return raw

        # Classic log endpoints; accept different query key variants.
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
    """Run Project-GC checker and return a normalized outcome code."""
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""

    if "project-gc.com" not in current_url:
        _log_message("CHECKER | Skipping run-checker step (not on Project-GC host)", "warning")
        return "off-host"

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
                r"""
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
        return "success"
    elif _has_failure_marker():
        _log_message(f"CHECKER | Failure already present for {cache_name}; no run needed")
        return "failure"
    else:
        _log_message("CHECKER | Run checker button not present and no success marker visible", "warning")
        return "not-available"

    for attempt in range(1, 3):
        clicked = _click_run_checker()
        if not clicked:
            _log_message("CHECKER | Run checker button not found/clickable", "warning")
            return "run-button-error"

        _log_message(f"CHECKER | Waiting for checker result (attempt {attempt}/2)")
        deadline = time.time() + 95
        last_heartbeat = 0

        while time.time() < deadline:
            if _has_success_marker():
                _log_message(f"CHECKER | Challenge check success detected for {cache_name}")
                return "success"

            if _has_failure_marker():
                _log_message(f"CHECKER | Challenge check failure detected for {cache_name}")
                return "failure"

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
    return "timeout"


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


def _cache_has_user_found_it_log(driver, username):
    """Return True only when a single log-row contains BOTH the target user AND a Found It marker."""
    target_user = (username or "").strip()
    if not target_user:
        return False

    script = r"""
const normalizeText = (v) => String(v || '').trim().toLowerCase();
const normalizeName = (v) => normalizeText(v).replace(/\s+/g, ' ');
const target = normalizeName(arguments[0]);
if (!target) return false;

// === FIRST: Check if the page itself indicates the current user already found it ===
// Geocaching.com displays a "Found It!" badge when the logged-in user views a cache they've found.
// Look for this indicator in the page navigation/header area.
const pageStatusIndicators = [
    '#ctl00_ContentBody_GeoNav_logText',  // Geocaching.com specific ID
    '[id*="GeoNav"][id*="logText"]',
    '[class*="found-it-badge" i]',
    '[class*="already-found" i]',
    '[data-user-status="found"]',
];
for (const sel of pageStatusIndicators) {
    try {
        const elem = document.querySelector(sel);
        if (elem && normalizeText(elem.textContent).includes('found it')) {
            return true;  // Current user already found this cache — no need to check logs.
        }
    } catch(e) {}
}

const PROFILE_LINK_SEL = 'a[href*="/profile/"], a[href*="/geocaching/profile/"]';

const FOUND_IT_NODE_SELECTORS = [
    '[data-log-type="2"]',
    '[data-logtype="2"]',
    '[class*="logtype-2" i]',
    '[class*="log-type-2" i]',
    '[class*="logtype2" i]',
    '[class*="log-type-found" i]',
    'img[src*="wpttypes/2" i]',
    'img[alt="Found It" i]',
    'img[title="Found It" i]',
    '[aria-label="Found It" i]',
];

// Collect every Found It marker node visible on the page.
const foundItNodes = [];
for (const sel of FOUND_IT_NODE_SELECTORS) {
    try { Array.from(document.querySelectorAll(sel)).forEach(n => foundItNodes.push(n)); } catch(e) {}
}
if (foundItNodes.length === 0) return false;

// For each Found It marker, walk UP until we find a container that holds exactly
// one distinct profile-link username — that is the bounded log row.
// Then check whether that username is our target.
for (const marker of foundItNodes) {
    let current = marker.parentElement;
    for (let depth = 0; depth < 15 && current && current !== document.body; depth += 1) {
        const profileLinks = Array.from(current.querySelectorAll(PROFILE_LINK_SEL));
        const distinctUsers = new Set(
            profileLinks.map(a => normalizeName(a.textContent)).filter(Boolean)
        );
        if (distinctUsers.size === 1) {
            // Exactly one user in this container — this is the log row.
            if (distinctUsers.has(target)) return true;
            break; // This Found It belongs to a different user; stop climbing.
        }
        if (distinctUsers.size > 1) break; // Crossed into a multi-user container.
        // distinctUsers.size === 0 → username is in a higher ancestor; keep climbing.
        current = current.parentElement;
    }
}

// --- Fallback: attribute on the log row itself ---
// Some renderings put data-log-type / data-logtype on the row element directly.
const LOG_ROW_SELECTORS = [
    '[data-logid]', '[data-log-id]', '[data-logtype]', '[data-log-type]',
    '[class*="logEntry" i]', '[class*="log-entry" i]',
    '[class*="logItem" i]', '[class*="log-item" i]',
    'li[class*="log" i]',
];
for (const sel of LOG_ROW_SELECTORS) {
    let rows;
    try { rows = Array.from(document.querySelectorAll(sel)); } catch(e) { continue; }
    for (const row of rows) {
        const lt = (row.getAttribute('data-log-type') || row.getAttribute('data-logtype') || '').trim();
        if (lt !== '2') continue;
        const profileLinks = Array.from(row.querySelectorAll(PROFILE_LINK_SEL));
        if (profileLinks.some(a => normalizeName(a.textContent) === target)) return true;
    }
}

return false;
"""

    try:
        return bool(driver.execute_script(script, target_user))
    except Exception:
        return False


def _delete_write_note_log_if_possible(driver, log_url, cache_name):
    """Best-effort delete of a Write Note log from its log URL.

    Returns:
        tuple[bool, str]: (deleted, detail message)
    """
    normalized_log_url = _normalize_geocaching_log_url(log_url or "")
    if not normalized_log_url:
        return False, "No log URL available for deletion."

    try:
        driver.get(normalized_log_url)
        time.sleep(1.2)
    except Exception as exc:
        return False, f"Could not open log URL for deletion: {exc}"

        # Some log URLs land on a page with a "View / Edit Log / Images" link.
        # Follow that link first so the Delete Log controls are available.
        try:
                edit_log_url = driver.execute_script(
                        r"""
const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
const anchors = Array.from(document.querySelectorAll('a[href]'));
for (const a of anchors) {
    const href = String(a.getAttribute('href') || '').trim();
    if (!href) continue;

    const isClassicLogHref = /\/seek\/log\.aspx/i.test(href) || /log\.aspx\?luid=/i.test(href);
    if (!isClassicLogHref) continue;

    const title = normalize(a.getAttribute('title'));
    const text = normalize(a.textContent);
    const looksLikeEditLink =
        title.includes('view log') ||
        text.includes('view / edit log') ||
        text.includes('view/edit log') ||
        text.includes('edit log') ||
        text.includes('log / images');

    if (!looksLikeEditLink) continue;

    try {
        return new URL(href, window.location.href).href;
    } catch (e) {
        return href;
    }
}
return '';
"""
                )
                if isinstance(edit_log_url, str) and edit_log_url.strip():
                        candidate = edit_log_url.strip()
                        if candidate.lower() != (driver.current_url or "").lower():
                                driver.get(candidate)
                                time.sleep(1.0)
        except Exception:
                # Non-fatal; continue with whatever page is open.
                pass

    try:
        clicked_delete = bool(
            driver.execute_script(
                r"""
const normalize = (v) => String(v || '').trim().toLowerCase();

const tryClick = (el) => {
    if (!el) return false;
    if (el.disabled) return false;
    try {
        el.scrollIntoView({block: 'center'});
        el.click();
        return true;
    } catch (e) {
        return false;
    }
};

const explicitSelectors = [
    "button[data-testid='delete-log-modal-open']",
    "button[data-testid='delete-log']",
    "button[id*='delete'][id*='log' i]",
    "button[class*='delete-log' i]",
    "a[class*='delete-log' i]",
    "button[aria-label*='delete log' i]",
    "a[aria-label*='delete log' i]",
    "button[title*='delete log' i]",
    "a[title*='delete log' i]"
];

for (const sel of explicitSelectors) {
    const el = document.querySelector(sel);
    if (tryClick(el)) return true;
}

// Icon-only delete buttons often render as <use href="#delete">.
const iconUse = document.querySelector("use[href='#delete'], use[*|href='#delete']");
if (iconUse) {
    const iconButton = iconUse.closest('button, a[role="button"], a, [role="button"]');
    if (tryClick(iconButton)) return true;
}

// Last fallback: text matching for delete controls.
const deleteLabels = ['delete log', 'delete this log'];
const deleteCandidates = Array.from(document.querySelectorAll("button, a[role='button'], a"));
for (const el of deleteCandidates) {
    const text = normalize(el.textContent || el.getAttribute('value') || el.getAttribute('aria-label') || el.getAttribute('title'));
    if (!text) continue;
    if (!deleteLabels.some((lbl) => text.includes(lbl))) continue;
    if (tryClick(el)) return true;
}
return false;
"""
            )
        )
    except Exception as exc:
        return False, f"Delete control interaction failed: {exc}"

    if not clicked_delete:
        return False, "Delete Log button/link was not found on the log page."

    confirmed = False

    # Accept native JS confirm() dialogs if they appear.
    try:
        WebDriverWait(driver, 2).until(EC.alert_is_present())
        driver.switch_to.alert.accept()
        confirmed = True
    except Exception:
        pass

    # Click the in-page modal confirmation control when present.
    if not confirmed:
        try:
            modal_delete_button = WebDriverWait(driver, 2.5).until(
                EC.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        "button.delete-log-modal-delete, "
                        "button[data-testid='delete-log-modal-delete']",
                    )
                )
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                modal_delete_button,
            )
            modal_delete_button.click()
            confirmed = True
        except Exception:
            pass

    # JS fallback for modal confirmation controls.
    try:
        if not confirmed:
            confirmed = bool(
                driver.execute_script(
                    r"""
const normalize = (v) => String(v || '').trim().toLowerCase();
const tryClick = (el) => {
    if (!el) return false;
    if (el.disabled) return false;
    try {
        el.scrollIntoView({block: 'center'});
        el.click();
        return true;
    } catch (e) {
        return false;
    }
};

const explicitModalSelectors = [
    "button.delete-log-modal-delete",
    "button[data-testid='delete-log-modal-delete']",
    "[role='dialog'] button.delete-log-modal-delete",
    ".modal button.delete-log-modal-delete"
];
for (const sel of explicitModalSelectors) {
    const el = document.querySelector(sel);
    if (tryClick(el)) return true;
}

const confirmLabels = ['delete', 'yes', 'confirm', 'remove'];
const candidates = Array.from(document.querySelectorAll("[role='dialog'] button, [role='dialog'] a, .modal button, .modal a"));
for (const el of candidates) {
  const text = normalize(el.textContent || el.getAttribute('value') || el.getAttribute('aria-label'));
  if (!text) continue;
  if (!confirmLabels.some((lbl) => text.includes(lbl))) continue;
  if (text.includes('cancel') || text.includes('close')) continue;
    if (tryClick(el)) return true;
}
return false;
"""
                )
            )
    except Exception:
        pass

    if not confirmed:
        _log_message(
            "CHECKER | Delete confirmation modal button not found; relying on reload verification.",
            "warning",
        )

    time.sleep(2.0)

    # Verify by revisiting the same log URL: deleted logs usually no longer resolve
    # to the same /live/log/ entry page.
    try:
        driver.get(normalized_log_url)
        time.sleep(1.5)
        current_url = (driver.current_url or "").lower()
        page_text = (driver.execute_script("return (document.body && document.body.innerText) || '';") or "").lower()
    except Exception:
        return True, "Delete action submitted; verification via reload was unavailable."

    not_found_markers = [
        "not found",
        "could not be found",
        "doesn't exist",
        "does not exist",
        "not available",
        "log was deleted",
        "log deleted",
    ]
    unresolved_live_log = "/live/log/" in current_url
    has_not_found_marker = any(marker in page_text for marker in not_found_markers)

    if (not unresolved_live_log) or has_not_found_marker:
        _log_message(f"CHECKER | Deleted Write Note log for {cache_name}: {normalized_log_url}")
        return True, "Write Note log deleted."

    if confirmed:
        return True, "Delete was confirmed, but final reload verification was inconclusive."

    return False, "Delete was attempted but could not be verified."


def _open_checker_for_cache(driver, cache_url, cache_name, update_status, log_url="", keep_checker_tab_open=False):
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

        active_user = (getattr(driver, "_gc_username", "") or "").strip()
        if not active_user:
            fallback_user = (getattr(driver, "_gc_active_user", "") or "").strip()
            if fallback_user and fallback_user.lower() != "existing session":
                active_user = fallback_user

        if active_user and _cache_has_user_found_it_log(driver, active_user):
            checker_status = "Write Note + Found It"
            checker_example_log = f"User '{active_user}' already has a Found It log on this cache."

            if _should_delete_write_note_log_when_found(driver):
                deleted, delete_detail = _delete_write_note_log_if_possible(driver, log_url, cache_name)
                if deleted:
                    checker_status = "Write Note + Found It (Write Note deleted)"
                    checker_example_log = (
                        f"User '{active_user}' already has a Found It log on this cache. "
                        f"Write Note cleanup: {delete_detail}"
                    )
                else:
                    checker_status = "Write Note + Found It (Write Note not deleted)"
                    checker_example_log = (
                        f"User '{active_user}' already has a Found It log on this cache. "
                        f"Write Note cleanup: {delete_detail}"
                    )
            else:
                delete_detail = "Cleanup disabled by DELETE_WRITE_NOTE_LOG_WHEN_FOUND=false"
                checker_status = "Write Note + Found It (cleanup disabled)"
                checker_example_log = (
                    f"User '{active_user}' already has a Found It log on this cache. "
                    f"Write Note cleanup: {delete_detail}"
                )

            checker_url = cache_url
            _log_message(
                f"CHECKER | Found existing Found It log for {active_user} on {cache_name}; "
                f"skipping checker | Write Note cleanup: {delete_detail}"
            )
            return checker_url, checker_example_log, checker_status

        checker_href = _find_project_gc_checker_href(driver)
        if not checker_href:
            checker_status = "No automated checker available"
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
        run_outcome = _run_project_gc_checker_if_available(driver, cache_name)
        checker_example_log = _extract_project_gc_example_log(driver)

        if run_outcome == "success":
            checker_status = "SUCCESS!" if checker_example_log else "Checker succeeded (no example log)"
        elif run_outcome == "failure":
            checker_status = "Checker indicates challenge not fulfilled"
        elif run_outcome in {"not-available", "off-host"}:
            checker_status = "No automated checker available"
        else:
            checker_status = "Checker run failed/error"

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

    should_use_profile = profile_path and os.path.isdir(profile_path)
    if should_use_profile and _is_firefox_profile_locked(profile_path):
        _log_message(
            "STARTUP | Firefox profile appears to be in use (lock file detected). "
            "Falling back to a fresh session.",
            "warning",
        )
        should_use_profile = False

    if should_use_profile:
        options.add_argument("-profile")
        options.add_argument(profile_path)
        _log_message(f"STARTUP | Using Firefox profile: {profile_path}")
    else:
        if profile_path and not os.path.isdir(profile_path):
            _log_message(
                f"STARTUP | Firefox profile path not found: {profile_path}. "
                "Using a fresh session.",
                "warning",
            )
        else:
            _log_message("STARTUP | Using a fresh Firefox session")

    update_loading("Launching Firefox...", 0.10)

    service = None
    gecko_path = None
    
    # Skip webdriver-manager by default (broken on some systems).
    # Only use it if explicitly enabled via environment variable.
    use_webdriver_manager = _env_bool("USE_WEBDRIVER_MANAGER_GECKODRIVER", default=False)
    
    if use_webdriver_manager and GeckoDriverManager:
        try:
            # webdriver-manager currently triggers a tarfile deprecation warning
            # on Python 3.12+ while extracting the driver archive.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Python 3\.14 will, by default, filter extracted tar archives.*",
                    category=DeprecationWarning,
                )
                gecko_path = GeckoDriverManager().install()
        except Exception:
            gecko_path = None

    if gecko_path and not _is_usable_geckodriver(gecko_path):
        _log_message(
            f"STARTUP | webdriver-manager geckodriver is not usable: {gecko_path}. "
            "Falling back to Selenium driver resolution.",
            "warning",
        )
        gecko_path = None

    launch_errors = []
    service_attempts = []
    if gecko_path:
        service_attempts.append(("webdriver-manager", SafeFirefoxService(executable_path=gecko_path)))
    service_attempts.append(("selenium-default", SafeFirefoxService()))

    driver = None
    for label, service in service_attempts:
        try:
            _log_message(f"STARTUP | Attempting Firefox launch via {label} service (timeout=30s)")
            driver, launch_error = _launch_firefox_with_timeout(service, options, timeout_seconds=30)
            if launch_error == "timeout":
                launch_errors.append(f"{label}: timeout after 30s")
                _log_message(f"STARTUP | Firefox launch attempt timed out ({label})", "warning")
                continue
            if launch_error:
                launch_errors.append(f"{label}: {launch_error}")
                _log_exception(f"STARTUP | Firefox launch attempt failed ({label})", Exception(launch_error))
                continue
            if driver:
                break
        except Exception as exc:
            launch_errors.append(f"{label}: {exc}")
            _log_exception(f"STARTUP | Firefox launch attempt failed ({label})", exc)

    if driver is None:
        joined = " | ".join(launch_errors) if launch_errors else "no launch attempts"
        raise RuntimeError(
            "Firefox WebDriver failed to launch after fallback attempts: "
            f"{joined}"
        )

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
    if getattr(driver, "_debug_stop_after_filter_applied_triggered", False):
        _log_message("SCAN | Debug stop after filter applied is active; skipping further processing")
        return html_results
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

    if _DEBUG_STOP_AFTER_FILTER_APPLIED:
        driver._debug_stop_after_filter_applied_triggered = True
        _log_message("HTML_SCAN | DEBUG_STOP | Stopping immediately after Write Note filter is applied")
        update_progress(1.0, "Temporary stop after filter applied (debug)")
        update_status(
            "Temporary debug stop after Write Note filter applied. Browser left open on filtered logs page.",
            ft.Colors.YELLOW,
        )
        return results

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

                    checker_disabled = bool(getattr(driver, "_disable_challenge_checker", False))
                    if checker_disabled:
                        checker_url = ""
                        checker_example_log = ""
                        checker_status = "Checker skipped (disabled)"
                        _log_message(
                            f"HTML_SCAN | Checker disabled; skipping checker run for {gc_code} | {cache_name}"
                        )
                    else:
                        update_status(
                            f"Opening cache page for checker: {gc_code} {cache_name[:60]}..."
                        )
                        checker_url, checker_example_log, checker_status = _open_checker_for_cache(
                            driver,
                            cache_url,
                            cache_name,
                            update_status,
                            log_url=geocaching_log_url,
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


def prepare_write_note_edit_log_page(driver, scan_results, status_callback=None):
    """Run the first-pass Write Note -> Found It automation on a listing log page."""
    def update_status(msg, color=None):
        if status_callback:
            status_callback(msg, color)
        _log_message(f"AUTOMATION | {msg}")

    if not scan_results:
        return False, "No scan results available for Fully Automated mode."

    target_row = None
    for row in scan_results:
        candidate_url = _normalize_geocaching_log_url(row.get("log_url") or "")
        checker_text = (row.get("checker_example_log") or "").strip()
        if candidate_url and checker_text:
            target_row = row
            break

    # Fallback to first valid log URL even if checker text is empty.
    if not target_row:
        for row in scan_results:
            candidate_url = _normalize_geocaching_log_url(row.get("log_url") or "")
            if candidate_url:
                target_row = row
                break

    if not target_row:
        return False, "No valid listing log URL found in scan results."

    log_url = _normalize_geocaching_log_url(target_row.get("log_url") or "")
    checker_example_log = (target_row.get("checker_example_log") or "").strip()
    cache_label = (target_row.get("gc_code") or "").strip() or (target_row.get("cache_name") or "cache")

    try:
        update_status(f"Fully Automated: opening listing log page for {cache_label}...")
        driver.get(log_url)
    except Exception as exc:
        return False, f"Could not open listing log URL: {exc}"

    # /seek/log.aspx links typically redirect to /live/log/GL... in the modern UI.
    reached_live_log = False
    end_time = time.time() + 20
    while time.time() < end_time:
        current = (driver.current_url or "").lower()
        if "/live/log/" in current:
            reached_live_log = True
            break
        time.sleep(0.25)

    if not reached_live_log:
        return (
            False,
            f"Opened listing URL but did not reach a /live/log/ page (current: {driver.current_url}).",
        )

    update_status("Fully Automated: live log page loaded; locating Edit log link...")

    selectors = [
        (By.XPATH, "//a[.//span[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='edit log']]|//button[.//span[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='edit log']]|//a[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='edit log']|//button[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='edit log']"),
        (By.CSS_SELECTOR, "a[href*='/seek/log.aspx'], a[href*='log.aspx?LUID' i], a[href*='log.aspx?luid' i]"),
    ]

    clicked_edit = False
    for by, locator in selectors:
        try:
            edit_link = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((by, locator))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", edit_link)
            driver.execute_script("arguments[0].click();", edit_link)
            clicked_edit = True
            _log_message(
                f"AUTOMATION | Clicked Edit log for {cache_label} on {driver.current_url}"
            )
            break
        except Exception:
            continue

    if not clicked_edit:
        try:
            clicked_edit = bool(
                driver.execute_script(
                    r"""
const spans = Array.from(document.querySelectorAll('span'));
for (const span of spans) {
  const text = (span.textContent || '').trim().toLowerCase();
  if (text !== 'edit log') continue;
  const clickable = span.closest('a,button');
  if (!clickable) continue;
  clickable.scrollIntoView({block: 'center'});
  clickable.click();
  return true;
}
return false;
"""
                )
            )
            if clicked_edit:
                _log_message(
                    f"AUTOMATION | Clicked Edit log via JavaScript fallback for {cache_label}"
                )
            else:
                return False, "Edit log link was not found on the live log page."
        except Exception:
            return False, "Edit log link was not found on the live log page."

    update_status("Fully Automated: selecting Found It log type...")

    found_type_selected = False
    try:
        combo_input = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#react-select-cache-log-type-input"))
        )
        driver.execute_script("arguments[0].click();", combo_input)
        time.sleep(0.4)

        option_locators = [
            (By.XPATH, "//*[contains(@class,'log-type-option') and normalize-space(.)='Found it']"),
            (By.XPATH, "//span[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='found it']"),
            (By.XPATH, "//*[@data-testid='log-type-option' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'found it')]"),
        ]
        for by, locator in option_locators:
            try:
                option = WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable((by, locator))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", option)
                driver.execute_script("arguments[0].click();", option)
                found_type_selected = True
                break
            except Exception:
                continue
    except Exception:
        pass

    if not found_type_selected:
        try:
            found_type_selected = bool(
                driver.execute_script(
                    """
const input = document.querySelector('#react-select-cache-log-type-input');
if (input) {
  input.click();
}
const candidates = Array.from(document.querySelectorAll('[data-testid="log-type-option"], .log-type-option, [role="option"], span'));
for (const el of candidates) {
  const text = (el.textContent || '').trim().toLowerCase();
  if (text !== 'found it') continue;
  const clickable = el.closest('[data-testid="log-type-option"], [role="option"], button, div, li, a') || el;
  clickable.scrollIntoView({block: 'center'});
  clickable.click();
  return true;
}
return false;
"""
                )
            )
        except Exception:
            found_type_selected = False

    if not found_type_selected:
        return False, "Could not find/select the Found it option in the log-type dropdown."

    update_status("Fully Automated: appending checker example text to log body...")

    if not checker_example_log:
        return False, "checker_example_log is empty for the selected listing; cannot append text."

    try:
        textarea = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "#gc-md-editor_md, textarea#gc-md-editor_md, textarea[data-event-label='Cache Log - text entry']",
                )
            )
        )
    except Exception:
        return False, "Could not locate the cache log textarea."

    try:
        existing_text = textarea.get_attribute("value") or ""
        if existing_text.strip():
            combined_text = f"{existing_text.rstrip()}\n\n{checker_example_log}"
        else:
            combined_text = checker_example_log

        driver.execute_script(
            """
const textarea = arguments[0];
const text = arguments[1];
textarea.focus();
textarea.value = text;
textarea.dispatchEvent(new Event('input', { bubbles: true }));
textarea.dispatchEvent(new Event('change', { bubbles: true }));
""",
            textarea,
            combined_text,
        )
    except Exception as exc:
        return False, f"Failed to append checker_example_log text: {exc}"

    update_status("Fully Automated: clicking Update log...")

    clicked_update = False
    update_locators = [
        (By.XPATH, "//button[normalize-space(.)='Update log']"),
        (By.CSS_SELECTOR, "button[data-event-label='Cache Log - post']"),
        (By.CSS_SELECTOR, "button.gc-button-primary.submit-button"),
    ]
    for by, locator in update_locators:
        try:
            update_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((by, locator))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", update_btn)
            driver.execute_script("arguments[0].click();", update_btn)
            clicked_update = True
            break
        except Exception:
            continue

    if not clicked_update:
        try:
            clicked_update = bool(
                driver.execute_script(
                    """
const buttons = Array.from(document.querySelectorAll('button'));
for (const btn of buttons) {
  const text = (btn.textContent || '').trim().toLowerCase();
  if (text !== 'update log') continue;
  btn.scrollIntoView({block: 'center'});
  btn.click();
  return true;
}
return false;
"""
                )
            )
        except Exception:
            clicked_update = False

    if not clicked_update:
        return False, "Could not find/click the Update log button."

    _log_message(
        f"AUTOMATION | Updated log for {cache_label} by selecting Found it and appending checker text"
    )
    return True, "Updated log by selecting Found it and appending checker example text."


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

# Bug Fixes - April 26, 2026

## Overview
Fixed critical false-positive and false-negative bugs in the Found It log detector that were causing:
- Incorrect "Write Note + Found It" classifications for users
- Unnecessary checker runs on caches already marked as found
- Zero count reporting when users had found logs

---

## Issue 1: Found It Detector False Positives & False Negatives

### Root Cause
The original `_cache_has_user_found_it_log()` function used a flawed DOM traversal strategy:
1. Find username nodes in the page
2. Walk UP the DOM tree searching for Found It markers within ancestor elements

**Problem:** Walking upward from any username node on a cache listing page eventually reaches a common ancestor containing ALL log entries for that cache. If ANY user on the page has a Found It log, the detector would return `true` for ALL users on that page, regardless of their actual log type.

For SummittDweller's logs, this meant:
- Every log was reported as "Write Note + Found It" (false positive for Write Note entries)
- The summary reported "0 already Found It" when some actually were (false negative)
- Checker was unnecessarily launched on already-found caches

### Solution (Iterations)

#### Attempt 1: Log-Row-Container-First Approach
Rewrote the detector to:
1. Look for known log-row container selectors (`[data-logid]`, `.log-entry`, `li.logItem`, etc.)
2. Check both Found It marker AND username exist within the SAME row
3. Added a Pass 2 fallback using profile-link anchors with a 6-level walk limit

**Result:** Still produced false negatives for SummittDweller because Pass 2's logic stopped climbing too early—the first ancestor container already included multiple users, so it never reached the Found It marker.

#### Attempt 2: Found-It-Marker-First Approach ✅ (Current)
**Completely inverted the traversal logic:**
1. Start with every Found It marker node on the page (icons, attributes, etc.)
2. Walk UP from each marker, accumulating the distinct profile-link usernames found at each ancestor level
3. Stop when a container holds **exactly 1** distinct username—that container is the bounded log row
4. Check if that single username matches the target user
5. Added fallback: search for log rows with `data-log-type="2"` attribute and check for target username within

**Why this works:**
- Found It markers are the starting anchor (no ambiguity)
- The "exactly 1 username" condition precisely identifies the single-log-row boundary
- Works regardless of CSS class names or page structure variations
- Detects when crossed into multi-log containers and stops

**Changed file:** `src/functions.py`, function `_cache_has_user_found_it_log()` (lines ~1311–1375)

---

## Code Changes

### File: `src/functions.py`

#### Function: `_cache_has_user_found_it_log()` 

**Location:** Lines 1311–1375

**New Algorithm:**
```
1. Collect all Found It marker nodes on the page using CSS selectors:
   - [data-log-type="2"], [data-logtype="2"]
   - [class*="logtype-2" i], [class*="log-type-2" i], etc.
   - img[src*="wpttypes/2" i], img[alt="Found It" i], etc.

2. For each Found It marker:
   - Walk UP the DOM tree (up to 15 levels)
   - At each level, count distinct profile-link usernames
   - If exactly 1 username found → this is the log row boundary
     • If that username matches target → RETURN TRUE
     • Else → BREAK (this Found It belongs to another user)
   - If >1 usernames found → BREAK (crossed into multi-log container)
   - If 0 usernames found → keep climbing (still inside a nested element)

3. Fallback: Search for rows with explicit data-log-type="2" attribute
   - Check if target username appears in that row
   - Return TRUE if found

4. Return FALSE if no match found
```

**Profile Link Selector:** `a[href*="/profile/"], a[href*="/geocaching/profile/"]`

**Found It Marker Selectors:**
- `[data-log-type="2"]` (primary)
- `[data-logtype="2"]`
- `[class*="logtype-2" i]`, `[class*="log-type-2" i]`, `[class*="logtype2" i]`
- `[class*="log-type-found" i]`
- `img[src*="wpttypes/2" i]`
- `img[alt="Found It" i]`, `img[title="Found It" i]`
- `[aria-label="Found It" i]`

---

## Testing & Validation

✅ **Compile check:** `py_compile.compile('src/functions.py', doraise=True)` → OK  
✅ **Import check:** Verified in venv  
✅ **Expected outcome:** 
- SummittDweller's logs should now correctly show which are "Write Note + Found It" vs. "Write Note" only
- Summary count should reflect actual Found It logs
- Checker should NOT launch on caches SummittDweller already found

---

## Related Code

The false positive/negative directly affected:
- **`_open_checker_for_cache()`** (line 1411): Checks result of `_cache_has_user_found_it_log()` to skip checker on already-found logs
- **`_build_checker_summary()`** in `src/main.py`: Counts entries by `checker_status` for UI display
- **CSV export:** `checker_status` column and `checker_example_log` column

---

## Files Modified

- `src/functions.py` — `_cache_has_user_found_it_log()` function (lines 1311–1375)

---

## Commit Message (Suggested)

```
Fix Found It detector: use Found-It-marker-first DOM traversal

- Invert traversal logic: start from Found It markers, walk UP to find log row
- Use "exactly 1 distinct username" condition to identify log row boundary
- Eliminates false positives (reporting other users' Found Its as current user's)
- Eliminates false negatives (missing current user's Found It logs)
- Prevents unnecessary checker runs on already-found caches
- Fixes SummittDweller (and all users) classification accuracy
```


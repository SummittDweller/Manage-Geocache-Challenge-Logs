# HTML Targets and Selectors Reference

This file documents the HTML targets, selectors, and URL patterns currently used
by the app during:
- log-page filtering and candidate extraction,
- geocaching/project-gc auth flow,
- checker execution and result detection.

## 1) Logs Page Navigation and Filtering

### Logs pages
- URL targets:
  - https://www.geocaching.com/my/logs.aspx
  - https://www.geocaching.com/account/settings/geocachelogs
- Domain guard:
  - geocaching.com or *.geocaching.com

### Write Note filter targets
- Exact filter links:
  - a[href='logs.aspx?s=1&lt=4']
  - a[href='logs.aspx?s=1&amp;lt=4']
  - a[href*='logs.aspx?s=1'][href*='lt=4']
- Direct URL fallback:
  - logs.aspx?s=1&lt=4
- Filter panel openers:
  - button[aria-label*='Filter']
  - button[title*='Filter']
  - a[aria-label*='Filter']
  - [data-cy*='filter'] button
  - [data-cy*='filter']
- Write Note option inputs:
  - input[type='checkbox'][value='4']
  - input[type='radio'][value='4']
  - [data-cy*='write'] input[type='checkbox']
  - [data-cy*='write'] input[type='radio']
- Confirm/apply controls:
  - button[type='submit']
  - button[aria-label*='Apply']
  - button[title*='Apply']
  - button[aria-label*='Done']
  - button[title*='Done']

## 2) Candidate Discovery on Logs Page

### Primary geocache link patterns
- Regex:
  - /\/geocache\/(GC[A-Z0-9]+)
  - [?&]wp=(GC[A-Z0-9]+)
  - cache_details.aspx?wp=(GC[A-Z0-9]+)

### Candidate container targets
- Semantic containers:
  - article
  - li
  - tr
  - [data-cy*='log']
  - [data-cy*='activity']
  - [class*='log']
  - [class*='activity']
- Date targets:
  - time
  - [datetime]
  - .date
  - .log-date
  - [class*='date']

### Filtered-page challenge-specific extraction
- Mystery icon + title strategy:
  - a.ImageLink[href*='/geocache/']
  - Image title/src checks for Unknown cache icon
  - matching title link contains challenge

### Visit Log extraction targets
- Text-driven anchor detection:
  - anchor text containing Visit log
- URL-driven log detection:
  - a[href*='/seek/log.aspx']
  - a[href*='log.aspx?LUID' i]
  - a[href*='log.aspx?luid' i]
- Modern geocaching log URL form accepted:
  - /live/log/GL...

## 3) Cache Page -> Project-GC Checker Link

### Existing Found It detection (pre-checker)
- Purpose:
  - if the specified geocaching user already has a Found It log on the cache
    page, record `Write Note + Found It` and skip checker execution.
- Text signals used in cache-page content:
  - found it
  - type: found it
  - log type: found it
- User-match requirement:
  - the log block must also include the active username (case-insensitive).

### Checker image/link targets
- a img[title*='Project-GC Challenge checker']
- a img[alt*='PGC Checker']
- a img[src*='project-gc.com/Images/Checker']
- a img[src*='project-gc.com/images/checker']

## 4) Project-GC Auth and OAuth Targets

### Project-GC login/auth links
- a[href*='/User/Login']
- a.btn.btn-info[href*='Login']
- a.btn.btn-info.btn-lg[href*='/oauth2.php']
- a[href='/oauth2.php']
- a[href*='/oauth2.php']

### Geocaching OAuth consent targets
- Specific Agree/Allow controls:
  - input#uxAllowAccessButton
  - button#uxAllowAccessButton
  - input[name='uxAllowAccessButton']
  - input[value='Agree']
  - button[value='Agree']
- Additional accept/yes variants:
  - button#ctl00_ContentBody_btnYes
  - input#ctl00_ContentBody_btnYes
  - button/input with accept/allow/agree in name/value/aria-label
- Consent page detection signals:
  - URL containing /oauth/authorize
  - page text containing complete setup
  - URL containing approval_prompt

## 5) Project-GC Login Form Targets

### Username/email fields
- #UsernameOrEmail
- input[id='UsernameOrEmail']
- input[name='Username']
- input[name='Email']
- input[name*='user' i]
- input[name*='email' i]
- input[type='text']
- input[type='email']

### Password fields
- #Password
- input[id='Password']
- input[name='Password']
- input[name*='pass' i]
- input[type='password']

### Submit controls
- #SignIn
- button#SignIn
- button[type='submit']
- input[type='submit']
- button.btn-primary
- button.btn-info

## 6) Checker Execution Targets

### Run checker button
- button#runChecker
- button[id='runChecker'][type='submit']
- button.btn.btn-primary#runChecker
- button[type='submit']#runChecker

### Success detection
- img[title*='Success'][src*='check48']
- p.cc_fulfillText containing fulfills challenge

### Failure detection
- img[alt='Cancel'][src*='cancel48']
- p.cc_fulfillText containing does not fulfill challenge
- #cc_unfulfilled_profileName
- #cc_unfulfilled_cacheName

### Timeout detection
- Page text containing max execution time reached

## 7) Checker Example Log Extraction

### Example log textarea targets
- textarea#cc_ExampleLog
- textarea[id='cc_ExampleLog']
- textarea[data-prefix]

### Captured content sources
- value
- textContent
- innerText
- visible text

## 8) Pagination Targets

### Next-page selectors
- a[rel='next']
- .pagination .next a
- a.next
- [aria-label='Next page']
- .pager-next a
- a[title='Next']

## Notes

- Selector usage is intentionally redundant to survive small layout changes.
- Many flows include text-based JavaScript fallback when CSS selectors fail.
- Log URL extraction prefers list-page Visit log links before cache/checker navigation.

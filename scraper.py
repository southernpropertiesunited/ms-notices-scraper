#!/usr/bin/env python3
"""
SPU MS Public Notices Scraper
=============================
Scrapes foreclosure notices from mspublicnotices.org for target MS counties.
Parses borrower info, DOT dates, attorneys, auction details.
Pushes new entries to Google Sheet, skips duplicates.

Runs via GitHub Actions on Mon/Wed/Fri at 6AM CT -- zero AI charges.

SETUP (one-time):
1. Create a Google Cloud project at https://console.cloud.google.com
2. Enable Google Sheets API + Google Drive API
3. Create a Service Account, download JSON key
4. Share your Google Sheet with the service account email (Editor access)
5. In your GitHub repo Settings > Secrets, add:
   - GOOGLE_SERVICE_ACCOUNT_JSON  (paste entire JSON key contents)
   - NOTIFY_EMAIL                 (paul@southernpropertiesunited.com)
   - SMTP_USER                    (Gmail address for sending email)
   - SMTP_PASSWORD                (app password for sending email)
6. Push this repo and the workflow runs automatically.
"""

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import re
import sys
import time
import smtplib
import traceback
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================================
# CONFIGURATION
# ============================================================

COUNTIES = ["George", "Hancock", "Harrison", "Hinds", "Jackson", "Rankin", "Stone"]
SHEET_ID = "1cgGpocIQBdP_39tuI3Pqy4xJud7vZ2yCrHr4_PBH0Ro"
BASE_URL = "https://www.mspublicnotices.org"
SEARCH_URL = f"{BASE_URL}/Search.aspx"

# County checkbox indices on the redesigned mspublicnotices.org form
# Each county has a checkbox: ctl00$ContentPlaceHolder1$as1$lstCounty$N
COUNTY_INDICES = {
    "George": 19, "Hancock": 22, "Harrison": 23,
    "Hinds": 24, "Jackson": 29, "Rankin": 60, "Stone": 65,
}

# Google Sheets scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column layout in each county tab (1-indexed for gspread)
COL = {
    "scrape_date": 1,       # A
    "borrower": 2,          # B
    "county": 3,            # C
    "phone": 4,             # D
    "alt_phone": 5,         # E
    "current_address": 6,   # F
    "mailing_address": 7,   # G
    "parcel_id": 8,         # H
    "property_address": 9,  # I
    "dot_date": 10,         # J
    "filing_info": 11,      # K
    "attorney": 12,         # L
    "auction_date": 13,     # M
    "auction_time": 14,     # N
    "auction_location": 15, # O
    "legal_desc": 16,       # P
    "pub_dates": 17,        # Q
    "notice_url": 18,       # R
    "assessed_value": 19,   # S
}

FIRST_DATA_ROW = 7  # Row 7 is first data row (rows 1-6 are headers)

# Rate limit: seconds between HTTP requests
RATE_LIMIT = 1.5

# Maximum pages to paginate per county (prevents runaway loops)
MAX_PAGES = 20

# ============================================================
# SCRAPER
# ============================================================

class MSNoticesScraper:
    """Scrapes mspublicnotices.org for foreclosure notices."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self.session_url = None  # Set after first GET (cookieless session)

    def get_hidden_fields(self, html):
        """Extract ALL hidden input fields from page HTML."""
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if name:
                fields[name] = inp.get("value", "")
        return fields

    def _build_base_form(self, hidden, keywords="", search_type="AND"):
        """Build the base form data dict with common fields."""
        form = dict(hidden)
        form["__LASTFOCUS"] = ""
        form["ctl00$ContentPlaceHolder1$as1$txtSearch"] = keywords
        form["ctl00$ContentPlaceHolder1$as1$rdoType"] = search_type
        form["ctl00$ContentPlaceHolder1$as1$txtExclude"] = ""
        form["ctl00$ContentPlaceHolder1$as1$hdnLastScrollPos"] = "0"
        form["ctl00$ContentPlaceHolder1$as1$hdnCountyScrollPosition"] = "-1"
        form["ctl00$ContentPlaceHolder1$as1$hdnCityScrollPosition"] = "-1"
        form["ctl00$ContentPlaceHolder1$as1$hdnPubScrollPosition"] = "-1"
        form["ctl00$ContentPlaceHolder1$as1$hdnField"] = ""
        form["ctl00$ContentPlaceHolder1$as1$dateRange"] = "rbLastNumDays"
        form["ctl00$ContentPlaceHolder1$as1$txtLastNumDays"] = "60"
        form["ctl00$ContentPlaceHolder1$as1$txtLastNumWeeks"] = "52"
        form["ctl00$ContentPlaceHolder1$as1$txtLastNumMonths"] = "12"
        return form

    def search_county(self, county):
        """
        Search for foreclosure notices in a given county.
        Uses 3-step ASP.NET postback:
          1) Select "Foreclosure" from Popular Searches dropdown
          2) Check the county checkbox (triggers postback)
          3) Click search button (btnGo)
        Returns list of dicts: {id, title, url, county}
        """
        results = []
        county_idx = COUNTY_INDICES.get(county)
        if county_idx is None:
            print(f"  ERROR: Unknown county '{county}'")
            return results

        cb_name = f"ctl00$ContentPlaceHolder1$as1$lstCounty${county_idx}"

        # --- Step 1: GET search page (follows redirect to session URL) ---
        try:
            resp = self.session.get(SEARCH_URL, timeout=30)
            resp.raise_for_status()
            self.session_url = resp.url  # e.g. /(S(xxx))/Search.aspx
        except Exception as e:
            print(f"  ERROR fetching search page: {e}")
            return results

        hidden = self.get_hidden_fields(resp.text)
        time.sleep(RATE_LIMIT)

        # --- Step 2: POST -- Select "Foreclosure" from Popular Searches ---
        form = self._build_base_form(hidden)
        form["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"
        form["__EVENTARGUMENT"] = ""
        form["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "6"

        try:
            resp = self.session.post(self.session_url, data=form, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR selecting Foreclosure: {e}")
            return results

        time.sleep(RATE_LIMIT)

        # --- Step 3: POST -- Check county checkbox (postback) ---
        hidden2 = self.get_hidden_fields(resp.text)
        form2 = self._build_base_form(hidden2, keywords="foreclosure real+estate", search_type="OR")
        form2["__EVENTTARGET"] = cb_name
        form2["__EVENTARGUMENT"] = ""
        form2["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "0"
        form2[cb_name] = "on"

        try:
            resp = self.session.post(self.session_url, data=form2, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR checking county {county}: {e}")
            return results

        time.sleep(RATE_LIMIT)

        # --- Step 4: POST -- Click search button (btnGo) ---
        hidden3 = self.get_hidden_fields(resp.text)
        form3 = self._build_base_form(hidden3, keywords="foreclosure real+estate", search_type="OR")
        form3["__EVENTTARGET"] = ""
        form3["__EVENTARGUMENT"] = ""
        form3["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "0"
        form3[cb_name] = "on"
        form3["ctl00$ContentPlaceHolder1$as1$btnGo"] = ""

        try:
            resp = self.session.post(self.session_url, data=form3, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR searching {county}: {e}")
            return results

        # --- Step 5: Parse results from all pages (capped at MAX_PAGES) ---
        page_num = 1
        while page_num <= MAX_PAGES:
            print(f"    Page {page_num}...")
            soup = BeautifulSoup(resp.text, "lxml")
            page_results = self._parse_search_results(soup, county)

            if not page_results:
                break

            results.extend(page_results)

            # Check for next page button
            next_btn = self._find_next_page_btn(soup)
            if not next_btn:
                break

            # Navigate to next page
            time.sleep(RATE_LIMIT)
            hidden_pg = self.get_hidden_fields(resp.text)
            form_pg = self._build_base_form(hidden_pg, keywords="foreclosure real+estate", search_type="OR")
            form_pg["__EVENTTARGET"] = ""
            form_pg["__EVENTARGUMENT"] = ""
            form_pg["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "0"
            form_pg[cb_name] = "on"
            form_pg[next_btn] = ""  # click next page image button

            try:
                resp = self.session.post(self.session_url, data=form_pg, timeout=30)
                resp.raise_for_status()
            except Exception as e:
                print(f"    ERROR fetching page {page_num + 1}: {e}")
                break

            page_num += 1

        if page_num > MAX_PAGES:
            print(f"    WARNING: Hit MAX_PAGES cap ({MAX_PAGES}) for {county}")

        return results

    def _parse_search_results(self, soup, county):
        """
        Parse notice listings from search results GridView.
        Extracts borrower name and DOT date from the snippet text
        shown in the sibling TR row (detail pages are CAPTCHA-protected).
        """
        results = []
        seen_ids = set()

        # Find all hdnPKValue hidden fields -- each holds a notice ID
        pk_fields = soup.find_all("input", {"name": re.compile(r"hdnPKValue")})
        for pk in pk_fields:
            notice_id = pk.get("value", "").strip()
            if not notice_id or notice_id in seen_ids:
                continue
            seen_ids.add(notice_id)

            # Get the parent row -- has publication name, date, city, county
            row = pk.find_parent("tr")
            row_text = row.get_text(separator=" ", strip=True) if row else ""
            county_match = re.search(r"County:\s*(\w+)", row_text)
            row_county = county_match.group(1) if county_match else ""
            city_match = re.search(r"City:\s*([\w\s'-]+?)(?:\s+County:|\s*$)", row_text)
            city = city_match.group(1).strip() if city_match else ""

            # Extract publication name and date from row
            pub_match = re.search(r"^(\S[\w\s&'-]+?)(?:\s*\||\s+\w+day,)", row_text)
            publication = pub_match.group(1).strip() if pub_match else ""
            date_match = re.search(
                r"(\w+day,\s+\w+\s+\d{1,2},\s+\d{4})", row_text
            )
            pub_date = date_match.group(1).strip() if date_match else ""

            url = f"{BASE_URL}/Details.aspx?ID={notice_id}"

            # Get the snippet text from the next sibling TR row
            # (contains truncated notice text with borrower name + DOT date)
            snippet = ""
            if row:
                sibling_tr = row.find_next_sibling("tr")
                if sibling_tr:
                    snippet = sibling_tr.get_text(separator=" ", strip=True)
                    # Remove "click 'view' to open the full text." suffix
                    snippet = re.sub(
                        r"\s*click\s*'view'\s+to\s+open\s+the\s+full\s+text\.\s*",
                        "", snippet, flags=re.IGNORECASE
                    ).strip()

            # Parse borrower from snippet WHEREAS clause
            borrower = ""
            if snippet:
                borrow_match = re.search(
                    r"(?:executed|given|made)\s+(?:a\s+certin\s+)?(?:deed of trust|Deed of Trust)"
                    r"|(?:WHEREAS,?\s+on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+)(.+?)(?:,?\s+executed)",
                    snippet, re.IGNORECASE | re.DOTALL
                )
                if not borrow_match or not borrow_match.group(1):
                    # Try alternate: "on [date], [NAME], executed"
                    borrow_match2 = re.search(
                        r"on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+(.+?),?\s+executed",
                        snippet, re.IGNORECASE
                    )
                    if borrow_match2:
                        borrower = borrow_match2.group(1).strip()
                else:
                    borrower = borrow_match.group(1).strip() if borrow_match.group(1) else ""
                # Clean up borrower name
                borrower = re.sub(r"\s+", " ", borrower).strip(" ,")

            # Parse DOT date from snippet
            dot_date = ""
            if snippet:
                dot_match = re.search(
                    r"WHEREAS,?\s+on\s+(\w+\s+\d{1,2},?\s+\d{4})",
                    snippet, re.IGNORECASE
                )
                if dot_match:
                    dot_date = dot_match.group(1).strip()

            results.append({
                "id": notice_id,
                "url": url,
                "county": row_county or county,
                "city": city,
                "borrower": borrower,
                "dot_date": dot_date,
                "publication": publication,
                "pub_dates": pub_date,
                "snippet": snippet[:500],
                # Fields we can't get without detail page (CAPTCHA-protected):
                "filing_info": "",
                "attorney": "",
                "auction_date": "",
                "auction_time": "",
                "auction_location": "",
                "legal_desc": "",
            })

        return results

    def _find_next_page_btn(self, soup):
        """Find the name of the Next page image button, if it exists."""
        # The GridView uses image buttons: btnNext (enabled when not on last page)
        btn = soup.find("input", {
            "name": re.compile(r"btnNext$"),
            "type": "image",
        })
        if btn and not btn.get("disabled"):
            return btn["name"]
        return None

    # NOTE: get_notice_detail() removed -- detail pages are CAPTCHA-protected
    # as of May 2026. All available data is now extracted from search result
    # snippets in _parse_search_results().

# ============================================================
# GOOGLE SHEETS HANDLER
# ============================================================

class SheetHandler:
    """Manages reading/writing to the Google Sheet."""

    def __init__(self):
        creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not creds_json:
            # Try loading from file (local dev)
            creds_path = os.path.join(os.path.dirname(__file__), "service_account.json")
            if os.path.exists(creds_path):
                with open(creds_path) as f:
                    creds_json = f.read()
            else:
                raise RuntimeError(
                    "No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON env var "
                    "or place service_account.json in the script directory."
                )

        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(SHEET_ID)

    def get_tab(self, county):
        """Get worksheet for a county tab."""
        try:
            return self.spreadsheet.worksheet(county)
        except gspread.exceptions.WorksheetNotFound:
            print(f"  WARNING: Tab '{county}' not found in sheet")
            return None

    def get_existing_urls(self, ws):
        """Get all existing notice URLs from column R to detect duplicates."""
        try:
            url_col = ws.col_values(COL["notice_url"])
            return set(u.strip() for u in url_col if u.strip())
        except Exception:
            return set()

    def get_existing_borrowers(self, ws):
        """Get all existing borrower names from column B."""
        try:
            return ws.col_values(COL["borrower"])
        except Exception:
            return []

    def find_next_empty_row(self, ws):
        """Find the first empty row after the header rows."""
        borrowers = self.get_existing_borrowers(ws)
        # Find last non-empty row in column B
        last_row = FIRST_DATA_ROW
        for i, val in enumerate(borrowers):
            if val.strip():
                last_row = i + 1  # 1-indexed
        return max(last_row + 1, FIRST_DATA_ROW)

    def append_notice(self, ws, notice, county):
        """Append a single notice as a new row."""
        next_row = self.find_next_empty_row(ws)
        today = date.today().strftime("%m/%d/%Y")

        row_data = [""] * 19  # A through S
        row_data[COL["scrape_date"] - 1] = today
        row_data[COL["borrower"] - 1] = notice.get("borrower", "")
        row_data[COL["county"] - 1] = county
        row_data[COL["dot_date"] - 1] = notice.get("dot_date", "")
        row_data[COL["filing_info"] - 1] = notice.get("filing_info", "")
        row_data[COL["attorney"] - 1] = notice.get("attorney", "")
        row_data[COL["auction_date"] - 1] = notice.get("auction_date", "")
        row_data[COL["auction_time"] - 1] = notice.get("auction_time", "")
        row_data[COL["auction_location"] - 1] = notice.get("auction_location", "")
        row_data[COL["legal_desc"] - 1] = notice.get("legal_desc", "")
        row_data[COL["pub_dates"] - 1] = notice.get("pub_dates", "")
        row_data[COL["notice_url"] - 1] = notice.get("url", "")

        try:
            ws.update(values=[row_data], range_name=f"A{next_row}:S{next_row}", value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            print(f"    ERROR writing row {next_row}: {e}")
            return False

    def update_summary(self, county_counts):
        """Update the Summary tab with latest counts."""
        try:
            summary = self.spreadsheet.worksheet("Summary")
            # Update is county-specific -- adjust cell references to match your layout
            print("  Summary tab update -- adjust cell references in code to match your layout")
        except Exception as e:
            print(f"  WARNING: Could not update Summary tab: {e}")

# ============================================================
# EMAIL NOTIFICATION
# ============================================================

def send_email_report(new_notices, errors):
    """Send email summary of scrape results."""
    email_to = os.environ.get("NOTIFY_EMAIL", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([email_to, smtp_user, smtp_pass]):
        print("  Email credentials not configured -- skipping notification")
        return

    today = date.today().strftime("%m/%d/%Y")
    total = sum(len(v) for v in new_notices.values())

    subject = f"SPU Foreclosure Scrape -- {today} -- {total} new notices"

    body_lines = [
        f"MS Public Notices Scrape Report",
        f"Date: {today}",
        f"Total new notices found: {total}",
        "",
        "BY COUNTY:",
        "=" * 40,
    ]

    for county in COUNTIES:
        notices = new_notices.get(county, [])
        body_lines.append(f"\n{county}: {len(notices)} new")
        for n in notices:
            body_lines.append(f"  - {n.get('borrower', 'Unknown')} | {n.get('url', '')}")

    if errors:
        body_lines.append("\n\nERRORS:")
        body_lines.append("=" * 40)
        for err in errors:
            body_lines.append(f"  - {err}")

    body_lines.append(f"\n\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")

    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            print(f"  Email sent to {email_to}")
    except Exception as e:
        print(f"  ERROR sending email: {e}")

# ============================================================
# MAIN PIPELINE
# ============================================================

def run_pipeline():
    """Main entry point -- scrape all counties, update sheet, send report."""
    print(f"{'=' * 60}")
    print(f"SPU MS Public Notices Scraper")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    scraper = MSNoticesScraper()
    errors = []

    # Initialize Google Sheets
    try:
        sheets = SheetHandler()
        print("Google Sheets connected.")
    except Exception as e:
        print(f"FATAL: Cannot connect to Google Sheets: {e}")
        traceback.print_exc()
        sys.exit(1)

    new_notices = {}

    for county in COUNTIES:
        print(f"\n--- {county} County ---")

        # Get the worksheet tab
        ws = sheets.get_tab(county)
        if not ws:
            errors.append(f"{county}: tab not found")
            continue

        # Get existing URLs to skip duplicates
        existing_urls = sheets.get_existing_urls(ws)
        print(f"  Existing entries: {len(existing_urls)}")

        # Scrape notices
        try:
            listings = scraper.search_county(county)
        except Exception as e:
            msg = f"{county}: search failed -- {e}"
            print(f"  ERROR: {msg}")
            errors.append(msg)
            continue

        print(f"  Found {len(listings)} total listings")

        # Filter out duplicates
        new_listings = [
            l for l in listings
            if l["url"] not in existing_urls
        ]
        print(f"  New (not in sheet): {len(new_listings)}")

        if not new_listings:
            new_notices[county] = []
            continue

        # Write new notices to sheet
        # Note: Detail pages are CAPTCHA-protected as of May 2026.
        # Borrower + DOT date are parsed from search result snippets.
        # Detail URL is stored so notices can be reviewed manually.
        county_new = []
        for i, listing in enumerate(new_listings):
            print(f"    [{i+1}/{len(new_listings)}] {listing.get('borrower', 'Unknown')[:50]}")

            # Write to sheet (data already extracted from search results)
            success = sheets.append_notice(ws, listing, county)
            if success:
                county_new.append(listing)
            else:
                errors.append(f"{county}: failed to write {listing['id']}")

        new_notices[county] = county_new
        print(f"  Added {len(county_new)} new entries to sheet")

        # Respect rate limits between counties
        time.sleep(2)

    # Summary
    total_new = sum(len(v) for v in new_notices.values())
    print(f"\n{'=' * 60}")
    print(f"COMPLETE -- {total_new} new notices added")
    for county in COUNTIES:
        count = len(new_notices.get(county, []))
        if count:
            print(f"  {county}: {count}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    print(f"{'=' * 60}")

    # Send email report
    send_email_report(new_notices, errors)

    # Exit with error code if there were critical failures
    if errors and total_new == 0:
        sys.exit(1)

if __name__ == "__main__":
    run_pipeline()

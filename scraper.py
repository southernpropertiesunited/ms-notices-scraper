#!/usr/bin/env python3
"""
SPU MS Public Notices Scraper
=============================
Scrapes foreclosure notices from mspublicnotices.org for target MS counties.
Parses borrower info, DOT dates, attorneys, auction details.
Pushes new entries to Google Sheet, skips duplicates.

Runs via GitHub Actions on Mon/Wed/Fri at 6AM CT — zero AI charges.

SETUP (one-time):
1. Create a Google Cloud project at https://console.cloud.google.com
2. Enable Google Sheets API + Google Drive API
3. Create a Service Account, download JSON key
4. Share your Google Sheet with the service account email (Editor access)
5. In your GitHub repo Settings > Secrets, add:
   - GOOGLE_SERVICE_ACCOUNT_JSON  (paste entire JSON key contents)
   - NOTIFY_EMAIL                 (paul@southernpropertiesunited.com)
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
SHEET_ID = "1QOZXHBHK1w8AoX6fFL4pWwO6v46HgFBh"
BASE_URL = "https://www.mspublicnotices.org"
SEARCH_URL = f"{BASE_URL}/Search.aspx"

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

    def get_viewstate(self, html):
        """Extract ASP.NET ViewState fields from page HTML."""
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
            tag = soup.find("input", {"name": name})
            if tag:
                fields[name] = tag.get("value", "")
        return fields

    def search_county(self, county, notice_type="Foreclosure"):
        """
        Search for notices in a given county.
        Returns list of dicts: {id, title, url, pub_date}
        """
        results = []

        # Step 1: GET search page to grab ViewState
        try:
            resp = self.session.get(SEARCH_URL, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR fetching search page: {e}")
            return results

        vs = self.get_viewstate(resp.text)
        time.sleep(RATE_LIMIT)

        # Step 2: POST search form with county + type filters
        form_data = {
            **vs,
            "ctl00$ContentPlaceHolder1$as1$txtSearch": "",
            "ctl00$ContentPlaceHolder1$as1$ddlCounty": county,
            "ctl00$ContentPlaceHolder1$as1$ddlNoticeType": notice_type,
            "ctl00$ContentPlaceHolder1$as1$btnSearch": "Search",
        }

        try:
            resp = self.session.post(SEARCH_URL, data=form_data, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR posting search for {county}: {e}")
            return results

        # Step 3: Parse results from all pages
        page_num = 1
        while True:
            print(f"    Page {page_num}...")
            soup = BeautifulSoup(resp.text, "lxml")
            page_results = self._parse_search_results(soup)

            if not page_results:
                break

            results.extend(page_results)

            # Check for next page link
            next_link = self._find_next_page(soup)
            if not next_link:
                break

            # Navigate to next page
            time.sleep(RATE_LIMIT)
            vs = self.get_viewstate(resp.text)
            form_data = {
                **vs,
                "__EVENTTARGET": next_link,
                "__EVENTARGUMENT": "",
            }
            try:
                resp = self.session.post(SEARCH_URL, data=form_data, timeout=30)
                resp.raise_for_status()
            except Exception as e:
                print(f"  ERROR fetching page {page_num + 1}: {e}")
                break

            page_num += 1

        return results

    def _parse_search_results(self, soup):
        """Parse notice listings from search results page."""
        results = []

        # Look for result links — format: Details.aspx?ID=XXXXXX
        links = soup.find_all("a", href=re.compile(r"Details\.aspx\?ID=\d+"))
        for link in links:
            href = link.get("href", "")
            match = re.search(r"ID=(\d+)", href)
            if not match:
                continue

            notice_id = match.group(1)
            title = link.get_text(strip=True)
            url = f"{BASE_URL}/Details.aspx?ID={notice_id}"

            results.append({
                "id": notice_id,
                "title": title,
                "url": url,
            })

        return results

    def _find_next_page(self, soup):
        """Find the EventTarget for the next page link."""
        # Look for pager controls — typically "Next" or ">" links
        pager = soup.find("tr", class_="pager") or soup.find("div", class_="pager")
        if not pager:
            # Try finding page number links
            page_links = soup.find_all("a", href=re.compile(r"__doPostBack.*Page"))
            if page_links:
                # Get the last link which is typically "Next"
                for link in page_links:
                    text = link.get_text(strip=True)
                    if text in [">", "Next", "..."]:
                        href = link.get("href", "")
                        match = re.search(r"__doPostBack\('([^']+)'", href)
                        if match:
                            return match.group(1)
            return None

        # Inside pager, find next page link
        links = pager.find_all("a")
        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href", "")
            if text in [">", "Next", "..."]:
                match = re.search(r"__doPostBack\('([^']+)'", href)
                if match:
                    return match.group(1)

        return None

    def get_notice_detail(self, url):
        """
        Fetch and parse a single notice detail page.
        Returns dict with all extracted fields.
        """
        time.sleep(RATE_LIMIT)

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    ERROR fetching {url}: {e}")
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(separator="\n")

        detail = {
            "url": url,
            "raw_text": text,
            "borrower": "",
            "dot_date": "",
            "filing_info": "",
            "attorney": "",
            "auction_date": "",
            "auction_time": "",
            "auction_location": "",
            "legal_desc": "",
            "pub_dates": "",
        }

        # --- Parse borrower names ---
        # Common patterns: "executed by [NAMES]" or "given by [NAMES]" or "made by [NAMES]"
        borrow_match = re.search(
            r"(?:executed|given|made)\s+by\s+(.+?)(?:\s*,\s*(?:to|unto|in favor)|\s+to\s+)",
            text, re.IGNORECASE | re.DOTALL
        )
        if borrow_match:
            borrower = borrow_match.group(1).strip()
            borrower = re.sub(r"\s+", " ", borrower)  # collapse whitespace
            detail["borrower"] = borrower

        # --- Parse Deed of Trust date ---
        dot_match = re.search(
            r"(?:Deed of Trust|DOT|deed of trust)\s+dated\s+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
            text, re.IGNORECASE
        )
        if dot_match:
            detail["dot_date"] = dot_match.group(1).strip()

        # --- Parse filing info (Book/Page or Instrument#) ---
        filing_match = re.search(
            r"(?:recorded\s+in|filed\s+(?:for record\s+)?in|recorded\s+as)\s+(.+?)(?:\.\s|\sin\sthe)",
            text, re.IGNORECASE
        )
        if filing_match:
            detail["filing_info"] = filing_match.group(1).strip()[:100]

        # Also try Book/Page pattern
        if not detail["filing_info"]:
            bp_match = re.search(r"Book\s+(\d+)\s*,?\s*(?:at\s+)?Page\s+(\d+)", text, re.IGNORECASE)
            if bp_match:
                detail["filing_info"] = f"Book {bp_match.group(1)}, Page {bp_match.group(2)}"

        # Instrument number pattern
        if not detail["filing_info"]:
            inst_match = re.search(r"Instrument\s*#?\s*(\d+)", text, re.IGNORECASE)
            if inst_match:
                detail["filing_info"] = f"Instrument# {inst_match.group(1)}"

        # --- Parse foreclosure attorney ---
        atty_patterns = [
            r"(?:Substitute\s+Trustee|Trustee)\s*[:\-]?\s*(.+?)(?:\n|,\s*(?:Attorney|PLLC|LLC|P\.?A\.?))",
            r"(?:Attorney\s+for\s+|Counsel\s+for\s+)(?:Trustee|Mortgagee)\s*[:\-]?\s*(.+?)(?:\n|\d{3})",
        ]
        for pattern in atty_patterns:
            atty_match = re.search(pattern, text, re.IGNORECASE)
            if atty_match:
                detail["attorney"] = atty_match.group(1).strip()[:150]
                break

        # Fallback: look for common MS foreclosure law firms
        if not detail["attorney"]:
            firms = [
                "Shapiro & Ingle", "McCalla Raymer", "Albertelli Law",
                "Mackie Wolf", "Underwood Law", "Dean Morris",
                "Sirote & Permutt", "Halliday Watkins", "Wilson & Associates",
                "Substitute Trustee Services", "Logs Legal Group",
            ]
            for firm in firms:
                if firm.lower() in text.lower():
                    # Get the full line
                    for line in text.split("\n"):
                        if firm.lower() in line.lower():
                            detail["attorney"] = line.strip()[:150]
                            break
                    break

        # --- Parse auction/sale date, time, location ---
        sale_patterns = [
            r"(?:sale|sell|sold)\s+(?:to the highest bidder\s+)?(?:on|at)\s+(\w+\s*,?\s*\w+\s+\d{1,2}\s*,?\s+\d{4})",
            r"(\w+\s+\d{1,2}\s*,?\s+\d{4})\s*,?\s+(?:at\s+)?(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)",
            r"(?:date of sale|sale date)\s*[:\-]?\s*(\w+\s+\d{1,2}\s*,?\s+\d{4})",
        ]
        for pattern in sale_patterns:
            sale_match = re.search(pattern, text, re.IGNORECASE)
            if sale_match:
                detail["auction_date"] = sale_match.group(1).strip()
                if sale_match.lastindex and sale_match.lastindex >= 2:
                    detail["auction_time"] = sale_match.group(2).strip()
                break

        # Time pattern if not caught above
        if not detail["auction_time"]:
            time_match = re.search(
                r"(?:at|@)\s+(\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?)?)\s*(?:o'clock)?",
                text, re.IGNORECASE
            )
            if time_match:
                detail["auction_time"] = time_match.group(1).strip()

        # Location — usually county courthouse
        loc_patterns = [
            r"(?:front door|south door|north door|east door|west door|steps)\s+of\s+(?:the\s+)?(.+?)(?:County|courthouse)",
            r"(Hinds|Rankin|Harrison|Jackson|George|Hancock|Stone)\s+County\s+Courthouse",
            r"(?:at the|at)\s+(.+?Courthouse.+?)(?:\.|,\s*(?:being|in the))",
        ]
        for pattern in loc_patterns:
            loc_match = re.search(pattern, text, re.IGNORECASE)
            if loc_match:
                detail["auction_location"] = loc_match.group(0).strip()[:200]
                break

        # --- Parse legal description ---
        legal_patterns = [
            r"(?:following\s+described\s+(?:property|real property|land|real estate))\s*[:\-]?\s*(.+?)(?:SAID|Said|WITNESS|Witness|This\s+(?:the|sale)|SUBJECT)",
            r"(?:property\s+described\s+as)\s*[:\-]?\s*(.+?)(?:\.|SAID|Said|subject)",
        ]
        for pattern in legal_patterns:
            legal_match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if legal_match:
                desc = legal_match.group(1).strip()
                desc = re.sub(r"\s+", " ", desc)  # collapse whitespace
                detail["legal_desc"] = desc[:500]
                break

        # --- Parse publication dates ---
        pub_match = re.search(
            r"(?:Published|Publication|Publish)\s*(?:dates?)?\s*[:\-]?\s*(.+?)(?:\n\n|\Z)",
            text, re.IGNORECASE
        )
        if pub_match:
            detail["pub_dates"] = pub_match.group(1).strip()[:200]

        return detail


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
            ws.update(f"A{next_row}:S{next_row}", [row_data], value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            print(f"    ERROR writing row {next_row}: {e}")
            return False

    def update_summary(self, county_counts):
        """Update the Summary tab with latest counts."""
        try:
            summary = self.spreadsheet.worksheet("Summary")
            # Update is county-specific — adjust cell references to match your layout
            print("  Summary tab update — adjust cell references in code to match your layout")
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
        print("  Email credentials not configured — skipping notification")
        return

    today = date.today().strftime("%m/%d/%Y")
    total = sum(len(v) for v in new_notices.values())

    subject = f"SPU Foreclosure Scrape — {today} — {total} new notices"

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
    """Main entry point — scrape all counties, update sheet, send report."""
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
            msg = f"{county}: search failed — {e}"
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

        # Fetch detail pages for new notices
        county_new = []
        for i, listing in enumerate(new_listings):
            print(f"  [{i+1}/{len(new_listings)}] Fetching {listing['id']}...")

            detail = scraper.get_notice_detail(listing["url"])
            if not detail:
                errors.append(f"{county}: failed to fetch notice {listing['id']}")
                continue

            # If borrower wasn't parsed from detail, use listing title
            if not detail["borrower"] and listing.get("title"):
                detail["borrower"] = listing["title"]

            # Write to sheet
            success = sheets.append_notice(ws, detail, county)
            if success:
                county_new.append(detail)
                print(f"    Added: {detail['borrower'][:50]}")
            else:
                errors.append(f"{county}: failed to write {listing['id']}")

        new_notices[county] = county_new
        print(f"  Added {len(county_new)} new entries to sheet")

        # Respect rate limits between counties
        time.sleep(2)

    # Summary
    total_new = sum(len(v) for v in new_notices.values())
    print(f"\n{'=' * 60}")
    print(f"COMPLETE — {total_new} new notices added")
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

#!/usr/bin/env python3
"""
SPU MS Public Notices AI Command Center — Scraper
==================================================
Scrapes foreclosure notices from mspublicnotices.org for 7 MS counties.
Parses ALL available data from search result snippets (detail pages are
CAPTCHA-protected). Pushes to Google Sheet with dedup, sorting, archival,
run logging, and email notifications.

Runs via GitHub Actions Mon/Wed/Fri at 6 AM CT.
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
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================================
# CONFIGURATION
# ============================================================

COUNTIES = ["George", "Hancock", "Harrison", "Hinds", "Jackson", "Rankin", "Stone"]
SHEET_ID = "1cgGpocIQBdP_39tuI3Pqy4xJud7vZ2yCrHr4_PBH0Ro"
BASE_URL = "https://www.mspublicnotices.org"
SEARCH_URL = f"{BASE_URL}/Search.aspx"

COUNTY_INDICES = {
    "George": 19, "Hancock": 22, "Harrison": 23,
    "Hinds": 24, "Jackson": 29, "Rankin": 60, "Stone": 65,
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column layout — 19 columns A-S
COL = {
    "scrape_date": 1,       # A
    "borrower": 2,          # B
    "county": 3,            # C
    "phone": 4,             # D
    "alt_phone": 5,         # E
    "mailing_address": 6,   # F
    "property_address": 7,  # G
    "parcel_id": 8,         # H
    "dot_date": 9,          # I
    "filing_info": 10,      # J
    "attorney": 11,         # K
    "auction_date": 12,     # L
    "auction_time": 13,     # M
    "auction_location": 14, # N
    "legal_desc": 15,       # O
    "pub_dates": 16,        # P
    "notice_url": 17,       # Q
    "notice_id": 18,        # R
    "assessed_value": 19,   # S
}

HEADERS = [
    "Scrape Date", "Borrower", "County", "Phone", "Alt Phone",
    "Mailing Address", "Property Address", "Parcel ID", "DOT Date",
    "Filing Info", "Attorney/Trustee", "Auction Date", "Auction Time",
    "Auction Location", "Legal Description", "Publication Dates",
    "Notice URL", "Notice ID", "Assessed Value"
]

FIRST_DATA_ROW = 7
RATE_LIMIT = 1.5
MAX_PAGES = 20

# ============================================================
# SNIPPET PARSER — Extracts everything possible from search snippets
# ============================================================

def parse_snippet(snippet, notice_id=""):
    """
    Aggressively parse a foreclosure notice snippet to extract all
    available fields. MS foreclosure notices follow a standard format:

    'WHEREAS, on [DOT_DATE], [BORROWER], executed a Deed of Trust to
    [TRUSTEE]... recorded in [BOOK/INSTRUMENT]... [COUNTY] County...
    I, [ATTORNEY], the duly appointed Substitute Trustee... will offer
    for sale... on [AUCTION_DATE], [at/between] [AUCTION_TIME]...
    at [AUCTION_LOCATION]...'
    """
    data = {
        "borrower": "",
        "dot_date": "",
        "filing_info": "",
        "attorney": "",
        "auction_date": "",
        "auction_time": "",
        "auction_location": "",
        "legal_desc": "",
        "property_address": "",
    }

    if not snippet:
        return data

    s = snippet

    # --- BORROWER ---
    # Pattern: "on [date], [BORROWER], executed a Deed of Trust"
    # Also: "on [date], [BORROWER] executed" (no comma before executed)
    borrow_patterns = [
        r"(?:WHEREAS,?\s+)?on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+(.+?),?\s+executed\s+(?:a\s+)?(?:certain\s+)?(?:deed\s+of\s+trust|Deed\s+of\s+Trust)",
        r"(?:WHEREAS,?\s+)?on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+(.+?)\s+executed\s+(?:a\s+)?(?:deed\s+of\s+trust|Deed\s+of\s+Trust)",
        r"(?:WHEREAS,?\s+)?on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+(.+?),?\s+(?:did\s+)?(?:execute|made\s+and\s+deliver)",
        r"(?:that\s+)?(\S.+?)\s+executed\s+(?:a\s+)?(?:certain\s+)?deed\s+of\s+trust",
    ]
    for pat in borrow_patterns:
        m = re.search(pat, s, re.IGNORECASE | re.DOTALL)
        if m and m.group(1):
            borrower = m.group(1).strip()
            borrower = re.sub(r"\s+", " ", borrower).strip(" ,;")
            # Cap at 80 chars to avoid grabbing too much
            if len(borrower) <= 80:
                data["borrower"] = borrower
                break

    # --- DOT DATE ---
    dot_patterns = [
        r"WHEREAS,?\s+on\s+(\w+\s+\d{1,2},?\s+\d{4})",
        r"on\s+(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+\w+,?\s+\d{4})",
        r"on\s+(\w+\s+\d{1,2},?\s+\d{4}),?\s+\w",
    ]
    for pat in dot_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            data["dot_date"] = m.group(1).strip()
            break

    # --- FILING INFO ---
    # "recorded in Book XXX, Page YYY" or "Instrument No. XXXX"
    filing_patterns = [
        r"recorded\s+in\s+(Book\s+\d+[\w\s,]+?Page\s+\d+)",
        r"recorded\s+(?:at\s+)?(?:in\s+)?(Instrument\s+(?:No\.?\s*)?\d[\d\-]+)",
        r"(?:Book|Bk\.?)\s+(\d+)\s*,?\s*(?:Page|Pg\.?|at\s+Page)\s+(\d+)",
        r"(Instrument\s+(?:#|No\.?|Number)?\s*\d[\d\-]+)",
    ]
    for pat in filing_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            if m.lastindex and m.lastindex >= 2:
                data["filing_info"] = f"Book {m.group(1)}, Page {m.group(2)}"
            else:
                data["filing_info"] = m.group(1).strip() if m.group(1) else m.group(0).strip()
            break

    # --- ATTORNEY / SUBSTITUTE TRUSTEE ---
    atty_patterns = [
        r"(?:I,|undersigned)\s+([A-Z][A-Za-z\s\.,']+?),?\s+(?:the\s+)?(?:duly\s+)?(?:appointed\s+)?Substitute\s+Trustee",
        r"(?:appointed|named)\s+([A-Z][A-Za-z\s\.,']+?)\s+as\s+(?:the\s+)?Substitute\s+Trustee",
        r"Substitute\s+Trustee[\s:]+([A-Z][A-Za-z\s\.,']+?)(?:\s*,|\s+will|\s+does)",
        r"(?:TRUSTEE|Trustee)[\s:,]+([A-Z][A-Za-z\s\.,']{5,50}?)(?:\s*,|\s+\d|\s+will)",
    ]
    for pat in atty_patterns:
        m = re.search(pat, s, re.DOTALL)
        if m:
            atty = m.group(1).strip().strip(",. ")
            atty = re.sub(r"\s+", " ", atty)
            if len(atty) <= 60 and len(atty) >= 3:
                data["attorney"] = atty
                break

    # --- AUCTION DATE ---
    # "will sell...on [DATE]" or "sale will be held on [DATE]"
    # Also: "on the Xth day of Month, Year"
    auction_patterns = [
        r"(?:sell|sale|sold)\s+(?:at\s+public\s+(?:outcry|auction|sale)\s+)?.*?on\s+(?:the\s+)?(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:sell|sale|sold)\s+.*?on\s+(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+\w+,?\s+\d{4})",
        r"(?:sale\s+(?:date|will\s+be\s+held))[\s:]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:on|dated?)\s+(\w+\s+\d{1,2},?\s+\d{4})\s*,?\s*(?:at|between)\s+\d{1,2}:\d{2}",
    ]
    for pat in auction_patterns:
        m = re.search(pat, s, re.IGNORECASE | re.DOTALL)
        if m:
            auction_str = m.group(1).strip()
            # Make sure this isn't the DOT date (check it's a different date)
            if auction_str != data["dot_date"]:
                data["auction_date"] = auction_str
                break

    # If we found an auction date in "Xth day of Month, Year" format, normalize it
    if data["auction_date"]:
        day_of_match = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+(\w+),?\s+(\d{4})", data["auction_date"])
        if day_of_match:
            data["auction_date"] = f"{day_of_match.group(2)} {day_of_match.group(1)}, {day_of_match.group(3)}"

    # --- AUCTION TIME ---
    time_patterns = [
        r"(?:at|between)\s+(\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.|a\.m\.|p\.m\.|o'clock|noon))",
        r"(?:at|between)\s+(\d{1,2}:\d{2})\s+(?:o'clock\s+)?(?:in\s+the\s+)?(morning|afternoon|noon)",
        r"(?:at\s+)?(\d{1,2}:\d{2})\s*(?:AM|PM|A\.M\.|P\.M\.)",
        r"(?:at\s+)?(\d{1,2}\s*(?:AM|PM|A\.M\.|P\.M\.))\b",
    ]
    for pat in time_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            time_str = m.group(1).strip()
            if m.lastindex >= 2 and m.group(2):
                time_str += " " + m.group(2)
            data["auction_time"] = time_str.upper().replace(".", "")
            break

    # --- AUCTION LOCATION ---
    loc_patterns = [
        r"(?:front|south|north|east|west|main)\s+(?:door|steps?|entrance)\s+of\s+(?:the\s+)?(.+?)(?:County\s+Courthouse)(?:\s+(?:in|at|,)\s+([A-Za-z\s]+),?\s*(?:Mississippi|MS))?",
        r"(?:at\s+the\s+)(.+?County\s+Courthouse.+?)(?:\s+on\s+|\s+at\s+\d|\s*,\s*(?:I|the|said|being))",
        r"(?:at\s+the\s+)([\w\s]+?Courthouse[\w\s,]*?(?:Mississippi|MS))",
        r"(?:at\s+the\s+)([\w\s]+?County\s+Courthouse)",
    ]
    for pat in loc_patterns:
        m = re.search(pat, s, re.IGNORECASE | re.DOTALL)
        if m:
            loc = m.group(0).strip()
            # Clean up — start from "at the" or the door description
            loc = re.sub(r"^(?:at\s+the\s+)", "", loc, flags=re.IGNORECASE).strip()
            loc = re.sub(r"\s+", " ", loc)
            if len(loc) <= 120:
                data["auction_location"] = loc
                break

    # --- PROPERTY / LEGAL DESCRIPTION ---
    legal_patterns = [
        r"(?:described\s+as|to[\s-]wit|property[\s:]+)([\s\S]{10,200}?)(?:\s+(?:WHEREAS|NOW|said\s+(?:deed|property|Deed)|I,\s+\w))",
        r"(Lot\s+\d+[\w\s,]+?(?:Section|Block|Township|Range|Addition|Subdivision)[\w\s,\.]+?)(?:\s+(?:in|of|WHEREAS|NOW|said))",
    ]
    for pat in legal_patterns:
        m = re.search(pat, s, re.IGNORECASE | re.DOTALL)
        if m:
            legal = m.group(1).strip().strip(",. ::")
            legal = re.sub(r"\s+", " ", legal)
            if len(legal) >= 10 and len(legal) <= 200:
                data["legal_desc"] = legal
                break

    return data


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
        self.session_url = None

    def get_hidden_fields(self, html):
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if name:
                fields[name] = inp.get("value", "")
        return fields

    def _build_base_form(self, hidden, keywords="", search_type="AND"):
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
        results = []
        county_idx = COUNTY_INDICES.get(county)
        if county_idx is None:
            print(f"  ERROR: Unknown county '{county}'")
            return results

        cb_name = f"ctl00$ContentPlaceHolder1$as1$lstCounty${county_idx}"

        # Step 1: GET search page
        try:
            resp = self.session.get(SEARCH_URL, timeout=30)
            resp.raise_for_status()
            self.session_url = resp.url
        except Exception as e:
            print(f"  ERROR fetching search page: {e}")
            return results

        hidden = self.get_hidden_fields(resp.text)
        time.sleep(RATE_LIMIT)

        # Step 2: Select Foreclosure
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

        # Step 3: Check county checkbox
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

        # Step 4: Click search
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

        # Step 5: Parse all pages
        page_num = 1
        while page_num <= MAX_PAGES:
            print(f"    Page {page_num}...")
            soup = BeautifulSoup(resp.text, "lxml")
            page_results = self._parse_search_results(soup, county)

            if not page_results:
                break

            results.extend(page_results)

            next_btn = self._find_next_page_btn(soup)
            if not next_btn:
                break

            time.sleep(RATE_LIMIT)
            hidden_pg = self.get_hidden_fields(resp.text)
            form_pg = self._build_base_form(hidden_pg, keywords="foreclosure real+estate", search_type="OR")
            form_pg["__EVENTTARGET"] = ""
            form_pg["__EVENTARGUMENT"] = ""
            form_pg["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "0"
            form_pg[cb_name] = "on"
            form_pg[next_btn] = ""

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
        results = []
        seen_ids = set()

        pk_fields = soup.find_all("input", {"name": re.compile(r"hdnPKValue")})
        for pk in pk_fields:
            notice_id = pk.get("value", "").strip()
            if not notice_id or notice_id in seen_ids:
                continue
            seen_ids.add(notice_id)

            row = pk.find_parent("tr")
            row_text = row.get_text(separator=" ", strip=True) if row else ""

            # Extract county/city from row header
            county_match = re.search(r"County:\s*(\w+)", row_text)
            row_county = county_match.group(1) if county_match else ""
            city_match = re.search(r"City:\s*([\w\s'-]+?)(?:\s+County:|\s+$)", row_text)
            city = city_match.group(1).strip() if city_match else ""

            # Publication info
            pub_match = re.search(r"^(\S[\w\s&'-]+?)(?:\s*\||\s+\w+day,)", row_text)
            publication = pub_match.group(1).strip() if pub_match else ""
            date_match = re.search(r"(\w+day,\s+\w+\s+\d{1,2},\s+\d{4})", row_text)
            pub_date = date_match.group(1).strip() if date_match else ""

            # Build URL with notice ID (not session token)
            url = f"{BASE_URL}/Details.aspx?ID={notice_id}"

            # Get FULL snippet text (no truncation during parsing)
            snippet = ""
            if row:
                sibling_tr = row.find_next_sibling("tr")
                if sibling_tr:
                    snippet = sibling_tr.get_text(separator=" ", strip=True)
                    snippet = re.sub(
                        r"\s*click\s+'view'\s+to\s+open\s+the\s+full\s+text\.\s*",
                        "", snippet, flags=re.IGNORECASE
                    ).strip()

            # Parse everything we can from the snippet
            parsed = parse_snippet(snippet, notice_id)

            results.append({
                "id": notice_id,
                "url": url,
                "county": row_county or county,
                "city": city,
                "borrower": parsed["borrower"],
                "dot_date": parsed["dot_date"],
                "publication": publication,
                "pub_dates": pub_date,
                "snippet": snippet[:500],
                "filing_info": parsed["filing_info"],
                "attorney": parsed["attorney"],
                "auction_date": parsed["auction_date"],
                "auction_time": parsed["auction_time"],
                "auction_location": parsed["auction_location"],
                "legal_desc": parsed["legal_desc"],
                "property_address": parsed.get("property_address", ""),
            })

        return results

    def _find_next_page_btn(self, soup):
        btn = soup.find("input", {
            "name": re.compile(r"btnNext$"),
            "type": "image",
        })
        if btn and not btn.get("disabled"):
            return btn["name"]
        return None


# ============================================================
# GOOGLE SHEETS HANDLER
# ============================================================

class SheetHandler:
    def __init__(self):
        creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not creds_json:
            creds_path = os.path.join(os.path.dirname(__file__), "service_account.json")
            if os.path.exists(creds_path):
                with open(creds_path) as f:
                    creds_json = f.read()
            else:
                raise RuntimeError("No Google credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON.")

        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(SHEET_ID)

    def get_tab(self, county):
        try:
            return self.spreadsheet.worksheet(county)
        except gspread.exceptions.WorksheetNotFound:
            print(f"  WARNING: Tab '{county}' not found")
            return None

    # --- DEDUP by Notice ID (column R) ---
    def get_existing_notice_ids(self, ws):
        """Get all existing notice IDs from column R for dedup."""
        try:
            id_col = ws.col_values(COL["notice_id"])
            ids = set()
            for val in id_col:
                val = val.strip()
                if val and val.lower() != "notice id":
                    ids.add(val)
            return ids
        except Exception:
            return set()

    def get_existing_ids_from_urls(self, ws):
        """Fallback: extract notice IDs from URL column for backward compat."""
        try:
            url_col = ws.col_values(COL["notice_url"])
            ids = set()
            for url in url_col:
                m = re.search(r"ID=(\d+)", url)
                if m:
                    ids.add(m.group(1))
            return ids
        except Exception:
            return set()

    def get_all_existing_ids(self, ws):
        """Get notice IDs from both ID column and URL column."""
        ids = self.get_existing_notice_ids(ws)
        ids.update(self.get_existing_ids_from_urls(ws))
        return ids

    def find_next_empty_row(self, ws):
        """Find first empty row after headers."""
        try:
            all_vals = ws.col_values(COL["borrower"])
            last_row = FIRST_DATA_ROW
            for i, val in enumerate(all_vals):
                if val.strip():
                    last_row = i + 1
            return max(last_row + 1, FIRST_DATA_ROW)
        except Exception:
            return FIRST_DATA_ROW

    def batch_append_notices(self, ws, notices, county):
        """Batch-write all notices in a single API call."""
        if not notices:
            return []

        start_row = self.find_next_empty_row(ws)
        today = date.today().strftime("%m/%d/%Y")
        all_rows = []

        for notice in notices:
            row_data = [""] * 19
            row_data[COL["scrape_date"] - 1] = today
            row_data[COL["borrower"] - 1] = notice.get("borrower", "")
            row_data[COL["county"] - 1] = county
            # phone, alt_phone left blank (skip trace Phase 2)
            # mailing_address left blank (skip trace Phase 2)
            row_data[COL["property_address"] - 1] = notice.get("property_address", "")
            # parcel_id left blank (land records Phase 2)
            row_data[COL["dot_date"] - 1] = notice.get("dot_date", "")
            row_data[COL["filing_info"] - 1] = notice.get("filing_info", "")
            row_data[COL["attorney"] - 1] = notice.get("attorney", "")
            row_data[COL["auction_date"] - 1] = notice.get("auction_date", "")
            row_data[COL["auction_time"] - 1] = notice.get("auction_time", "")
            row_data[COL["auction_location"] - 1] = notice.get("auction_location", "")
            row_data[COL["legal_desc"] - 1] = notice.get("legal_desc", "")
            row_data[COL["pub_dates"] - 1] = notice.get("pub_dates", "")
            row_data[COL["notice_url"] - 1] = notice.get("url", "")
            row_data[COL["notice_id"] - 1] = notice.get("id", "")
            all_rows.append(row_data)

        end_row = start_row + len(all_rows) - 1
        range_name = f"A{start_row}:S{end_row}"

        try:
            ws.update(values=all_rows, range_name=range_name, value_input_option="USER_ENTERED")
            print(f"  Batch wrote {len(all_rows)} rows ({range_name})")
            return notices
        except Exception as e:
            print(f"    ERROR batch writing {range_name}: {e}")
            return []

    # --- SORTING by Auction Date (soonest first) ---
    def sort_by_auction_date(self, ws):
        """Sort all data rows by auction date ascending (soonest at top).
        Rows with no auction date go to the bottom."""
        try:
            all_data = ws.get_all_values()
        except Exception as e:
            print(f"    ERROR reading for sort: {e}")
            return

        if len(all_data) <= FIRST_DATA_ROW:
            return

        header_rows = all_data[:FIRST_DATA_ROW - 1]
        data_rows = [r for r in all_data[FIRST_DATA_ROW - 1:] if any(c.strip() for c in r)]

        if not data_rows:
            return

        def parse_date_for_sort(row):
            """Parse auction date for sorting. Returns (has_date, date_obj)."""
            auction_str = row[COL["auction_date"] - 1].strip() if len(row) >= COL["auction_date"] else ""
            if not auction_str:
                return (1, datetime(2099, 12, 31))  # No date = bottom

            for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
                try:
                    dt = datetime.strptime(auction_str, fmt)
                    return (0, dt)
                except ValueError:
                    continue
            return (1, datetime(2099, 12, 31))

        data_rows.sort(key=parse_date_for_sort)

        # Pad rows to 19 cols
        for i, row in enumerate(data_rows):
            if len(row) < 19:
                data_rows[i] = row + [""] * (19 - len(row))
            elif len(row) > 19:
                data_rows[i] = row[:19]

        # Write back sorted data
        start = FIRST_DATA_ROW
        end = start + len(data_rows) - 1
        try:
            ws.update(values=data_rows, range_name=f"A{start}:S{end}", value_input_option="USER_ENTERED")
            # Clear any leftover rows below
            total_rows = ws.row_count
            if end + 1 <= total_rows:
                leftover = total_rows - end
                if leftover > 0 and leftover <= 500:
                    blank_rows = [[""] * 19] * leftover
                    ws.update(values=blank_rows, range_name=f"A{end+1}:S{end+leftover}", value_input_option="USER_ENTERED")
            print(f"    Sorted {len(data_rows)} rows by auction date")
        except Exception as e:
            print(f"    ERROR writing sorted data: {e}")

    # --- DEDUP CLEANUP (removes duplicate notice IDs from existing data) ---
    def dedup_existing_data(self, ws):
        """Remove duplicate entries based on notice ID. Keeps the first occurrence."""
        try:
            all_data = ws.get_all_values()
        except Exception as e:
            print(f"    ERROR reading for dedup: {e}")
            return 0

        if len(all_data) <= FIRST_DATA_ROW:
            return 0

        seen_ids = set()
        rows_to_keep = []
        dupes_removed = 0

        for i, row in enumerate(all_data):
            if i < FIRST_DATA_ROW - 1:
                continue  # skip headers

            if not any(c.strip() for c in row):
                continue

            # Get notice ID from column R, or extract from URL in column Q
            notice_id = row[COL["notice_id"] - 1].strip() if len(row) >= COL["notice_id"] else ""
            if not notice_id:
                url = row[COL["notice_url"] - 1].strip() if len(row) >= COL["notice_url"] else ""
                m = re.search(r"ID=(\d+)", url)
                if m:
                    notice_id = m.group(1)

            # Also check borrower + county as fallback composite key
            borrower = row[COL["borrower"] - 1].strip() if len(row) >= COL["borrower"] else ""
            county = row[COL["county"] - 1].strip() if len(row) >= COL["county"] else ""
            composite_key = f"{borrower}|{county}".lower()

            if notice_id and notice_id in seen_ids:
                dupes_removed += 1
                continue
            if not notice_id and composite_key in seen_ids:
                dupes_removed += 1
                continue

            if notice_id:
                seen_ids.add(notice_id)
            if composite_key:
                seen_ids.add(composite_key)
            rows_to_keep.append(row)

        if dupes_removed == 0:
            return 0

        # Pad rows
        for i, row in enumerate(rows_to_keep):
            if len(row) < 19:
                rows_to_keep[i] = row + [""] * (19 - len(row))

        # Write deduped data back
        start = FIRST_DATA_ROW
        end = start + len(rows_to_keep) - 1
        try:
            ws.update(values=rows_to_keep, range_name=f"A{start}:S{end}", value_input_option="USER_ENTERED")
            # Clear leftover rows
            total_before = len(all_data)
            if end + 1 <= total_before:
                blank_count = total_before - end
                if blank_count > 0 and blank_count <= 1000:
                    blank_rows = [[""] * 19] * blank_count
                    ws.update(values=blank_rows, range_name=f"A{end+1}:S{end+blank_count}", value_input_option="USER_ENTERED")
            print(f"    Removed {dupes_removed} duplicates")
        except Exception as e:
            print(f"    ERROR writing deduped data: {e}")

        return dupes_removed

    # --- PAST AUCTIONS ARCHIVAL ---
    def get_or_create_past_auctions_tab(self):
        try:
            return self.spreadsheet.worksheet("Past Auctions")
        except gspread.exceptions.WorksheetNotFound:
            print("  Creating 'Past Auctions' tab...")
            ws = self.spreadsheet.add_worksheet(title="Past Auctions", rows=1000, cols=19)
            ws.update(values=[HEADERS], range_name="A1:S1", value_input_option="USER_ENTERED")
            return ws

    def archive_past_auctions(self, county_ws, county):
        """Move rows with past auction dates to Past Auctions tab."""
        today = date.today()
        try:
            all_data = county_ws.get_all_values()
        except Exception as e:
            print(f"  ERROR reading {county} for archival: {e}")
            return 0

        if len(all_data) <= FIRST_DATA_ROW - 1:
            return 0

        rows_to_archive = []
        rows_to_keep = []

        for i, row in enumerate(all_data):
     
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
    "Notice URL", "Notice ID", "Assessed Value",
]

FIRST_DATA_ROW = 7  # Row 6 is headers, data starts at row 7
MAX_PAGES = 20       # Safety cap per county


# ============================================================
# SNIPPET PARSER — extracts data from search result text
# ============================================================

def parse_snippet(text, notice_id=""):
    """Parse a foreclosure notice snippet to extract structured fields."""
    result = {
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

    if not text:
        return result

    # --- BORROWER ---
    borrower_patterns = [
        r"(?:default\s+(?:having\s+been\s+)?made\s+(?:in\s+)?(?:the\s+)?(?:terms\s+and\s+)?(?:conditions\s+of\s+)?(?:a\s+)?(?:certain\s+)?(?:deed\s+of\s+trust|mortgage)\s+(?:executed\s+(?:and\s+delivered\s+)?by|made\s+by|from|given\s+by)\s+)([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*,?\s*(?:to|unto|in\s+favor|dated|recorded|filed|a\s+(?:married|single|unmarried)))",
        r"(?:DEED\s+OF\s+TRUST|MORTGAGE)\s+(?:executed\s+(?:and\s+delivered\s+)?by|made\s+by|from)\s+([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*,?\s*(?:to|unto|in\s+favor|dated|recorded|filed|a\s+(?:married|single|unmarried)))",
        r"(?:default\s+(?:of|in)\s+(?:the\s+)?payment).*?(?:by|from|executed\s+by)\s+([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*,?\s*(?:to|unto|in\s+favor|dated|recorded|filed))",
        r"(?:I,?\s+the\s+undersigned\s+)?(?:Substituted\s+)?Trustee.*?(?:sell|convey).*?(?:property\s+of|executed\s+by|made\s+by)\s+([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*[,.])",
    ]
    for pat in borrower_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            borrower = m.group(1).strip().rstrip(",. ")
            borrower = re.sub(r"\s+", " ", borrower)
            if len(borrower) > 3 and not re.match(r"^(the|a|an|to|in|for|and|or)$", borrower, re.I):
                result["borrower"] = borrower
                break

    # --- DOT DATE ---
    dot_patterns = [
        r"(?:deed\s+of\s+trust|mortgage)\s+dated\s+(?:the\s+)?(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:deed\s+of\s+trust|mortgage)\s+dated\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"dated\s+(?:the\s+)?(\w+\s+\d{1,2},?\s+\d{4})\s*,?\s*(?:and\s+)?recorded",
    ]
    for pat in dot_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["dot_date"] = m.group(1).strip()
            break

    # --- FILING INFO ---
    filing_patterns = [
        r"recorded\s+in\s+(Book\s+\d+[,\s]+(?:Page|at\s+Page)\s+\d+)",
        r"(?:filed|recorded)\s+(?:for\s+record\s+)?(?:on\s+)?(?:\w+\s+\d{1,2},?\s+\d{4}\s+)?(?:in\s+)?(?:the\s+)?(?:office\s+of\s+)?.*?(Book\s+\d+[,\s]+(?:Page|at\s+Page)\s+\d+)",
        r"(?:Instrument|Document)\s*(?:No\.?|Number|#)\s*[:\s]*(\d[\d-]+)",
        r"recorded\s+(?:on\s+)?(\w+\s+\d{1,2},?\s+\d{4})\s+(?:in\s+)?(?:the\s+)?(?:office|land\s+records)",
    ]
    for pat in filing_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["filing_info"] = m.group(1).strip()
            break

    # --- ATTORNEY / TRUSTEE ---
    attorney_patterns = [
        r"(?:Substituted\s+Trustee|Trustee)[:\s]+([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*,?\s*(?:Attorney|Esq|P\.?A|LLC|PLLC|PC|Mississippi|MS\s+\d|having|will|shall|\d{3}[\s.-]\d{3}))",
        r"(?:Attorney\s+for\s+(?:the\s+)?(?:Trustee|Mortgagee|Beneficiary)|Prepared\s+by)[:\s]+([A-Z][A-Za-z\s,.'&-]{2,60}?)(?:\s*,?\s*(?:Esq|P\.?A|LLC|PLLC|PC|Mississippi|MS\s+\d|\d{3}[\s.-]\d{3}))",
        r"(?:MCCALLA\s+RAYMER|ALBERTELLI\s+LAW|SHAPIRO\s+&\s+INGLE|SIROTE\s+&\s+PERMUTT|DEAN\s+MORRIS|UNDERWOOD\s+LAW|HALLIDAY\s+WATKINS)[A-Za-z\s,.'&-]*",
        r"(\w+\s+(?:Law\s+(?:Firm|Group|Office)|PLLC|LLC|P\.?A\.?))\s*,",
    ]
    for pat in attorney_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["attorney"] = m.group(0).strip() if pat == attorney_patterns[2] else m.group(1).strip()
            result["attorney"] = result["attorney"].rstrip(",. ")
            break

    # --- AUCTION DATE --- (must NOT match DOT date)
    dot_date_str = result.get("dot_date", "").lower()
    auction_date_patterns = [
        r"(?:sale|sold|sell|auction|foreclosure\s+sale)\s+(?:will\s+(?:be\s+)?)?(?:held\s+)?(?:on\s+)?(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+\w+,?\s+\d{4})",
        r"(?:sale|sold|sell|auction|foreclosure\s+sale)\s+(?:will\s+(?:be\s+)?)?(?:held\s+)?(?:on\s+)?(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:on|at)\s+(\w+\s+\d{1,2},?\s+\d{4})\s*,?\s*(?:at|between|during|within)\s+(?:the\s+)?(?:legal\s+)?(?:hours|hour|time)",
        r"(\w+\s+\d{1,2},?\s+\d{4})\s*,?\s*(?:at|between)\s+(?:the\s+)?(?:legal\s+)?(?:hours|hour)",
    ]
    for pat in auction_date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() not in dot_date_str and "deed" not in text[max(0, m.start()-30):m.start()].lower():
                result["auction_date"] = candidate
                break

    # --- AUCTION TIME ---
    time_patterns = [
        r"(?:at|between)\s+(?:the\s+)?(?:hour\s+of\s+)?(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?|o'clock|noon))",
        r"(?:between\s+the\s+(?:legal\s+)?hours\s+of\s+)(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)\s+and\s+\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))",
        r"(?:at\s+)?(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))\s*(?:,|on|at\s+the)",
        r"(?:legal\s+hours|during\s+(?:the\s+)?(?:legal\s+)?hours?\s+of\s+sale\s+)(?:between\s+)?(\d{1,2}[:\s]*\d{0,2}\s*(?:a\.?m\.?|p\.?m\.?|o'clock))",
    ]
    for pat in time_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["auction_time"] = m.group(1).strip()
            break

    # --- AUCTION LOCATION ---
    location_patterns = [
        r"(?:front\s+door|courthouse\s+(?:door|steps|building))\s+(?:of\s+(?:the\s+)?)?([A-Za-z\s]+County\s+Courthouse[A-Za-z\s,]*?(?:Mississippi|MS))",
        r"(?:at\s+the\s+)((?:front\s+door|south\s+door|north\s+door|east\s+door|west\s+door|main\s+entrance)\s+of\s+(?:the\s+)?[A-Za-z\s]+County\s+Courthouse)",
        r"(?:at\s+the\s+)([A-Za-z\s]+County\s+Courthouse\b[^.]*?(?:Mississippi|MS|in\s+\w+))",
        r"(?:sale\s+(?:shall|will)\s+be\s+held\s+at\s+)(.+?(?:Courthouse|Building|Office)[^.]{0,60}?)(?:\.|,\s+(?:on|at|between))",
    ]
    for pat in location_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["auction_location"] = re.sub(r"\s+", " ", m.group(1).strip()).rstrip(",. ")
            break

    # --- LEGAL DESCRIPTION ---
    legal_patterns = [
        r"(?:(?:the\s+)?following\s+(?:described\s+)?(?:real\s+)?(?:property|land|estate)\s*[:\-]\s*)(.*?)(?:\s*(?:SAID|SUBJECT|WITNESS|I\s+WILL|TERMS|being\s+the\s+same))",
        r"(?:Lot\s+\d+[A-Za-z\s,.'&\d()-]+(?:Subdivision|Addition|Plat|Section|Township)[A-Za-z\s,.'&\d()-]*?)(?:\.|SAID|SUBJECT)",
    ]
    for pat in legal_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            legal = m.group(1).strip() if "following" in pat else m.group(0).strip()
            legal = re.sub(r"\s+", " ", legal).rstrip(",. ")
            if len(legal) > 10:
                result["legal_desc"] = legal[:500]
                break

    return result


# ============================================================
# ASP.NET SCRAPER
# ============================================================

class MSNoticesScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self.session_url = None

    def _init_session(self):
        """Initialize ASP.NET session — gets cookieless session URL."""
        resp = self.session.get(SEARCH_URL, timeout=30)
        resp.raise_for_status()
        self.session_url = resp.url
        return BeautifulSoup(resp.text, "html.parser")

    def _get_form_data(self, soup):
        """Extract all hidden form fields from ASP.NET page."""
        form = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if name:
                form[name] = inp.get("value", "")
        return form

    def search_county(self, county):
        """Run 3-step ASP.NET postback to get foreclosure listings for a county."""
        idx = COUNTY_INDICES.get(county)
        if idx is None:
            print(f"  WARNING: No index for county {county}")
            return []

        # Step 1: Init session
        soup = self._init_session()
        form = self._get_form_data(soup)
        print(f"  Session: {self.session_url}")

        # Step 2: Select Foreclosure from dropdown
        form["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "3"
        form["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"
        form.pop("ctl00$ContentPlaceHolder1$as1$btnGo", None)

        try:
            resp = self.session.post(self.session_url, data=form, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR selecting Foreclosure type: {e}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        form = self._get_form_data(soup)

        # Step 3: Check county checkbox + click Search
        cb_name = f"ctl00$ContentPlaceHolder1$as1$cblNewspapers${idx}"
        form[cb_name] = "on"
        form["ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] = "3"
        form["ctl00$ContentPlaceHolder1$as1$btnGo"] = ""

        try:
            resp = self.session.post(self.session_url, data=form, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ERROR searching {county}: {e}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = self._parse_search_results(soup, county)
        print(f"  Page 1: {len(results)} results")

        # Paginate
        page_num = 1
        while page_num < MAX_PAGES:
            next_btn = self._find_next_page_btn(soup)
            if not next_btn:
                break

            form_pg = self._get_form_data(soup)
            form_pg[next_btn + ".x"] = "10"
            form_pg[next_btn + ".y"] = "10"
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
            city_match = re.search(r"City:\s*([\w\s'-]+?)(?:\s+County:|\s*$)", row_text)
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
            composite_key = f"{borrower}||{county}".lower()

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
            sheet_row = i + 1
            if sheet_row < FIRST_DATA_ROW:
                continue
            if not any(cell.strip() for cell in row):
                continue

            auction_str = row[COL["auction_date"] - 1].strip() if len(row) >= COL["auction_date"] else ""
            if not auction_str:
                rows_to_keep.append(row)
                continue

            auction_dt = None
            for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
                try:
                    auction_dt = datetime.strptime(auction_str, fmt).date()
                    break
                except ValueError:
                    continue

            if auction_dt and auction_dt < today:
                rows_to_archive.append(row[:19])
            else:
                rows_to_keep.append(row)

        if not rows_to_archive:
            return 0

        # Append archived rows to Past Auctions tab
        past_ws = self.get_or_create_past_auctions_tab()
        past_all = past_ws.get_all_values()
        past_next_row = max(len(past_all) + 1, 2)

        end_row = past_next_row + len(rows_to_archive) - 1
        # Pad rows
        for i, row in enumerate(rows_to_archive):
            if len(row) < 19:
                rows_to_archive[i] = row + [""] * (19 - len(row))

        try:
            past_ws.update(
                values=rows_to_archive,
                range_name=f"A{past_next_row}:S{end_row}",
                value_input_option="USER_ENTERED"
            )
            print(f"  Archived {len(rows_to_archive)} past auctions from {county}")
        except Exception as e:
            print(f"  ERROR writing to Past Auctions: {e}")
            return 0

        # Rewrite county tab with only current rows
        for i, row in enumerate(rows_to_keep):
            if len(row) < 19:
                rows_to_keep[i] = row + [""] * (19 - len(row))

        start = FIRST_DATA_ROW
        if rows_to_keep:
            end = start + len(rows_to_keep) - 1
            try:
                county_ws.update(
                    values=rows_to_keep,
                    range_name=f"A{start}:S{end}",
                    value_input_option="USER_ENTERED"
                )
            except Exception as e:
                print(f"  ERROR rewriting {county} tab: {e}")
                return 0
        else:
            end = start - 1

        # Clear leftover rows
        old_total = len(all_data)
        if end + 1 <= old_total:
            blank_count = old_total - end
            if blank_count > 0 and blank_count <= 1000:
                blank_rows = [[""] * 19] * blank_count
                try:
                    county_ws.update(
                        values=blank_rows,
                        range_name=f"A{end+1}:S{end+blank_count}",
                        value_input_option="USER_ENTERED"
                    )
                except Exception:
                    pass

        return len(rows_to_archive)

    # --- SUMMARY TAB ---
    def update_summary(self, county_counts, run_stats):
        try:
            summary = self.spreadsheet.worksheet("Summary")
        except gspread.exceptions.WorksheetNotFound:
            summary = self.spreadsheet.add_worksheet(title="Summary", rows=20, cols=10)

        now_str = datetime.now().strftime("%m/%d/%Y %I:%M %p CT")

        rows = []
        rows.append(["SPU MS PUBLIC NOTICE AI COMMAND CENTER", "", "", "", "", "", "", "", ""])
        rows.append([f"Last Run: {now_str}", "", "", "", "", "", "", "", ""])
        rows.append(["", "", "", "", "", "", "", "", ""])
        rows.append([
            "County", "Total Active", "New This Run", "Archived This Run",
            "Errors", "Pages Scraped", "Status", "Run Duration", "Last Updated"
        ])

        for county in COUNTIES:
            stats = run_stats.get(county, {})
            rows.append([
                county,
                str(stats.get("total_active", 0)),
                str(stats.get("new_count", 0)),
                str(stats.get("archived", 0)),
                str(stats.get("errors", 0)),
                str(stats.get("pages", 0)),
                stats.get("status", "OK"),
                stats.get("duration", ""),
                now_str,
            ])

        rows.append(["", "", "", "", "", "", "", "", ""])
        total_active = sum(run_stats.get(c, {}).get("total_active", 0) for c in COUNTIES)
        total_new = sum(run_stats.get(c, {}).get("new_count", 0) for c in COUNTIES)
        total_archived = sum(run_stats.get(c, {}).get("archived", 0) for c in COUNTIES)
        rows.append([
            "TOTAL", str(total_active), str(total_new), str(total_archived),
            "", "", "", "", ""
        ])

        try:
            summary.update(
                values=rows,
                range_name=f"A1:I{len(rows)}",
                value_input_option="USER_ENTERED"
            )
            print("  Summary tab updated")
        except Exception as e:
            print(f"  ERROR updating Summary: {e}")

    # --- RUN LOG TAB ---
    def update_run_log(self, run_stats):
        try:
            log_ws = self.spreadsheet.worksheet("Run Log")
        except gspread.exceptions.WorksheetNotFound:
            log_ws = self.spreadsheet.add_worksheet(title="Run Log", rows=500, cols=12)
            log_headers = [
                "Run Timestamp", "Total New", "Total Archived", "Counties Scraped",
                "George", "Hancock", "Harrison", "Hinds", "Jackson", "Rankin", "Stone", "Errors"
            ]
            log_ws.update(values=[log_headers], range_name="A1:L1", value_input_option="USER_ENTERED")

        now_str = datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")
        total_new = sum(run_stats.get(c, {}).get("new_count", 0) for c in COUNTIES)
        total_archived = sum(run_stats.get(c, {}).get("archived", 0) for c in COUNTIES)
        counties_scraped = sum(1 for c in COUNTIES if run_stats.get(c, {}).get("status", "") != "FAILED")
        errors_list = [run_stats.get(c, {}).get("error_msg", "") for c in COUNTIES if run_stats.get(c, {}).get("error_msg")]
        errors_str = "; ".join(errors_list) if errors_list else "None"

        row = [now_str, str(total_new), str(total_archived), str(counties_scraped)]
        for county in COUNTIES:
            stats = run_stats.get(county, {})
            row.append(str(stats.get("new_count", 0)))
        row.append(errors_str)

        try:
            all_vals = log_ws.col_values(1)
            next_row = len(all_vals) + 1
            if next_row < 2:
                next_row = 2
        except Exception:
            next_row = 2

        try:
            log_ws.update(values=[row], range_name=f"A{next_row}:L{next_row}", value_input_option="USER_ENTERED")
            print(f"  Run Log updated (row {next_row})")
        except Exception as e:
            print(f"  ERROR updating Run Log: {e}")

    # --- RESOURCE SHEET ---
    def update_resource_sheet(self):
        try:
            res_ws = self.spreadsheet.worksheet("Resources")
        except gspread.exceptions.WorksheetNotFound:
            res_ws = self.spreadsheet.add_worksheet(title="Resources", rows=30, cols=4)

        rows = [
            ["SPU MS PUBLIC NOTICE AI COMMAND CENTER — Resources", "", "", ""],
            ["", "", "", ""],
            ["RESOURCE", "URL", "PURPOSE", "NOTES"],
            ["MS Public Notices", "https://www.mspublicnotices.org/Search.aspx", "Primary data source — all MS foreclosure notices", "Search by county, select Foreclosure type"],
            ["Notice Lookup", "https://www.mspublicnotices.org/Search.aspx", "Look up individual notices by keyword", "Copy borrower name from sheet, paste into search box, select county"],
            ["George County Assessor", "https://www.deltacomputersystems.com/MS/MS19/index.html", "Property records, parcel IDs, assessed values", "Search by owner name or parcel ID"],
            ["Hancock County Assessor", "https://www.deltacomputersystems.com/MS/MS24/index.html", "Property records, parcel IDs, assessed values", ""],
            ["Harrison County Assessor", "https://www.deltacomputersystems.com/MS/MS25/index.html", "Property records, parcel IDs, assessed values", ""],
            ["Hinds County Assessor", "https://www.deltacomputersystems.com/MS/MS27/index.html", "Property records, parcel IDs, assessed values", ""],
            ["Jackson County Tax Assessor", "https://www.deltacomputersystems.com/MS/MS30/index.html", "Property records, parcel IDs, assessed values", ""],
            ["Rankin County Assessor", "https://www.deltacomputersystems.com/MS/MS60/index.html", "Property records, parcel IDs, assessed values", ""],
            ["Stone County Assessor", "https://www.deltacomputersystems.com/MS/MS67/index.html", "Property records, parcel IDs, assessed values", ""],
            ["", "", "", ""],
            ["HOW TO LOOK UP A NOTICE MANUALLY", "", "", ""],
            ["1. Go to mspublicnotices.org/Search.aspx", "", "", ""],
            ["2. Select 'Foreclosure' from Popular Searches dropdown", "", "", ""],
            ["3. Check the county checkbox", "", "", ""],
            ["4. Paste the borrower name from column B into the search box", "", "", ""],
            ["5. Click Search — the full notice text will appear", "", "", ""],
            ["", "", "", ""],
            ["SKIP TRACING (Phase 2 — Not Yet Implemented)", "", "", ""],
            ["Skip tracing requires integration with a paid API (TLO, Spokeo, or BeenVerified).", "", "", ""],
            ["Phone numbers, mailing addresses, and property addresses will auto-populate", "", "", ""],
            ["once the skip tracing module is built and connected.", "", "", ""],
        ]

        try:
            res_ws.update(
                values=rows,
                range_name=f"A1:D{len(rows)}",
                value_input_option="USER_ENTERED"
            )
            print("  Resource sheet updated")
        except Exception as e:
            print(f"  ERROR updating Resource sheet: {e}")

    # --- UPDATE COUNTY TAB HEADERS ---
    def update_county_headers(self, ws):
        try:
            ws.update(
                values=[HEADERS],
                range_name=f"A6:S6",
                value_input_option="USER_ENTERED"
            )
        except Exception as e:
            print(f"    WARNING: Could not update headers: {e}")


# ============================================================
# EMAIL NOTIFICATION
# ============================================================

def send_email_report(new_notices, errors, run_stats):
    email_to = os.environ.get("NOTIFY_EMAIL", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([email_to, smtp_user, smtp_pass]):
        print("  Email not configured — skipping")
        return

    today = date.today().strftime("%m/%d/%Y")
    total = sum(len(v) for v in new_notices.values())
    total_archived = sum(run_stats.get(c, {}).get("archived", 0) for c in COUNTIES)

    subject = f"SPU Command Center — {today} — {total} new | {total_archived} archived"

    lines = [
        "SPU MS PUBLIC NOTICE AI COMMAND CENTER",
        f"Scrape Report: {today}",
        f"Total new notices: {total}",
        f"Total archived (past auctions): {total_archived}",
        "",
        "BY COUNTY:",
        "=" * 50,
    ]

    for county in COUNTIES:
        notices = new_notices.get(county, [])
        stats = run_stats.get(county, {})
        lines.append(f"\n{county}: {len(notices)} new | {stats.get('archived', 0)} archived | {stats.get('total_active', 0)} active")
        for n in notices[:10]:
            borrower = n.get("borrower", "Unknown")
            auction = n.get("auction_date", "No date")
            lines.append(f"  - {borrower} | Auction: {auction}")
        if len(notices) > 10:
            lines.append(f"  ... and {len(notices) - 10} more")

    if errors:
        lines.append("\n\nERRORS:")
        lines.append("=" * 50)
        for err in errors:
            lines.append(f"  - {err}")

    lines.append(f"\n\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")

    body = "\n".join(lines)
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
    print(f"{'=' * 60}")
    print(f"SPU MS PUBLIC NOTICE AI COMMAND CENTER")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    scraper = MSNoticesScraper()
    errors = []

    try:
        sheets = SheetHandler()
        print("Google Sheets connected.")
    except Exception as e:
        print(f"FATAL: Cannot connect to Google Sheets: {e}")
        traceback.print_exc()
        sys.exit(1)

    new_notices = {}
    run_stats = {}

    for county in COUNTIES:
        print(f"\n--- {county} County ---")
        county_start = time.time()

        ws = sheets.get_tab(county)
        if not ws:
            errors.append(f"{county}: tab not found")
            run_stats[county] = {"status": "FAILED", "error_msg": "Tab not found"}
            continue

        sheets.update_county_headers(ws)

        dupes = sheets.dedup_existing_data(ws)
        if dupes:
            print(f"  Cleaned {dupes} duplicate entries")

        existing_ids = sheets.get_all_existing_ids(ws)
        print(f"  Existing unique entries: {len(existing_ids)}")

        try:
            listings = scraper.search_county(county)
        except Exception as e:
            msg = f"{county}: search failed — {e}"
            print(f"  ERROR: {msg}")
            errors.append(msg)
            run_stats[county] = {"status": "FAILED", "error_msg": str(e)}
            continue

        print(f"  Found {len(listings)} total listings")

        new_listings = [l for l in listings if l["id"] not in existing_ids]
        print(f"  New (not in sheet): {len(new_listings)}")

        if new_listings:
            for i, listing in enumerate(new_listings):
                b = listing.get("borrower", "Unknown")[:40]
                a = listing.get("auction_date", "No date")
                print(f"    [{i+1}/{len(new_listings)}] {b} | Auction: {a}")

            county_new = sheets.batch_append_notices(ws, new_listings, county)
            if not county_new and new_listings:
                errors.append(f"{county}: batch write failed for {len(new_listings)} notices")
        else:
            county_new = []

        new_notices[county] = county_new

        archived = 0
        try:
            archived = sheets.archive_past_auctions(ws, county)
            if archived:
                print(f"  Moved {archived} past auctions to 'Past Auctions' tab")
        except Exception as e:
            print(f"  WARNING: archive failed for {county}: {e}")

        sheets.sort_by_auction_date(ws)

        try:
            active_data = ws.get_all_values()
            total_active = max(0, len([r for r in active_data[FIRST_DATA_ROW-1:] if any(c.strip() for c in r)]))
        except Exception:
            total_active = 0

        county_duration = f"{time.time() - county_start:.1f}s"
        run_stats[county] = {
            "total_active": total_active,
            "new_count": len(county_new),
            "archived": archived,
            "errors": 1 if any(county in e for e in errors) else 0,
            "pages": min(len(listings) // 10 + 1, MAX_PAGES) if listings else 0,
            "status": "OK" if not any(county in e for e in errors) else "ERROR",
            "duration": county_duration,
            "error_msg": "",
        }

        time.sleep(2)

    print("\n--- Updating Summary ---")
    sheets.update_summary({c: len(new_notices.get(c, [])) for c in COUNTIES}, run_stats)

    sheets.update_run_log(run_stats)

    sheets.update_resource_sheet()

    total_new = sum(len(v) for v in new_notices.values())
    total_archived = sum(run_stats.get(c, {}).get("archived", 0) for c in COUNTIES)
    print(f"\n{'=' * 60}")
    print(f"COMPLETE — {total_new} new | {total_archived} archived")
    for county in COUNTIES:
        s = run_stats.get(county, {})
        print(f"  {county}: +{s.get('new_count',0)} new | -{s.get('archived',0)} archived | {s.get('total_active',0)} active")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    print(f"{'=' * 60}")

    send_email_report(new_notices, errors, run_stats)

    if errors and total_new == 0:
        sys.exit(1)


if __name__ == "__main__":
    run_pipeline()

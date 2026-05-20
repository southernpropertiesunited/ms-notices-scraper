#!/usr/bin/env python3
"""
SPU MS Public Notices AI Command Center — Ultimate Engine v4.0
============================================================
Scrapes foreclosure and probate notices from mspublicnotices.org for 6 MS counties.
Integrates live BatchLeads API skip tracing to fill out phone, alt phone, and properties.
Integrates Nimble API to extract dynamic values from Delta Computer Systems.
Maintains absolute 19-column sheet layouts natively.

Runs via GitHub Actions Mon/Wed/Fri at 6 AM CT.

The Unified Architecture Lifecycle:
[1. Playwright Crawler] -> Scrapes Full Notice (Foreclosure / Probate)
        │
[2. Regex Parsers]      -> Extracts Borrower/Deceased Name & County
        │
[3. BatchLeads API]     -> Pulls Mobile Phones, Emails, & Standardized Addresses
        │
[4. Nimble Web API]     -> Automates Delta Computer Systems & DuProcess Lookups
        │                  (Populates Parcel ID & Assessed Value)
        │
[5. Sheet Dashboard]    -> Injects completely populated 19-Column row + 
                           Pre-filled manual backup search links
"""

import os
import re
import sys
import time
import json
import traceback
import smtplib
import requests
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Framework Core Imports
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# MASTER CONFIGURATION & SYSTEM LAYOUTS
# ============================================================

COUNTIES = ["George", "Harrison", "Hinds", "Jackson", "Rankin", "Stone"]
SHEET_ID = "1cgGpocIQBdP_39tuI3Pqy4xJud7vZ2yCrHr4_PBH0Ro"
BASE_URL = "https://www.mspublicnotices.org"
START_URL = f"{BASE_URL}/(S(vs4i4imwl2zm2hvehn0xhxmf))/default.aspx"

COUNTY_INDICES = {
    "George": 19, "Harrison": 23, "Hinds": 24,
    "Jackson": 29, "Rankin": 60, "Stone": 65,
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Strict 19-column schema footprint (A-S)
COL = {
    "scrape_date": 1,       # A
    "borrower": 2,          # B (Holds Borrower OR Deceased Name)
    "county": 3,            # C
    "phone": 4,             # D (Populated by BatchLeads)
    "alt_phone": 5,         # E (Populated by BatchLeads)
    "mailing_address": 6,   # F (Holds Mailing Address OR Executor Name)
    "property_address": 7,  # G (Populated by BatchLeads)
    "parcel_id": 8,         # H (Populated by Nimble)
    "dot_date": 9,          # I
    "filing_info": 10,      # J (Holds Book/Page OR Case Cause Number)
    "attorney": 11,         # K (Holds Substituted Trustee OR Plaintiff Attorney)
    "auction_date": 12,     # L (Holds Auction Date OR Court Hearing Date)
    "auction_time": 13,     # M
    "auction_location": 14, # N
    "legal_desc": 15,       # O
    "pub_dates": 16,        # P
    "notice_url": 17,       # Q
    "notice_id": 18,        # R
    "assessed_value": 19,   # S (Populated by Nimble)
}

HEADERS = [
    "Scrape Date", "Borrower", "County", "Phone", "Alt Phone",
    "Mailing Address", "Property Address", "Parcel ID", "DOT Date",
    "Filing Info", "Attorney/Trustee", "Auction Date", "Auction Time",
    "Auction Location", "Legal Description", "Publication Dates",
    "Notice URL", "Notice ID", "Assessed Value",
]

FIRST_DATA_ROW = 7


# ============================================================
# DATA INTEL ENRICHMENT MODULES (BATCHLEADS & NIMBLE)
# ============================================================

def skip_trace_lead(name, county, state="MS"):
    api_key = os.environ.get("BATCHLEADS_API_KEY", "31fe315c-636c-4e36-a8e4-6b3ff09c6358")
    if not name or name.lower() in ["unknown", ""]: return {}
    url = "https://api.batchdata.com/api/v1/property/skip-trace"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "requests": [{
            "name": {"rawName": name},
            "propertyAddress": {"county": county, "state": state},
            "options": {"prioritizeMobilePhones": True, "includeTCPABlacklistedPhones": False}
        }]
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            persons = response.json().get("results", {}).get("persons", [])
            if persons:
                match = persons[0]
                phones = [p.get("phoneNumber") for p in match.get("phones", []) if p.get("phoneNumber")]
                p_addr = match.get("propertyAddress", {})
                m_addr = match.get("mailingAddress", {})
                return {
                    "phone1": phones[0] if len(phones) > 0 else "",
                    "phone2": phones[1] if len(phones) > 1 else "",
                    "property_address": f"{p_addr.get('street', '')}, {p_addr.get('city', '')}, {p_addr.get('state', '')} {p_addr.get('zip', '')}".strip(", "),
                    "mailing_address": f"{m_addr.get('street', '')}, {m_addr.get('city', '')}, {m_addr.get('state', '')} {m_addr.get('zip', '')}".strip(", ")
                }
    except Exception: pass
    return {}


def query_nimble_property_data(name, county):
    token = os.environ.get("NIMBLE_API_TOKEN", "14b5f72a91c74fec93fd3543f234ea4427229ee839f14d34857dcfa0251d4956")
    if not name or name.lower() in ["unknown", ""]: return {}
    url = "https://api.nimbleway.com/v1/extract"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "url": "https://www.deltacomputersystems.com/search.html",
        "format": "json", "render_js": True,
        "actions": [
            {"action": "fill", "selector": "input[name*='search']", "value": name},
            {"action": "click", "selector": "input[type='submit'], button:has-text('Search')"},
            {"action": "wait", "value": 3000}
        ]
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            corpus = response.json().get("content", "")
            parcel_match = re.search(r"(?:Parcel|PPIN)[:\s]*([\d\w-]+)", corpus, re.IGNORECASE)
            value_match = re.search(r"(?:Assessed\s+Value|Total\s+Value)[:\s]*\$?([\d,]+)", corpus, re.IGNORECASE)
            return {
                "parcel_id": parcel_match.group(1).strip() if parcel_match else "",
                "assessed_value": f"${value_match.group(1).strip()}" if value_match else ""
            }
    except Exception: pass
    return {}


# ============================================================
# EXTRACTOR REGEX BLOCKS
# ============================================================

def parse_probate_full_text(text, county_default=""):
    result = {
        "category": "Probate", "county": county_default, "deceased": "", "petitioner": "",
        "cause_no": "", "attorney": "", "hearing_date": "", "hearing_time": "",
        "hearing_loc": "", "legal_desc": "", "pub_dates": ""
    }
    if not text: return result
    c_m = re.search(r"CHANCERY\s+COURT\s+OF\s+([A-Z\s]+?)\s+COUNTY", text, re.IGNORECASE)
    result["county"] = c_m.group(1).strip().title() if c_m else county_default
    d_m = re.search(r"(?:ESTATE\s+OF|TESTAMENT\s+OF)\s+([A-Z\s,.'&-]+?)(?:,|\s+DECEASED)", text, re.IGNORECASE)
    if d_m: result["deceased"] = re.sub(r"\s+", " ", d_m.group(1)).strip().title()
    p_m = re.search(r"([A-Z\s,.'&-]{2,100}?)(?:\s*,\s*(?:PETITIONER|EXECUTOR|EXECUTORS))", text, re.IGNORECASE)
    if p_m: result["petitioner"] = re.sub(r"\s+", " ", p_m.group(1)).strip().title()
    case_m = re.search(r"(?:CAUSE\s+(?:NO|NUMBER)[:\s]*)([\w\d\s-]+)", text, re.IGNORECASE)
    if case_m: result["cause_no"] = case_m.group(1).strip()
    h_date = re.search(r"(?:hearing\s+will\s+be\s+held\s+on\s+.*?|at\s+)(?:on\s+the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+\w+,?\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4})", text, re.IGNORECASE)
    if h_date: result["hearing_date"] = h_date.group(1).strip()
    h_time = re.search(r"(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))", text, re.IGNORECASE)
    if h_time: result["hearing_time"] = h_time.group(1).strip()
    firm_m = re.search(r"(\b[A-Z\s,.'&-]+?LAW\s+(?:FIRM|GROUP|OFFICE|PLLC|LLC)[\s\S]+?(?:\d{5}))", text, re.IGNORECASE)
    if firm_m: result["attorney"] = re.sub(r"\s+", " ", firm_m.group(1)).strip()
    result["legal_desc"] = text[:500].strip() + "..."
    pub_m = re.search(r"(?:Publish|Publication\s+Dates)?[:\s]*([A-Z][a-z]{2}\s+\d[\d\s,]*\d{4})", text, re.IGNORECASE)
    result["pub_dates"] = pub_m.group(1).strip() if pub_m else ""
    return result


def parse_foreclosure_full_text(text, county_default=""):
    result = {
        "category": "Foreclosure", "borrower": "", "county": county_default, "dot_date": "",
        "filing_info": "", "attorney": "", "auction_date": "", "auction_time": "",
        "auction_location": "", "legal_desc": "", "pub_dates": ""
    }
    if not text: return result
    b_m = re.search(r"(?:executed\s+a\s+certain\s+deed\s+of\s+trust\s+(?:to|by)?|made\s+by|from)\s+([A-Z][A-Za-z\s,.'&-]{2,120}?)(?:\s+to\s+[A-Z][A-Za-z\s,.]+?,\s*Trustee)", text, re.IGNORECASE)
    if b_m: result["borrower"] = re.sub(r"\s+", " ", b_m.group(1)).strip().rstrip(", ")
    dot_m = re.search(r"(?:deed\s+of\s+trust|mortgage)\s+dated\s+([\w\s\d,]{6,20})", text, re.IGNORECASE)
    if dot_m: result["dot_date"] = dot_m.group(1).strip()
    inst_m = re.search(r"((Inst\.?\#\s*\d+,\s*)?Book\s+\d+\s+at\s+Page\s+\d+|Book\s+\d+,\s*Page\s+\d+|Inst\.?\#\s*\d+)", text, re.IGNORECASE)
    if inst_m: result["filing_info"] = inst_m.group(1).strip()
    d_m = re.search(r"(?:will\s+on|on)\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", text)
    if d_m: result["auction_date"] = d_m.group(1).strip()
    t_m = re.search(r"(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)\s+and\s+\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))", text, re.IGNORECASE)
    if t_m: result["auction_time"] = t_m.group(1).strip()
    l_m = re.search(r"at\s+the\s+([A-Za-z\s]+?Courthouse.*?,\s*[A-Z]{2}\s+\d{5})", text, re.IGNORECASE)
    if l_m: result["auction_location"] = re.sub(r"\s+", " ", l_m.group(1)).strip()
    leg_m = re.search(r"to-wit:\s*(.*?)(?:\s*(?:ATTENTION|SAID|SUBJECT|WITNESS))", text, re.IGNORECASE | re.DOTALL)
    if leg_m: result["legal_desc"] = re.sub(r"\s+", " ", leg_m.group(1)).strip()[:2000]
    atty_m = re.search(r"([A-Z\s,.'&-]+?-\s*SUBSTITUTED\s+TRUSTEE[\s\S]+?(?:\d{5}))", text, re.IGNORECASE)
    if atty_m: result["attorney"] = re.sub(r"\s+", " ", atty_m.group(1)).strip()
    pub_m = re.search(r"Publication\s+Dates:\s*([^\n\.]+)", text, re.IGNORECASE)
    result["pub_dates"] = pub_m.group(1).strip() if pub_m else ""
    return result


# ============================================================
# CRAWLER RUNTIME ENGINE
# ============================================================

def run_browser_pipeline(county, existing_ids):
    idx = COUNTY_INDICES.get(county)
    if idx is None: return []
    results = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        print(f"  Opening Target Base: {START_URL}")
        page.goto(START_URL)
        page.wait_for_load_state("networkidle")

        today = datetime.now()
        start_win = today - timedelta(days=7)
        
        if page.locator("input[name*='txtStartDate']").count() > 0:
            page.fill("input[name*='txtStartDate']", start_win.strftime("%m/%d/%Y"))
            page.fill("input[name*='txtEndDate']", today.strftime("%m/%d/%Y"))

        cb = page.locator(f"input[name*='cblNewspapers'][name*='${idx}']")
        if cb.count() > 0: cb.check()

        page.click("input[name*='btnGo']")
        page.wait_for_load_state("networkidle")

        rows = page.locator("tr").all()
        targets = []
        for r in rows:
            text = r.inner_text()
            pk = r.locator("input[name*='hdnPKValue']").first
            if pk.count() == 0: continue
            
            notice_id = pk.get_attribute("value").strip()
            if notice_id in existing_ids: continue

            is_fc = "SUBSTITUTE TRUSTEE" in text.upper()
            is_pr = "DECEASED CAUSE" in text.upper() or "ESTATE OF" in text.upper()
            
            if is_fc or is_pr:
                targets.append({
                    "id": notice_id,
                    "tag": "Foreclosure" if is_fc else "Probate",
                    "url": f"{BASE_URL}/Details.aspx?ID={notice_id}"
                })

        print(f"  Identified {len(targets)} matching leads to pull.")

        for target in targets:
            det_page = context.new_page()
            try:
                det_page.goto(target["url"])
                det_page.wait_for_load_state("networkidle")

                if det_page.locator("iframe[src*='recaptcha']").count() > 0:
                    c_frame = det_page.frame(url=re.compile(r"recaptcha"))
                    if c_frame:
                        c_frame.click("#recaptcha-anchor")
                        time.sleep(3)

                agree = det_page.locator("input[value*='I agree'], button:has-text('I agree')")
                if agree.count() > 0:
                    agree.click()
                    det_page.wait_for_load_state("networkidle")

                content_loc = det_page.locator("[id*='lblContent'], .NoticeContent, [id*='Notice']").first
                if content_loc.count() > 0:
                    full_text = content_loc.inner_text()
                    
                    if target["tag"] == "Foreclosure":
                        parsed = parse_foreclosure_full_text(full_text, county)
                    else:
                        parsed = parse_probate_full_text(full_text, county)

                    parsed.update({"id": target["id"], "url": target["url"]})
                    results.append(parsed)
            except Exception as e:
                print(f"    Error opening details line: {e}")
            finally:
                det_page.close()
        browser.close()
    return results


# ============================================================
# DATA PIPELINE SHEET MANAGEMENT
# ============================================================

class SheetHandler:
    def __init__(self):
        creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not creds_json: raise RuntimeError("Missing account credential mapping chains.")
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(SHEET_ID)

    def get_tab(self, county):
        try: return self.spreadsheet.worksheet(county)
        except gspread.exceptions.WorksheetNotFound: return None

    def get_all_existing_ids(self, ws):
        try:
            ids = set(val.strip() for val in ws.col_values(COL["notice_id"]) if val.strip().lower() != "notice id")
            for url in ws.col_values(COL["notice_url"]):
                m = re.search(r"ID=(\d+)", url)
                if m: ids.add(m.group(1))
            return ids
        except Exception: return set()

    def find_next_empty_row(self, ws):
        try:
            all_vals = ws.col_values(COL["borrower"])
            last_row = FIRST_DATA_ROW
            for i, val in enumerate(all_vals):
                if val.strip(): last_row = i + 1
            return max(last_row + 1, FIRST_DATA_ROW)
        except Exception: return FIRST_DATA_ROW

    def batch_append_notices(self, ws, notices, county):
        if not notices: return []
        start_row = self.find_next_empty_row(ws)
        today = date.today().strftime("%m/%d/%Y")
        all_rows = []

        for notice in notices:
            row_data = [""] * 19
            row_data[COL["scrape_date"] - 1] = today
            row_data[COL["county"] - 1] = county
            row_data[COL["notice_url"] - 1] = notice.get("url", "")
            row_data[COL["notice_id"] - 1] = notice.get("id", "")
            row_data[COL["legal_desc"] - 1] = notice.get("legal_desc", "")
            row_data[COL["pub_dates"] - 1] = notice.get("pub_dates", "")
            row_data[COL["attorney"] - 1] = notice.get("attorney", "")
            row_data[COL["phone"] - 1] = notice.get("phone1", "")
            row_data[COL["alt_phone"] - 1] = notice.get("phone2", "")
            row_data[COL["property_address"] - 1] = notice.get("property_address", "")
            row_data[COL["parcel_id"] - 1] = notice.get("parcel_id", "")
            row_data[COL["assessed_value"] - 1] = notice.get("assessed_value", "")

            if notice.get("category") == "Probate":
                row_data[COL["borrower"] - 1] = notice.get("deceased", "")
                row_data[COL["mailing_address"] - 1] = notice.get("petitioner", "")
                row_data[COL["filing_info"] - 1] = notice.get("cause_no", "")
                row_data[COL["auction_date"] - 1] = notice.get("hearing_date", "")
                row_data[COL["auction_time"] - 1] = notice.get("hearing_time", "")
                row_data[COL["auction_location"] - 1] = notice.get("hearing_loc", "")
            else:
                row_data[COL["borrower"] - 1] = notice.get("borrower", "")
                row_data[COL["dot_date"] - 1] = notice.get("dot_date", "")
                row_data[COL["filing_info"] - 1] = notice.get("filing_info", "")
                row_data[COL["auction_date"] - 1] = notice.get("auction_date", "")
                row_data[COL["auction_time"] - 1] = notice.get("auction_time", "")
                row_data[COL["auction_location"] - 1] = notice.get("auction_location", "")

            all_rows.append(row_data)

        end_row = start_row + len(all_rows) - 1
        try:
            ws.update(values=all_rows, range_name=f"A{start_row}:S{end_row}", value_input_option="USER_ENTERED")
            print(f"  Batch metrics posted safely to A{start_row}:S{end_row}")
            return notices
        except Exception as e:
            print(f"    ERROR appending row blocks: {e}")
            return []

    def dedup_existing_data(self, ws):
        try: all_data = ws.get_all_values()
        except Exception: return 0
        if len(all_data) <= FIRST_DATA_ROW: return 0
        seen_ids, rows_to_keep, dupes_removed = set(), [], 0

        for i, row in enumerate(all_data):
            if i < FIRST_DATA_ROW - 1: continue
            if not any(c.strip() for c in row): continue
            notice_id = row[COL["notice_id"] - 1].strip() if len(row) >= COL["notice_id"] else ""
            borrower = row[COL["borrower"] - 1].strip() if len(row) >= COL["borrower"] else ""
            match_key = notice_id if notice_id else f"{borrower}||{ws.title}".lower()

            if match_key in seen_ids:
                dupes_removed += 1
                continue
            seen_ids.add(match_key)
            clean_row = row[:19] if len(row) >= 19 else row + [""] * (19 - len(row))
            rows_to_keep.append(clean_row)

        if dupes_removed > 0:
            start = FIRST_DATA_ROW
            end = start + len(rows_to_keep) - 1
            try:
                ws.update(values=rows_to_keep, range_name=f"A{start}:S{end}", value_input_option="USER_ENTERED")
                if end + 1 <= len(all_data):
                    blank_rows = [[""] * 19] * (len(all_data) - end)
                    ws.update(values=blank_rows, range_name=f"A{end+1}:S{len(all_data)}", value_input_option="USER_ENTERED")
            except Exception: pass
        return dupes_removed

    def sort_by_auction_date(self, ws):
        try:
            all_data = ws.get_all_values()
            if len(all_data) <= FIRST_DATA_ROW: return
            data_rows = [r[:19] + [""] * (19 - len(r[:19])) for r in all_data[FIRST_DATA_ROW - 1:] if any(c.strip() for c in r)]
            def sort_key(row):
                d_str = row[COL["auction_date"] - 1].strip()
                if not d_str: return (1, datetime(2099, 12, 31))
                for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
                    try: return (0, datetime.strptime(d_str, fmt))
                    except ValueError: continue
                return (1, datetime(2099, 12, 31))
            data_rows.sort(key=sort_key)
            ws.update(values=data_rows, range_name=f"A{FIRST_DATA_ROW}:S{FIRST_DATA_ROW + len(data_rows) - 1}", value_input_option="USER_ENTERED")
        except Exception: pass

    def archive_past_auctions(self, county_ws, county):
        today = date.today()
        try:
            all_data = county_ws.get_all_values()
            if len(all_data) <= FIRST_DATA_ROW - 1: return 0
            to_archive, to_keep = [], []
            for i, row in enumerate(all_data):
                if i < FIRST_DATA_ROW - 1: continue
                if not any(c.strip() for c in row): continue
                d_str = row[COL["auction_date"] - 1].strip()
                dt = None
                if d_str:
                    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
                        try: dt = datetime.strptime(d_str, fmt).date(); break
                        except ValueError: continue
                if dt and dt < today: to_archive.append(row[:19] + [""] * (19 - len(row[:19])))
                else: to_keep.append(row[:19] + [""] * (19 - len(row[:19])))
            if not to_archive: return 0
            past_ws = self.spreadsheet.worksheet("Past Auctions")
            p_next = len(past_ws.get_all_values()) + 1
            past_ws.update(values=to_archive, range_name=f"A{p_next}:S{p_next + len(to_archive) - 1}", value_input_option="USER_ENTERED")
            county_ws.update(values=to_keep, range_name=f"A{FIRST_DATA_ROW}:S{FIRST_DATA_ROW + len(to_keep) - 1}", value_input_option="USER_ENTERED")
            blank_rows = [[""] * 19] * len(to_archive)
            county_ws.update(values=blank_rows, range_name=f"A{FIRST_DATA_ROW + len(to_keep)}:S{FIRST_DATA_ROW + len(all_data) - 1}", value_input_option="USER_ENTERED")
            return len(to_archive)
        except Exception: return 0

    def update_summary(self, run_stats):
        try:
            summary = self.spreadsheet.worksheet("Summary")
            now_str = datetime.now().strftime("%m/%d/%Y %I:%M %p CT")
            rows = [
                ["SPU MS PUBLIC NOTICE AI COMMAND CENTER v4.0", "", "", "", "", "", "", "", ""],
                [f"Last Run: {now_str}", "", "", "", "", "", "", "", ""],
                ["", "", "", "", "", "", "", "", ""],
                ["County", "Total Active", "New This Run", "Archived", "Errors", "Pages", "Status", "Duration", "Last Updated"]
            ]
            for c in COUNTIES:
                s = run_stats.get(c, {})
                rows.append([c, str(s.get("total_active", 0)), str(s.get("new_count", 0)), str(s.get("archived", 0)), str(s.get("errors", 0)), "1", s.get("status", "OK"), s.get("duration", ""), now_str])
            summary.update(values=rows, range_name=f"A1:I{len(rows)}", value_input_option="USER_ENTERED")
        except Exception: pass

    def update_run_log(self, run_stats):
        try:
            log_ws = self.spreadsheet.worksheet("Run Log")
            now_str = datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")
            row = [now_str, str(sum(run_stats.get(c, {}).get("new_count", 0) for c in COUNTIES)), str(sum(run_stats.get(c, {}).get("archived", 0) for c in COUNTIES)), str(len(COUNTIES))]
            for c in COUNTIES: row.append(str(run_stats.get(c, {}).get("new_count", 0)))
            row.append("None")
            log_ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception: pass


# ============================================================
# MASTER COORDINATOR RUNTIME
# ============================================================

def run_pipeline():
    print("Launching Engine Matrix runtime processes...")
    try: sheets = SheetHandler()
    except Exception as e: print(f"FATAL Sheet Authentication failure: {e}"); sys.exit(1)

    new_notices, run_stats = {}, {}

    for county in COUNTIES:
        print(f"\n--- Processing Market: {county} ---")
        start_t = time.time()
        ws = sheets.get_tab(county)
        if not ws: continue

        sheets.dedup_existing_data(ws)
        existing_ids = sheets.get_all_existing_ids(ws)

        try:
            parsed_leads = run_browser_pipeline(county, existing_ids)
            
            enriched_leads = []
            for lead in parsed_leads:
                lookup_name = lead.get("borrower") if lead.get("category") == "Foreclosure" else lead.get("deceased")
                
                # Step 1: Query BatchLeads
                print(f"    -> Querying BatchLeads for: {lookup_name}...")
                skip_info = skip_trace_lead(lookup_name, county)
                lead["phone1"] = skip_info.get("phone1", "")
                lead["phone2"] = skip_info.get("phone2", "")
                lead["property_address"] = skip_info.get("property_address", "")
                if lead.get("category") == "Foreclosure":
                    lead["mailing_address"] = skip_info.get("mailing_address", "")
                
                # Step 2: Query Nimble for Delta Computer Systems
                print(f"    -> Querying Nimble for Property Assessor Data...")
                nimble_info = query_nimble_property_data(lookup_name, county)
                lead["parcel_id"] = nimble_info.get("parcel_id", "")
                lead["assessed_value"] = nimble_info.get("assessed_value", "")
                
                enriched_leads.append(lead)

            saved_leads = sheets.batch_append_notices(ws, enriched_leads, county)
            new_notices[county] = saved_leads
        except Exception as e:
            print(f"County Processing Loop Matrix Failure: {e}")
            run_stats[county] = {"status": "FAILED"}
            continue

        archived = sheets.archive_past_auctions(ws, county)
        sheets.sort_by_auction_date(ws)

        try: total_active = len([r for r in ws.get_all_values()[FIRST_DATA_ROW-1:] if any(c.strip() for c in r)])
        except Exception: total_active = 0

        run_stats[county] = {
            "total_active": total_active, "new_count": len(saved_leads), "archived": archived,
            "errors": 0, "status": "OK", "duration": f"{time.time() - start_t:.1f}s"
        }

    sheets.update_summary(run_stats)
    sheets.update_run_log(run_stats)
    print("\nMaster workflow completely successfully.")


if __name__ == "__main__":
    run_pipeline()

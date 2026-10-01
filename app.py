def fetch_live_quotes_safe(instrument_keys, access_token):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    url = "https://api.upstox.com/v2/market-quote/quotes"
    
    # Reduced batch size to 50 keys to prevent URI length limits / gateway dropouts
    batch_size = 50
    batches = [instrument_keys[i:i + batch_size] for i in range(0, len(instrument_keys), batch_size)]
    
    quotes_data = {}
    last_error = None

    for idx, chunk in enumerate(batches):
        keys_param = ",".join(chunk)
        try:
            encoded_params = urllib.parse.urlencode({'instrument_key': keys_param}, safe=',|:')
            res = requests.get(f"{url}?{encoded_params}", headers=headers, timeout=8)
            
            if res.status_code == 200:
                resp_json = res.json()
                if resp_json.get('status') == 'success':
                    quotes_data.update(resp_json.get('data', {}))
                else:
                    last_error = f"API Error Status: {resp_json}"
            elif res.status_code == 401:
                last_error = "HTTP 401 Unauthorized: Your Upstox Access Token has expired or is invalid. Please regenerate it."
                break
            elif res.status_code == 429:
                last_error = "HTTP 429 Rate Limit hit. Pausing..."
                time.sleep(1.0)
                res_retry = requests.get(f"{url}?{encoded_params}", headers=headers, timeout=8)
                if res_retry.status_code == 200 and res_retry.json().get('status') == 'success':
                    quotes_data.update(res_retry.json().get('data', {}))
                else:
                    last_error = f"HTTP 429 Retry Failed: {res_retry.text}"
            else:
                last_error = f"HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_error = str(e)
            
        if idx < len(batches) - 1:
            time.sleep(0.2)

    return quotes_data, last_error

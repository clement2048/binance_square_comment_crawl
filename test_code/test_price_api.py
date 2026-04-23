import requests


r=requests.get('https://api.binance.com/api/v3/klines', params={'symbol':'BTCUSDT','interval':'1h','limit':1}, timeout=10)

print(r.status_code)
print(r.text[:200])
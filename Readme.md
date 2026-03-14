# Proxy Rotator

A self-hosted rotating proxy server. Deploy on Railway, point any tool at `host:port`.

## How it works

- Loads thousands of proxies from `proxies.txt`
- Rotates through them (round-robin or random)
- Exposes a single HTTP/HTTPS proxy endpoint you can use anywhere

## proxies.txt format

One proxy per line. All formats supported:

```
185.199.228.220:7300
http://103.152.112.120:80
http://user:pass@gate.smartproxy.com:7000
```

Lines starting with `#` are ignored.

## Deploy on Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variables (see below)
5. Railway auto-deploys — grab your public URL from the **Settings → Networking** tab

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Port to listen on (Railway sets this automatically) |
| `ROTATION_MODE` | `round-robin` | `round-robin` or `random` |
| `CONNECT_TIMEOUT` | `10` | Seconds before upstream connection times out |
| `AUTH_USER` | _(none)_ | Optional: require Proxy-Authorization (username) |
| `AUTH_PASS` | _(none)_ | Optional: require Proxy-Authorization (password) |
| `PROXIES_FILE` | `proxies.txt` | Path to your proxy list |

## Usage

Once deployed, Railway gives you a public URL like `your-app.up.railway.app`.
Railway also exposes a TCP port — use that as your `host:port`.

**curl:**
```bash
curl -x http://your-app.up.railway.app:8080 https://httpbin.org/ip
```

**Python requests:**
```python
import requests
proxies = {"http": "http://your-app.up.railway.app:8080",
           "https": "http://your-app.up.railway.app:8080"}
r = requests.get("https://httpbin.org/ip", proxies=proxies)
print(r.json())
```

**With auth enabled:**
```bash
curl -x http://user:pass@your-app.up.railway.app:8080 https://httpbin.org/ip
```

## Stats

A lightweight stats endpoint runs on `PORT+1` (e.g. 8081):

```
GET http://your-app.up.railway.app:8081/
→ proxies_loaded: 3500
   requests_ok: 1042
   requests_fail: 18
   rotation_mode: round-robin
```

## Local development

```bash
pip install -r requirements.txt
python server.py
# then:
curl -x http://localhost:8080 https://httpbin.org/ip
```

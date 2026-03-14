import os
import random
from flask import Flask, jsonify

app = Flask(__name__)

def load_proxies(filepath="proxies.txt"):
    proxies = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            host, port, user, pwd = line.split(":")
            proxies.append(f"http://{user}:{pwd}@{host}:{port}")
    return proxies

proxies = load_proxies()

@app.route("/proxy")
def get_proxy():
    return jsonify({"proxy": random.choice(proxies)})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

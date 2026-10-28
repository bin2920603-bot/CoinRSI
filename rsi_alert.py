import os, json, time, requests

OKX = "https://www.okx.com"
TOP_N = 100   # 거래대금 상위 몇 개를 볼지
LOW = 30      # RSI 바닥 기준
STATE = "state/sent.json"


def get(path, params=None):
    r = requests.get(OKX + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def rsi(closes, n=14):
    if len(closes) < n + 2:
        return []
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out = []
    for i in range(n, len(gains) + 1):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def telegram(token, chat, text):
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text},
        timeout=15,
    )


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    first = not os.path.exists(STATE)
    sent = {} if first else json.load(open(STATE))

    rows = []
    for t in get("/api/v5/market/tickers", {"instType": "SWAP"}):
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            rows.append((float(t["volCcy24h"]) * float(t["last"]), t["instId"]))
        except ValueError:
            pass
    rows.sort(reverse=True)

    hits = []
    for vol, inst in rows[:TOP_N]:
        time.sleep(0.15)
        try:
            c = get("/api/v5/market/candles",
                    {"instId": inst, "bar": "1H", "limit": "100"})
        except Exception:
            continue
        c = [x for x in c if x[8] == "1"][::-1]  # 마감된 봉만, 오래된 순
        if len(c) < 20:
            continue
        r = rsi([float(x[4]) for x in c])
        if len(r) < 2:
            continue
        ts = c[-1][0]
        if r[-2] <= LOW < r[-1] and sent.get(inst) != ts:
            name = inst.replace("-USDT-SWAP", "")
            hits.append(f"{name}  RSI {r[-2]:.1f} → {r[-1]:.1f}")
            sent[inst] = ts

    if hits:
        telegram(token, chat, "📈 1시간봉 RSI 바닥 반등\n" + "\n".join(hits))
    elif first:
        telegram(token, chat, "✅ RSI 알림이 켜졌어요. 조건에 맞는 코인이 나오면 알려 드려요.")

    os.makedirs("state", exist_ok=True)
    json.dump(sent, open(STATE, "w"))


if __name__ == "__main__":
    main()

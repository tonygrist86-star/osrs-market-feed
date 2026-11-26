import json
import time
import math
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

DATA = Path("data")
NOW = int(time.time())

LATEST_MAX_AGE = 600
FIVE_MAX_AGE = 600
HOUR_MAX_AGE = 4500
DOWNLOAD_MAX_AGE = 600

def load(name):
    return json.loads((DATA / name).read_text())

def save(name, content):
    path = DATA / name
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(content, indent=2))
    temp.replace(path)

def interval_age(feed, duration):
    stamp = feed.get("timestamp")
    if not isinstance(stamp, (int, float)):
        return None
    return NOW - (stamp + duration)

def fresh(age, maximum):
    return age is not None and 0 <= age <= maximum

def positive(value):
    return isinstance(value, (int, float)) and value > 0

def tax(price):
    return min(5000000, price * 2 // 100)

def scan():
    latest = load("latest.json")
    five = load("5m.json")

    try:
        hour = load("1h.json")
    except (OSError, ValueError):
        hour = {}

    mapping = load("mapping.json")

    if isinstance(mapping, dict):
        mapping = mapping.get("data", mapping)
        if isinstance(mapping, dict):
            mapping = list(mapping.values())

    latest_data = latest["data"]
    five_data = five["data"]
    hour_data = hour.get("data", {})

    updated = datetime.fromisoformat(
        (DATA / "updated.txt").read_text()
        .strip().replace("Z", "+00:00")
    )

    download_age = NOW - updated.timestamp()
    five_age = interval_age(five, 300)
    hour_age = interval_age(hour, 3600)

    download_ok = fresh(download_age, DOWNLOAD_MAX_AGE)
    five_ok = fresh(five_age, FIVE_MAX_AGE)
    hour_ok = fresh(hour_age, HOUR_MAX_AGE)

    mode = "rejected"

    if download_ok and five_ok:
        mode = "normal" if hour_ok else "provisional"

    results = []
    rejected = Counter()
    seen = set()
    evaluated = 0

    for item in mapping:
        item_id = str(item["id"])

        if item_id in seen:
            continue
        seen.add(item_id)

        if mode == "rejected":
            rejected["feed_stale"] += 1
            continue

        l = latest_data.get(item_id, {})
        f = five_data.get(item_id, {})
        h = hour_data.get(item_id, {})

        high = l.get("high")
        low = l.get("low")
        high_time = l.get("highTime")
        low_time = l.get("lowTime")

        if not all(positive(x) for x in
                   [high, low, high_time, low_time]):
            rejected["missing_latest"] += 1
            continue

        if not all(fresh(NOW - t, LATEST_MAX_AGE)
                   for t in [high_time, low_time]):
            rejected["stale_trades"] += 1
            continue

        fh = f.get("avgHighPrice")
        fl = f.get("avgLowPrice")
        hv = f.get("highPriceVolume") or 0
        lv = f.get("lowPriceVolume") or 0

        if not all(positive(x) for x in [fh, fl, hv, lv]):
            rejected["missing_two_sided_volume"] += 1
            continue

        if high <= low or fh <= fl:
            rejected["inconsistent_spread"] += 1
            continue

        if abs(high - fh) / fh > 0.05:
            rejected["unstable_sell_price"] += 1
            continue

        if abs(low - fl) / fl > 0.05:
            rejected["unstable_buy_price"] += 1
            continue

        evaluated += 1
        item_mode = mode

        buy = math.ceil(max(low, fl))
        sell = math.floor(min(high, fh))

        if hour_ok:
            hh = h.get("avgHighPrice")
            hl = h.get("avgLowPrice")
            hhv = h.get("highPriceVolume") or 0
            hlv = h.get("lowPriceVolume") or 0

            if all(positive(x) for x in [hh, hl, hhv, hlv]):
                if hh <= hl:
                    rejected["hourly_inconsistent"] += 1
                    continue

                if abs(fh - hh) / hh > 0.10:
                    rejected["hourly_price_instability"] += 1
                    continue

                if abs(fl - hl) / hl > 0.10:
                    rejected["hourly_price_instability"] += 1
                    continue

                buy = max(buy, math.ceil(hl))
                sell = min(sell, math.floor(hh))
            else:
                item_mode = "provisional"

        if buy <= 0 or sell <= buy:
            rejected["no_spread"] += 1
            continue

        fee = tax(sell)
        net = sell - buy - fee

        if net <= 0:
            rejected["no_net_profit"] += 1
            continue

        roi = net / buy * 100
        volume = min(hv, lv)
        limit = item.get("limit") or 0

        if limit <= 0:
            rejected["unknown_buy_limit"] += 1
            continue

        if item_mode == "provisional" and volume < 100:
            rejected["provisional_low_volume"] += 1
            continue

        budget = 1000000 if item_mode == "provisional" else 3000000

        qty = min(
            limit,
            budget // buy,
            int(volume * 0.10)
        )

        if qty < 1:
            rejected["insufficient_test_capacity"] += 1
            continue

        profit = qty * net

        # Conservative throughput model, not measured fills.
        hourly_capacity = int(volume * 12 * 0.05)

        if item_mode == "normal":
            hourly_capacity = min(
                hourly_capacity,
                int(min(hhv, hlv) * 0.05)
            )

        hourly_qty = min(qty, hourly_capacity)
        gp_hour = hourly_qty * net

        if gp_hour <= 0:
            rejected["low_throughput"] += 1
            continue

        risk = "moderate"

        if volume >= 1000 and roi <= 3:
            risk = "lower_relative_risk"

        if roi > 5 or volume < 100:
            risk = "elevated"

        risk_factor = {
            "lower_relative_risk": 1.0,
            "moderate": 0.7,
            "elevated": 0.35
        }[risk]

        if item_mode == "provisional":
            risk_factor *= 0.5

        results.append({
            "id": int(item_id),
            "item": item["name"],
            "validation": item_mode,
            "buy": buy,
            "sell": sell,
            "tax_per_item": fee,
            "net_per_item": net,
            "roi_percent": round(roi, 3),
            "buy_limit": limit,
            "five_min_high_volume": hv,
            "five_min_low_volume": lv,
            "high_trade_age_seconds": NOW - high_time,
            "low_trade_age_seconds": NOW - low_time,
            "test_quantity": qty,
            "test_cost": qty * buy,
            "estimated_profit": profit,
            "modeled_gp_per_hour": gp_hour,
            "risk": risk,
            "score": round(gp_hour * risk_factor, 2)
        })

    results.sort(
        key=lambda x: (x["score"], x["estimated_profit"]),
        reverse=True
    )

    stamp = datetime.now(timezone.utc).isoformat()

    health = {
        "scanned_at": stamp,
        "validation": mode,
        "mapped_items_screened": len(seen),
        "items_evaluated": evaluated,
        "qualified_opportunities": len(results),
        "download_age_seconds": round(download_age),
        "five_min_timestamp": five.get("timestamp"),
        "five_min_completed_age_seconds": five_age,
        "hour_timestamp": hour.get("timestamp"),
        "hour_completed_age_seconds": hour_age,
        "rejections": dict(rejected)
    }

    save("health.json", health)

    save("opportunities.json", {
        "scanned_at": stamp,
        "validation": mode,
        "total_candidates": len(results),
        "budget_ceiling": 100000000,
        "allocation": {
            "consumables": 70,
            "weapons_armour": 20,
            "experimental": 10
        },
        "opportunities": results[:50]
    })

    print(json.dumps(health, indent=2))

if __name__ == "__main__":
    try:
        scan()
    except Exception as error:
        stamp = datetime.now(timezone.utc).isoformat()

        save("health.json", {
            "scanned_at": stamp,
            "validation": "rejected",
            "error": str(error)
        })

        save("opportunities.json", {
            "scanned_at": stamp,
            "validation": "rejected",
            "opportunities": []
        })

        print("Market validation failed:", error)

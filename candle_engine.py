"""Penjana candle 1 minit untuk carta dashboard.

Server ialah satu-satunya sumber candle yang sudah lengkap. Carta di pelayar
hanya melukis candle ini; baki dan had pengeluaran dikira daripada penutup candle
terakhir yang sama. Nilai Close admin hanya rujukan supaya carta berhenti di
sekitar nilai itu.
"""
import math
import threading
from collections import OrderedDict
from functools import lru_cache

CANDLE_SEC = 60
CLOSE_TOLERANCE_PCT = 0.02
_CACHE_CANDLE_LIMIT = 90_000
_CACHE_SESSION_LIMIT = 16
_cache = OrderedDict()
_cache_lock = threading.RLock()
_CACHE_LOCK_TIMEOUT = 15


class CandleCacheBusy(RuntimeError):
    """Candle generation is temporarily saturated."""

# (had atas minit, baseVolPct, moveMul, momentumMul, highSpikeTh, midSpikeTh,
#  highSpikeMin, highSpikeRange, midSpikeMin, midSpikeRange, medSpikeMin,
#  medSpikeRange, shockTh, shockMin, shockRange, clampHigh, clampNorm)
_PROFILES = (
    (30, 0.030, 2.35, 1.90, 0.80, 0.55, 3.8, 2.8, 2.5, 2.0, 1.4, 1.0, 0.90, 0.012, 0.020, 0.20, 0.12),
    (120, 0.027, 2.15, 1.75, 0.84, 0.62, 3.5, 2.6, 2.3, 1.8, 1.3, 0.9, 0.93, 0.010, 0.018, 0.18, 0.10),
    (360, 0.023, 1.95, 1.55, 0.88, 0.68, 3.1, 2.2, 2.0, 1.5, 1.2, 0.7, 0.95, 0.008, 0.014, 0.16, 0.09),
    (math.inf, 0.020, 1.75, 1.35, 0.91, 0.74, 2.8, 1.8, 1.7, 1.2, 1.1, 0.5, 0.97, 0.006, 0.010, 0.14, 0.08),
)


def _i32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _imul(a, b):
    return _i32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def _hash_seed(text):
    h = 2166136261
    for char in text:
        h ^= ord(char)
        h = _imul(h, 16777619)
    return h & 0xFFFFFFFF


def _mulberry32(seed):
    state = {"seed": _i32(seed)}

    def next_value():
        seed_value = _i32(state["seed"] + 0x6D2B79F5)
        state["seed"] = seed_value
        t = _imul(seed_value ^ ((seed_value & 0xFFFFFFFF) >> 15), 1 | seed_value)
        t = _i32(t + _imul(t ^ ((t & 0xFFFFFFFF) >> 7), 61 | t)) ^ t
        return (_i32(t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296

    return next_value


def _profile(trade_minutes):
    for profile in _PROFILES:
        if trade_minutes <= profile[0]:
            return profile[1:]
    return _PROFILES[-1][1:]


def _build_candles(user_id, start_value, final_target, start_ts, end_ts, now_ts,
                   candles=None, resume=None, index_offset=0):
    session_secs = end_ts - start_ts
    elapsed = min(now_ts, end_ts) - start_ts
    if elapsed < 0:
        return [], (start_value, 0.0)
    total_candles = max(1, math.ceil(session_secs / CANDLE_SEC))
    completed = min(total_candles, int(elapsed // CANDLE_SEC))
    if completed <= 0:
        return [], (start_value, 0.0)

    (base_vol_pct, move_mul, momentum_mul, high_th, mid_th, high_min, high_range,
     mid_min, mid_range, med_min, med_range, shock_th, shock_min, shock_range,
     clamp_high, clamp_norm) = _profile(max(60, session_secs) / 60)

    min_price = max(1, start_value * 0.05)
    session_ended = now_ts >= end_ts
    price = start_value if resume is None else resume[0]
    momentum = 0.0 if resume is None else resume[1]
    seed_base = str(user_id)
    candles = [] if candles is None else candles
    previous_state = (price, momentum)

    for ci in range(index_offset + len(candles), completed):
        previous_state = (price, momentum)
        r1, r2, r3, r4 = (
            _mulberry32(_hash_seed(f"{seed_base}_c{ci * 4 + offset}"))
            for offset in range(4)
        )
        candles_left = max(1, total_candles - ci)
        steady_pull = ((final_target - price) / candles_left) * 0.85

        vol = price * base_vol_pct
        spike_roll = r1()
        if spike_roll > high_th:
            spike = high_min + r2() * high_range
        elif spike_roll > mid_th:
            spike = mid_min + r2() * mid_range
        elif spike_roll > 0.40:
            spike = med_min + r2() * med_range
        else:
            spike = 1.0
        vol *= spike

        bias_roll = r4()
        if bias_roll < 0.425:
            bias = 0.5 + r2() * 0.6
        elif bias_roll < 0.85:
            bias = -(0.5 + r2() * 0.6)
        else:
            bias = (r2() - 0.5) * 0.12
        momentum = momentum * 0.35 + bias * 0.45

        move = (r3() - 0.5) * 2 * vol * move_mul + momentum * vol * momentum_mul

        shock_roll = r4()
        if shock_roll > shock_th:
            shock_dir = 1 if r2() > 0.5 else -1
            move += shock_dir * (price * (shock_min + r3() * shock_range))

        open_price = price
        close = open_price + move + steady_pull

        max_move = price * (clamp_high if spike > 3 else clamp_norm) * spike
        if abs(close - open_price) > max_move:
            close = open_price + math.copysign(1, close - open_price) * max_move
        if close < min_price:
            close = min_price

        # Candle penutup hanya boleh lari sedikit daripada nilai Close rujukan.
        if session_ended and ci == completed - 1:
            tolerance = max(1, start_value * CLOSE_TOLERANCE_PCT)
            close = max(final_target - tolerance, min(final_target + tolerance, close))
            if close < min_price:
                close = min_price

        body = max(abs(close - open_price), price * 0.003)
        wick_roll = r1()
        if wick_roll < 0.22:
            wick_up = body * (0.5 + r2() * 0.6)
            wick_down = body * (0.05 + r3() * 0.1)
        elif wick_roll < 0.44:
            wick_up = body * (0.05 + r2() * 0.1)
            wick_down = body * (0.5 + r3() * 0.6)
        elif wick_roll < 0.66:
            wick_up = body * (0.15 + r2() * 0.3)
            wick_down = body * (0.15 + r3() * 0.3)
        else:
            wick_up = body * (0.25 + r2() * 0.35)
            wick_down = body * (0.25 + r3() * 0.35)
        if spike > 3:
            wick_up *= 1.2
            wick_down *= 1.2
        elif spike > 1.8:
            wick_up *= 1.05
            wick_down *= 1.05

        high = max(open_price, close) + wick_up
        low = max(min_price, min(open_price, close) - wick_down)
        volume = body * (25 + r4() * 100)

        candles.append({
            "t": start_ts + ci * CANDLE_SEC,
            "dur": CANDLE_SEC,
            "o": open_price,
            "c": close,
            "h": high,
            "l": low,
            "v": volume,
            "ci": ci,
        })
        price = close

    return candles, previous_state


def build_candles(user_id, start_value, final_target, start_ts, end_ts, now_ts):
    """Candle lengkap dengan cache terhad; sesi tamat mengira semula candle terakhir."""
    completed = min(
        max(1, math.ceil((end_ts - start_ts) / CANDLE_SEC)),
        max(0, int((min(now_ts, end_ts) - start_ts) // CANDLE_SEC)),
    )
    if not completed:
        return []
    key = (user_id, start_value, final_target, start_ts, end_ts)
    if completed > _CACHE_CANDLE_LIMIT:
        candles, _ = _build_candles(*key, now_ts)
        return candles

    if not _cache_lock.acquire(timeout=_CACHE_LOCK_TIMEOUT):
        raise CandleCacheBusy("Candle generation is temporarily saturated.")
    try:
        cached = _cache.pop(key, None)
        ended = now_ts >= end_ts
        if cached and cached[2] == ended and len(cached[0]) == completed:
            _cache[key] = cached
            return list(cached[0])

        if cached and len(cached[0]) <= completed and not cached[2]:
            candles = list(cached[0][:-1])
            resume = cached[1]
        else:
            candles = []
            resume = None
        while _cache and (
            len(_cache) >= _CACHE_SESSION_LIMIT
            or sum(len(value[0]) for value in _cache.values()) + completed > _CACHE_CANDLE_LIMIT
        ):
            _cache.popitem(last=False)
        candles, previous_state = _build_candles(
            *key, now_ts, candles=candles, resume=resume,
        )
        _cache[key] = (candles, previous_state, ended)
        while len(_cache) > _CACHE_SESSION_LIMIT or sum(len(value[0]) for value in _cache.values()) > _CACHE_CANDLE_LIMIT:
            _cache.popitem(last=False)
        return list(candles)
    finally:
        _cache_lock.release()


def chart_candles(user_id, start_value, final_target, start_ts, end_ts, now_ts):
    """Keep the original financial close while showing an unaligned final partial candle."""
    key = (user_id, start_value, final_target, start_ts, end_ts)
    if now_ts < end_ts or end_ts <= start_ts or (end_ts - start_ts) % CANDLE_SEC == 0:
        return build_candles(*key, now_ts)
    complete = build_candles(*key, end_ts - 0.000001)
    final = final_candle_close(*key)
    if final is None:
        final = final_target
    opening = complete[-1]["c"] if complete else start_value
    complete.append({
        "t": start_ts + len(complete) * CANDLE_SEC,
        "dur": end_ts - (start_ts + len(complete) * CANDLE_SEC),
        "o": opening, "c": final, "h": max(opening, final),
        "l": min(opening, final), "v": abs(final - opening) * 30 + 20,
        "ci": len(complete),
    })
    return complete


def chart_live_candle(user_id, start_value, final_target, start_ts, end_ts, now_ts):
    """Calculate the current point without returning the future candle."""
    if not start_ts <= now_ts < end_ts:
        return None
    key = (user_id, start_value, final_target, start_ts, end_ts)
    if not _cache_lock.acquire(timeout=_CACHE_LOCK_TIMEOUT):
        raise CandleCacheBusy("Candle generation is temporarily saturated.")
    try:
        completed = build_candles(*key, now_ts)
        count = len(completed)
        candle_start = start_ts + count * CANDLE_SEC
        boundary = min(candle_start + CANDLE_SEC, end_ts)
        if count:
            cached = _cache.get(key)
            if cached and len(cached[0]) == count and not cached[2]:
                previous_state = cached[1]
            else:
                _, previous_state = _build_candles(*key, now_ts)
            offset = count - 1
        else:
            previous_state = (start_value, 0.0)
            offset = 0
        if boundary - candle_start < CANDLE_SEC:
            tail, _ = _build_candles(
                *key, end_ts, candles=[], resume=previous_state, index_offset=offset,
            )
            opening = completed[-1]["c"] if completed else start_value
            closing = tail[-1]["c"] if tail else final_target
            points = (opening, closing)
        else:
            tail, _ = _build_candles(
                *key, boundary, candles=[], resume=previous_state, index_offset=offset,
            )
            target = tail[-1]
            points = (target["o"], target["h"], target["l"], target["c"])
    finally:
        _cache_lock.release()
    progress = (now_ts - candle_start) / (boundary - candle_start)
    position = progress * (len(points) - 1)
    segment = min(int(position), len(points) - 2)
    price = points[segment] + (points[segment + 1] - points[segment]) * (position - segment)
    visited = list(points[:segment + 1]) + [price]
    return {
        "t": candle_start, "dur": boundary - candle_start, "o": points[0],
        "c": price, "h": max(visited), "l": min(visited),
        "v": abs(price - points[0]) * 30 + 20, "isLive": True,
    }


@lru_cache(maxsize=4096)
def final_candle_close(user_id, start_value, final_target, start_ts, end_ts):
    """Penutup candle terakhir sesi yang sudah tamat, atau None jika tiada candle."""
    candles = build_candles(user_id, start_value, final_target, start_ts, end_ts, end_ts)
    return candles[-1]["c"] if candles else None

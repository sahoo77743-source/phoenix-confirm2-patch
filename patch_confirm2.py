#!/usr/bin/env python3
"""Save a 1/3 impulse and enter only after the index confirms.
Finds the impulse index check by structure, not by one exact text."""
import os
import shutil
import py_compile

TARGET = "main.py"
BACKUP = "main.py.bak_confirm2"

WAIT = """        ok_idx, why_idx = index_entry_allowed(idx_now, side)
        if not ok_idx:
            direction = str((idx_now or {}).get("direction") or "NEUTRAL").upper()
            agree = int((idx_now or {}).get("agree") or 0)
            same = agree == 1 and (
                (side == "BUY" and direction == "BULL")
                or (side == "SELL" and direction == "BEAR")
            )
            if same:
                _impulse_wait[symbol] = {
                    "side": side,
                    "px": float(price),
                    "pre": float(pre or 0),
                    "chg": float(chg),
                    "volx": float(volx),
                    "ts": time.time(),
                }
                logger.info(
                    f"⏳ IMPULSE wait {symbol} | {direction} {agree}/3 {side} {float(chg):+.2f}%"
                )
                return
            logger.info(f"🚫 IMPULSE index {symbol} | {why_idx}")
            return
"""

HELPERS = '''
_impulse_wait = {}
_impulse_confirm_at = 0.0
_impulse_confirming = False

def impulse_arm_confirm(now: float) -> None:
    global _impulse_confirm_at
    if not _impulse_wait or now < _impulse_confirm_at:
        return
    _impulse_confirm_at = now + 5.0
    asyncio.create_task(impulse_confirm_saved())

async def impulse_confirm_saved() -> None:
    """The burst was saved at 1/3. Enter only after 2/3 if the move still holds."""
    global _impulse_busy, _impulse_confirming
    if _impulse_confirming or _impulse_busy or not _impulse_wait:
        return
    _impulse_confirming = True
    try:
        now = time.time()
        idx = None
        for sym, rec in list(_impulse_wait.items()):
            age = now - float(rec.get("ts") or 0)
            if age > 15.0 * 60.0:
                _impulse_wait.pop(sym, None)
                logger.info(f"⏳ IMPULSE wait expired {sym}")
                continue
            hist = _impulse_hist.get(sym)
            last = float(hist[-1][1]) if hist else 0.0
            anchor = float(rec.get("pre") or 0)
            side = rec.get("side")
            if last <= 0 or anchor <= 0:
                continue
            move = (last - anchor) / anchor * 100.0
            faded = (side == "BUY" and (move < 1.0 or move > 6.0)) or (
                side == "SELL" and (move > -1.0 or move < -6.0)
            )
            if faded:
                _impulse_wait.pop(sym, None)
                logger.info(f"🚫 IMPULSE wait drop {sym} | move={move:+.2f}%")
                continue
            if idx is None:
                idx = await get_index_direction()
            direction = str((idx or {}).get("direction") or "NEUTRAL").upper()
            agree = int((idx or {}).get("agree") or 0)
            same = (side == "BUY" and direction == "BULL") or (side == "SELL" and direction == "BEAR")
            if not same:
                _impulse_wait.pop(sym, None)
                logger.info(f"🚫 IMPULSE wait drop {sym} | {direction} {agree}/3")
                continue
            if agree < 2:
                continue
            if impulse_open_count() >= int(getattr(config, "IMPULSE_MAX_POSITIONS", 1)):
                return
            if any(sym in trades for trades in active_trades.values()):
                _impulse_wait.pop(sym, None)
                continue
            _impulse_wait.pop(sym, None)
            _impulse_busy = True
            _impulse_pending.add(sym)
            logger.info(f"✅ IMPULSE confirmed {sym} | {direction} {agree}/3 move={move:+.2f}%")
            await impulse_enter(sym, side, last, float(rec.get("chg") or move), float(rec.get("volx") or 0), anchor)
            return
    finally:
        _impulse_confirming = False

'''


def fail(msg):
    shutil.copy2(BACKUP, TARGET)
    print(msg)
    print(f"restored from {BACKUP} — bot file was not changed")


def replace_index_block(text):
    start = text.find("async def impulse_enter(")
    if start < 0:
        return None, "no impulse_enter"
    nxt = text.find("\nasync def ", start + 10)
    nxt2 = text.find("\ndef ", start + 10)
    cuts = [x for x in (nxt, nxt2) if x > 0]
    end = min(cuts) if cuts else len(text)
    body = text[start:end]
    mark = body.find("index_entry_allowed(")
    if mark < 0:
        return None, "impulse_enter has no index check"
    line_start = body.rfind("\n", 0, mark) + 1
    lines = body[line_start:].splitlines(keepends=True)
    if len(lines) < 2 or "if not ok_idx" not in lines[1]:
        return None, "index check shape is unexpected"
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    take = 2
    while take < len(lines):
        raw = lines[take]
        if raw.strip() == "":
            take += 1
            continue
        if (len(raw) - len(raw.lstrip(" "))) <= indent:
            break
        take += 1
    new_body = body[:line_start] + WAIT + "".join(lines[take:])
    return text[:start] + new_body + text[end:], f"replaced {take} index lines"


def main():
    if not os.path.exists(TARGET):
        print(f"STOPPED missing {TARGET}")
        return
    shutil.copy2(TARGET, BACKUP)
    text = open(TARGET, encoding="utf-8").read()
    if "async def impulse_confirm_saved" in text and "⏳ IMPULSE wait" in text:
        fail("STOPPED confirm logic is already in this file")
        return
    text, note = replace_index_block(text)
    if text is None:
        fail(f"STOPPED {note}")
        return
    old_sig = "async def impulse_enter(symbol: str, side: str, price: float, chg: float, volx: float) -> None:"
    new_sig = "async def impulse_enter(symbol: str, side: str, price: float, chg: float, volx: float, pre: float = 0.0) -> None:"
    if old_sig in text:
        text = text.replace(old_sig, HELPERS + new_sig, 1)
    elif "pre: float = 0.0" not in text[text.find("async def impulse_enter"):text.find("async def impulse_enter") + 220]:
        fail("STOPPED impulse signature was not found")
        return
    old_call = "asyncio.create_task(impulse_enter(sym, side, px, chg, volx))"
    new_call = "asyncio.create_task(impulse_enter(sym, side, px, chg, volx, prev[1]))"
    if old_call in text:
        text = text.replace(old_call, new_call, 1)
    elif "impulse_enter(sym, side, px, chg, volx, prev[1])" not in text:
        fail("STOPPED impulse call was not found")
        return
    old_loop = """                        for row in data:
                            impulse_scan_row(row, now)
                        touch_activity()
"""
    new_loop = """                        for row in data:
                            impulse_scan_row(row, now)
                        impulse_arm_confirm(now)
                        touch_activity()
"""
    if old_loop in text:
        text = text.replace(old_loop, new_loop, 1)
    elif "impulse_arm_confirm(now)" not in text:
        fail("STOPPED impulse stream hook was not found")
        return
    if "IMPULSE confirmed" not in text or "IMPULSE lean" in text:
        fail("STOPPED result check failed")
        return
    open(TARGET, "w", encoding="utf-8").write(text)
    try:
        py_compile.compile(TARGET, doraise=True)
    except Exception as e:
        fail(f"STOPPED compile: {e}")
        return
    print(f"DONE pieces=4 {note}")
    print(f"backup={BACKUP}")


if __name__ == "__main__":
    main()

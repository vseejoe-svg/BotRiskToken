# ladder/commands.py
from __future__ import annotations
import os, json, textwrap, asyncio
from typing import Any
from .policy import LDR_POLICY, LadderParams

def _on() -> str:
    os.environ["LDR_ENABLE"] = "1"; LDR_POLICY.p.enable = True
    return "✅ Ladder aktiviert."

def _off() -> str:
    os.environ["LDR_ENABLE"] = "0"; LDR_POLICY.p.enable = False
    return "⏸ Ladder deaktiviert."

async def _status_text() -> str:
    from .storage import day_agg_usd, open_positions_count_from_csv
    day_sum, mdd = day_agg_usd()
    open_n = open_positions_count_from_csv()
    p = LDR_POLICY.p
    return "\n".join([
        "🪜 <b>Barbell‑Ladder Status</b>",
        f"enable={p.enable}  | open_pos={open_n}",
        f"core={p.core_notional_sol} SOL | opp={p.opp_notional_sol} SOL | max_concurrent={p.max_concurrent}",
        f"day_loss_stop={p.day_loss_stop_usd} USD | day_mdd_stop={p.day_mdd_stop_usd} USD",
        f"S1: adx≥{p.s1_min_adx} bbw≥{p.s1_min_bbw}",
        f"S2: age≤{p.s2_max_age_min}m  lp≥{p.s2_min_lp_sol}  tx24≥{p.s2_min_tx24}  top10≤{int(p.s2_top10_max*100)}%  block_auth={p.s2_block_auth}",
        f"S3: m5_delta≥{p.s3_m5_delta_min}  qscore≥{p.s3_qscore_min}  sanity≥{p.s3_sanity_min}",
        f"S4: refs≥{p.s4_liqrefs_min}  burst≥+{p.s4_liq_burst_min} in {p.s4_burst_window_s}s",
    ])

async def cmd_ldr_on(update, context):
    if not context or not update: return
    from bot_core import guard, send
    if not guard(update): return
    await send(update, _on())

async def cmd_ldr_off(update, context):
    if not context or not update: return
    from bot_core import guard, send
    if not guard(update): return
    await send(update, _off())

async def cmd_ldr_status(update, context):
    from bot_core import guard, send
    if not guard(update): return
    await send(update, await _status_text())

async def cmd_ldr_config(update, context):
    from bot_core import guard, send
    if not guard(update): return
    p = LDR_POLICY.p
    payload = json.dumps(p.__dict__, indent=2, ensure_ascii=False)
    msg = "⚙️ Ladder‑Config (ENV‑Override möglich):\n<pre>{}</pre>".format(payload)
    await send(update, msg)

async def cmd_ldr_test(update, context):
    """Schnelltest gegen einen Mint: zeigt Gate‑Entscheidung + Bucket."""
    from bot_core import guard, send, ENGINES
    if not guard(update): return
    if not context.args:
        return await send(update, "Nutzung: /ldr_test <MINT>")
    mint = context.args[0].strip()
    eng = ENGINES.get(mint)
    if not eng:
        return await send(update, "Kein Engine‑State vorhanden (Mint erst in WATCHLIST aufnehmen und /auto starten).")
    allow, why, bucket = await LDR_POLICY.allow_entry(mint, {"close":0}, eng.last_diag)
    await send(update, f"🔍 Ladder {mint[:6]}…  allow={allow}  bucket={bucket}  reason={why}")

def register_ladder_commands(app):
    """
    app: telegram.ext.Application
    Registiert die Handler – Aufruf in build_app() nach den anderen Commands.
    """
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("ldr_on",     cmd_ldr_on))
    app.add_handler(CommandHandler("ldr_off",    cmd_ldr_off))
    app.add_handler(CommandHandler("ldr_status", cmd_ldr_status))
    app.add_handler(CommandHandler("ldr_config", cmd_ldr_config))
    app.add_handler(CommandHandler("ldr_test",   cmd_ldr_test))

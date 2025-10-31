# ladder/policy.py
from __future__ import annotations
import os, time, asyncio
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, Any

from .storage import day_agg_usd, open_positions_count_from_csv

# ---------- ENV / Defaults ----------
def _env_float(k: str, d: float) -> float:
    try: return float(os.getenv(k, d))
    except: return d

def _env_int(k: str, d: int) -> int:
    try: return int(os.getenv(k, d))
    except: return d

def _env_bool(k: str, d: bool) -> bool:
    v = (os.getenv(k, str(int(d))) or "").lower()
    return v in ("1","true","yes","on")

@dataclass
class LadderParams:
    # --- Risk (Barbell) ---
    enable: bool = _env_bool("LDR_ENABLE", True)
    core_notional_sol: float = _env_float("LDR_CORE_NOTIONAL_SOL", 0.05)
    opp_notional_sol:  float = _env_float("LDR_OPP_NOTIONAL_SOL", 0.02)
    max_concurrent:    int   = _env_int  ("LDR_MAX_CONCURRENT", 4)
    day_loss_stop_usd: float = _env_float("LDR_DAY_LOSS_STOP_USD", 60.0)   # täglicher Realized Verlust
    day_mdd_stop_usd:  float = _env_float("LDR_DAY_MDD_STOP_USD", 80.0)    # täglicher Max Drawdown
    kill_on_rug:       bool  = _env_bool("LDR_KILL_ON_RUG", True)

    # --- S1 Momentum + Liquidity ---
    s1_min_adx:        float = _env_float("LDR_S1_MIN_ADX", 12.0)
    s1_min_bbw:        float = _env_float("LDR_S1_MIN_BBW", 0.30)   # Bandwidth (cfg.bbw_min)
    s1_min_vol1m_usd:  float = _env_float("LDR_S1_MIN_VOL1M_USD", 50.0)  # grob

    # --- S2 New-Listing + Rug Filter ---
    s2_max_age_min:    int   = _env_int  ("LDR_S2_MAX_AGE_MIN", 240)
    s2_min_lp_sol:     float = _env_float("LDR_S2_MIN_LP_SOL", 0.5)
    s2_min_tx24:       int   = _env_int  ("LDR_S2_MIN_TX24", 50)
    s2_top10_max:      float = _env_float("LDR_S2_TOP10_MAX", 0.45)  # ≤ 45%
    s2_block_auth:     bool  = _env_bool ("LDR_S2_BLOCK_AUTH", True)

    # --- S3 Sentiment + On-chain Flow ---
    s3_m5_delta_min:   int   = _env_int  ("LDR_S3_M5_DELTA_MIN", 8)       # buys - sells
    s3_qscore_min:     int   = _env_int  ("LDR_S3_QSCORE_MIN", 35)        # DexScreener QScore, grob
    s3_sanity_min:     int   = _env_int  ("LDR_S3_SANITY_MIN", 60)        # unser Sanity-Score

    # --- S4 Liquidity Shock ---
    s4_liqrefs_min:    int   = _env_int  ("LDR_S4_LIQREFS_MIN", 20)
    s4_liq_burst_min:  int   = _env_int  ("LDR_S4_LIQ_BURST_MIN", 3)      # +X neue Refs in Y min
    s4_burst_window_s: int   = _env_int  ("LDR_S4_BURST_WINDOW_S", 180)

    # --- global Cooldowns ---
    entry_cooldown_s:  int   = _env_int  ("LDR_ENTRY_COOLDOWN_S", 60)

class BarbellPolicy:
    """
    „Gatekeeper“ VOR jedem ENTRY.
    Verwendet vorhandene Helfer aus bot_core: 
      - sanity_check_token(mint)           -> Score + Issues
      - count_liquidity_refs(mint)         -> #AMMs/Markets
      - dexscreener_meta(mint) / metrics   -> age, m5, volume24, qscore, ...
    """
    def __init__(self):
        self.p = LadderParams()
        self._last_entry_ts: Dict[str, float] = {}
        # einfache Historie für S4
        self._liq_hist: Dict[str, list[Tuple[int,int]]] = {}  # {mint: [(ts, refs), ...]}

    # ------- EXTERNE FUNKTIONEN werden lazy importiert (um Zyklen zu vermeiden) -------
    def _mod(self):
        # Alles aus deinem bot_core importieren, wenn gebraucht
        import importlib
        bc = importlib.import_module("bot_core")
        return bc

    async def _dex_meta(self, mint: str) -> Dict[str, Any]:
        bc = self._mod()
        name, ag = bc.dexscreener_token_meta(mint)
        # DexScreener 1m approx, tx, age etc. holen (best effort)
        try:
            # viele deiner Utils liefern „combined“ Strukturen – hier reduzieren wir
            age_min = int((ag or {}).get("age_min") or 0)
            m5b     = int((ag or {}).get("m5_buys")   or 0)
            m5s     = int((ag or {}).get("m5_sells")  or 0)
            vol24   = float((ag or {}).get("vol24_usd") or 0.0)
            qscore  = int((ag or {}).get("score") or (ag or {}).get("qscore") or 0)
            lp_sol  = float((ag or {}).get("lp_sol") or 0.0)
        except Exception:
            age_min, m5b, m5s, vol24, qscore, lp_sol = 0,0,0,0.0,0,0.0
        return {"name": name, "age_min": age_min, "m5b": m5b, "m5s": m5s, "vol24": vol24, "qscore": qscore, "lp_sol": lp_sol}

    async def _sanity(self, mint: str) -> Dict[str, Any]:
        bc = self._mod()
        try:
            return await bc.sanity_check_token(mint)
        except Exception:
            return {"ok": False, "score": 0, "issues": [], "metrics": {}}

    def _engine_diag_ok(self, diag: Dict[str, Any]) -> bool:
        # S1 Momentum/Vol: nutzt Felder aus deinem SwingBotV163 (momo_ok, bo_ok, adx, gate_pb, vol_ok, bbw)
        adx  = float(diag.get("adx") or 0.0)
        bbw  = float(diag.get("bbw") or diag.get("bbw_val") or 0.0)
        vol_ok = bool(diag.get("vol_ok"))
        momo = bool(diag.get("momo_ok"))
        bo   = bool(diag.get("bo_ok"))
        return (adx >= self.p.s1_min_adx) and (bbw >= self.p.s1_min_bbw) and vol_ok and (momo or bo)

    async def _s2_rugfilter_ok(self, mint: str, meta: Dict[str,Any], sanity: Dict[str,Any]) -> bool:
        if meta["age_min"] > self.p.s2_max_age_min:
            return True  # kein New‑Listing -> Rug‑Filter lockerer
        mtr = sanity.get("metrics") or {}
        top10 = float(mtr.get("top10_share") or 0.0)
        if top10 > self.p.s2_top10_max: return False
        if self.p.s2_block_auth:
            if mtr.get("freezeAuthority") or mtr.get("mintAuthority"):
                return False
        if meta["lp_sol"] < self.p.s2_min_lp_sol: return False
        if int(mtr.get("tx24") or meta.get("m5b",0)+meta.get("m5s",0)) < self.p.s2_min_tx24: 
            return False
        return True

    async def _s3_sentiment_ok(self, meta: Dict[str,Any], sanity: Dict[str,Any]) -> bool:
        delta = int(meta["m5b"]) - int(meta["m5s"])
        if delta < self.p.s3_m5_delta_min: return False
        if int(meta["qscore"]) < self.p.s3_qscore_min: return False
        if int(sanity.get("score") or 0) < self.p.s3_sanity_min: return False
        return True

    async def _s4_liqshock_ok(self, mint: str) -> bool:
        bc = self._mod()
        try:
            refs = int(await bc.count_liquidity_refs(mint))
        except Exception:
            refs = 0
        now = int(time.time())
        hist = self._liq_hist.setdefault(mint, [])
        hist.append((now, refs))
        # alte Punkte verwerfen
        lim = now - self.p.s4_burst_window_s
        self._liq_hist[mint] = [(t,r) for (t,r) in hist if t >= lim]
        if refs < self.p.s4_liqrefs_min:
            return False
        if len(self._liq_hist[mint]) >= 2:
            r0 = self._liq_hist[mint][0][1]
            r1 = self._liq_hist[mint][-1][1]
            if (r1 - r0) >= self.p.s4_liq_burst_min:
                return True
        # auch ohne Burst okay, wenn Basis >= min
        return refs >= max(self.p.s4_liqrefs_min, 1)

    def _day_risk_ok(self) -> Tuple[bool,str]:
        day_sum, mdd = day_agg_usd()
        if float(day_sum) <= -abs(self.p.day_loss_stop_usd):
            return False, f"DayLoss {day_sum} ≤ -{self.p.day_loss_stop_usd}"
        if float(mdd) <= -abs(self.p.day_mdd_stop_usd):
            return False, f"DayMDD {mdd} ≤ -{self.p.day_mdd_stop_usd}"
        if open_positions_count_from_csv() >= self.p.max_concurrent:
            return False, f"MaxConcurrent ≥ {self.p.max_concurrent}"
        return True, "OK"

    def _cool_ok(self, mint: str) -> bool:
        now = time.time()
        last = float(self._last_entry_ts.get(mint) or 0.0)
        return (now - last) >= self.p.entry_cooldown_s

    # ---------------- public API ----------------
    async def allow_entry(self, mint: str, bar: Dict[str,Any], engine_diag: Dict[str,Any]) -> Tuple[bool,str,str]:
        """
        returns: (allowed, reason, bucket) 
        bucket = 'core' | 'opp'
        """
        if not self.p.enable:
            return False, "Disabled", "opp"
        if not self._cool_ok(mint):
            return False, "Cooldown", "opp"

        ok, why_not = self._day_risk_ok()
        if not ok:
            return False, why_not, "opp"

        if not self._engine_diag_ok(engine_diag):
            return False, "S1(momo/vol/ADX) fail", "opp"

        meta    = await self._dex_meta(mint)
        sanity  = await self._sanity(mint)
        if not sanity.get("ok", False) or int(sanity.get("score") or 0) < self.p.s3_sanity_min:
            return False, "Sanity fail", "opp"

        # S2 Rug-Filter (strenger wenn jung)
        if not await self._s2_rugfilter_ok(mint, meta, sanity):
            if self.p.kill_on_rug:
                return False, "RugFilter block", "opp"
            else:
                return False, "RugFilter warn", "opp"

        # S3 Sentiment/Flows
        if not await self._s3_sentiment_ok(meta, sanity):
            return False, "S3(senti/flow) fail", "opp"

        # S4 Liquidity‑Shock (optional positiv)
        s4_ok = await self._s4_liqshock_ok(mint)

        bucket = "core" if (meta["age_min"] > self.p.s2_max_age_min and s4_ok) else "opp"
        return True, "OK", bucket

    def notional_for_bucket(self, bucket: str, default_notional_sol: float) -> float:
        if bucket == "core":
            return float(os.getenv("LDR_CORE_NOTIONAL_SOL", self.p.core_notional_sol) or self.p.core_notional_sol)
        return float(os.getenv("LDR_OPP_NOTIONAL_SOL",  self.p.opp_notional_sol)  or self.p.opp_notional_sol)

    def mark_entry(self, mint: str):
        self._last_entry_ts[mint] = time.time()

# Singleton (bequem in bot_core nutzbar)
LDR_POLICY = BarbellPolicy()

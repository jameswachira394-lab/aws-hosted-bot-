"""
mt5/position_manager.py
=======================
Monitors all open positions placed by this bot.
Responsibilities:
  - Detect TP1 hit → move SL to breakeven
  - Track open trade count
  - Report open P&L
  - Emergency close all positions
"""

from utils.logger import get_logger, log_trade
from utils import notifier

log = get_logger("position_manager")


class PositionManager:
    """
    Monitors positions opened by this bot (filtered by magic number).
    Call monitor() on every position-check cycle.
    """

    def __init__(self, cfg_exec: dict, cfg_risk: dict, pip_size: float = 0.0001):
        self.magic    = cfg_exec.get('magic_number', 202501)
        self.cfg_risk = cfg_risk
        self.pip      = pip_size
        # ticket → tp1 price (set when order is placed, cleared after BE move)
        self._tp1_map: dict[int, float] = {}

    # ── public ────────────────────────────────

    def register_trade(self, ticket: int, tp1: float):
        """Call this right after a new order is filled to register its TP1."""
        self._tp1_map[ticket] = tp1
        log.debug(f"Registered TP1={tp1:.5f} for ticket #{ticket}")

    def monitor(self, executor) -> list[dict]:
        """
        Check all open positions for this bot.
        Moves SL to breakeven when TP1 is hit.
        Returns list of position summary dicts.
        """
        positions = self._get_positions()
        summaries = []

        for pos in positions:
            tick = self._get_tick(pos.symbol)
            if tick is None:
                continue

            current_price = tick.bid if pos.type == 0 else tick.ask  # 0=BUY,1=SELL
            entry         = pos.price_open
            sl            = pos.sl
            tp1           = self._tp1_map.get(pos.ticket)

            pips = (
                (current_price - entry) / self.pip if pos.type == 0
                else (entry - current_price) / self.pip
            )

            summary = {
                'ticket':    pos.ticket,
                'symbol':    pos.symbol,
                'type':      'BUY' if pos.type == 0 else 'SELL',
                'volume':    pos.volume,
                'entry':     entry,
                'sl':        sl,
                'current':   current_price,
                'pips':      round(pips, 1),
                'profit':    pos.profit,
            }
            summaries.append(summary)

            # ── Move SL to breakeven after TP1 ────────────────
            if (tp1 is not None
                    and self.cfg_risk.get('move_be_at_tp1', True)
                    and not self._is_at_be(sl, entry)):
                tp1_hit = (
                    (pos.type == 0 and current_price >= tp1) or
                    (pos.type == 1 and current_price <= tp1)
                )
                if tp1_hit:
                    log.info(
                        f"TP1 hit for #{pos.ticket} | Moving SL to breakeven {entry:.5f}"
                    )
                    success = executor.modify_sl(pos, entry)
                    if success:
                        del self._tp1_map[pos.ticket]   # one-time BE move
                        log_trade(
                            action="MODIFY_BE", symbol=pos.symbol,
                            direction=summary['type'], volume=pos.volume,
                            entry=entry, sl=entry, tp1=tp1, tp2=pos.tp,
                            ticket=pos.ticket
                        )
                        notifier.send(
                            f"⚪ SL moved to breakeven | "
                            f"{pos.symbol} #{pos.ticket} | TP1 reached"
                        )

        return summaries

    def open_count(self) -> int:
        """Number of open positions belonging to this bot."""
        return len(self._get_positions())

    def close_all(self, executor) -> int:
        """Emergency: close every open position. Returns count closed."""
        positions = self._get_positions()
        closed = 0
        for pos in positions:
            if executor.close_position(pos):
                closed += 1
                notifier.alert_close(
                    pos.symbol, pos.ticket,
                    pos.profit / (pos.volume * 10),   # rough pips
                    "MANUAL_CLOSE"
                )
        log.info(f"Emergency close: {closed}/{len(positions)} positions closed.")
        return closed

    # ── private ───────────────────────────────

    def _get_positions(self) -> list:
        mt5 = self._get_mt5()
        all_pos = mt5.positions_get()
        if all_pos is None:
            return []
        return [p for p in all_pos if p.magic == self.magic]

    def _get_tick(self, symbol: str):
        mt5 = self._get_mt5()
        return mt5.symbol_info_tick(symbol)

    def _is_at_be(self, sl: float, entry: float, tol: float = 0.00003) -> bool:
        """Returns True if SL is already at / near breakeven."""
        return abs(sl - entry) < tol

    def _get_mt5(self):
        from mt5.connector import get_connector
        return get_connector().get_mt5()

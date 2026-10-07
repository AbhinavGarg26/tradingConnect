"""Broker adapters for the one-time recovery-GTT rule engine.

Each position retains one application-managed GTT.  A later eligible movement
modifies that GTT instead of adding a second full-quantity sell instruction.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import text

from database.live_market_state import upsert_live_metric
from market.groww_entry_price import execution_entry_price
from market.kite_ltp import live_prices
from market.recovery_gtt import RecoveryGttState, evaluate


def _tick(price: float) -> float:
    return float((Decimal(str(price)) / Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("0.05"))


def _key(provider: str, user_id: int, position: dict) -> str:
    return ":".join((provider, str(user_id), str(position.get("exchange", "")), str(position.get("tradingsymbol", "")), str(position.get("product", ""))))


def _load(db, key: str) -> dict:
    value = db.execute(text("""
        SELECT payload FROM market_live_state
        WHERE entity_type = 'POSITION' AND entity_key = :key
          AND metric_type = 'RECOVERY_GTT' AND metric_key = 'state'
    """), {"key": key}).scalar()
    return json.loads(value) if isinstance(value, str) else (value or {})


def _save(db, key: str, state: RecoveryGttState, **extra) -> None:
    upsert_live_metric(
        db, entity_type="POSITION", entity_key=key, metric_type="RECOVERY_GTT",
        metric_key="state", payload={**state.payload(), **extra}, numeric_value=state.peak_pct,
    )


class ZerodhaRecoveryGttExecutor:
    def __init__(self, kite, logger, user_id: int, allow_loss_recovery: bool = True):
        self.kite, self.logger, self.user_id = kite, logger, user_id
        self.allow_loss_recovery = allow_loss_recovery

    def process(self, db, position: dict, entry_price: float, ltp: float) -> None:
        key = _key("zerodha", self.user_id, position)
        payload = _load(db, key)
        state = RecoveryGttState.from_payload(payload)
        event = evaluate(entry_price, ltp, state, allow_loss_recovery=self.allow_loss_recovery)
        if not event:
            _save(db, key, state, gtt_id=payload.get("gtt_id"))
            return
        trigger = _tick(entry_price * (1 + event.trigger_pct / 100))
        order = [{"transaction_type": self.kite.TRANSACTION_TYPE_SELL, "quantity": int(position["quantity"]), "product": position["product"], "order_type": self.kite.ORDER_TYPE_LIMIT, "price": trigger}]
        gtt_id = payload.get("gtt_id")
        try:
            if gtt_id:
                self.kite.modify_gtt(trigger_id=gtt_id, trigger_type=self.kite.GTT_TYPE_SINGLE, tradingsymbol=position["tradingsymbol"], exchange=position["exchange"], trigger_values=[trigger], last_price=ltp, orders=order)
                action = "replaced"
            else:
                response = self.kite.place_gtt(trigger_type=self.kite.GTT_TYPE_SINGLE, tradingsymbol=position["tradingsymbol"], exchange=position["exchange"], trigger_values=[trigger], last_price=ltp, orders=order)
                gtt_id, action = response["trigger_id"], "placed"
            _save(db, key, state, gtt_id=gtt_id, event=event.key, trigger_price=trigger)
            self.logger.warning("[%s] Recovery GTT %s at ₹%.2f (%s)", position["tradingsymbol"], action, trigger, event.key)
        except Exception as exc:
            # Never create a second order after an uncertain broker response.
            self.logger.exception("[%s] Recovery GTT %s failed: %s", position["tradingsymbol"], event.key, exc)


class GrowwRecoveryGttExecutor:
    """Groww smart-GTT adapter; caller supplies the encrypted-link access token."""
    BASE_URL = "https://api.groww.in/v1"

    def __init__(self, access_token: str, logger, user_id: int, allow_loss_recovery: bool = True):
        self.access_token, self.logger, self.user_id = access_token, logger, user_id
        self.allow_loss_recovery = allow_loss_recovery

    def process(self, db, position: dict, entry_price: float, ltp: float) -> None:
        key = _key("groww", self.user_id, position)
        payload = _load(db, key)
        state = RecoveryGttState.from_payload(payload)
        event = evaluate(entry_price, ltp, state, allow_loss_recovery=self.allow_loss_recovery)
        if not event:
            _save(db, key, state, gtt_id=payload.get("gtt_id"))
            return
        trigger = _tick(entry_price * (1 + event.trigger_pct / 100))
        segment = position["segment"]
        direction = "DOWN" if trigger < ltp else "UP"
        order = {"order_type": "LIMIT", "price": f"{trigger:.2f}", "transaction_type": "SELL"}
        gtt_id = payload.get("gtt_id")
        try:
            if gtt_id:
                self._request("PUT", f"/order-advance/modify/{gtt_id}", {"smart_order_type": "GTT", "segment": segment, "quantity": int(position["quantity"]), "trigger_price": f"{trigger:.2f}", "trigger_direction": direction, "order": order})
                action = "replaced"
            else:
                reference_id = f"RG{str(self.user_id)[-4:]}{abs(hash(key + event.key)) % 10_000_000:07d}"
                response = self._request("POST", "/order-advance/create", {"reference_id": reference_id, "smart_order_type": "GTT", "segment": segment, "trading_symbol": position["tradingsymbol"], "quantity": int(position["quantity"]), "trigger_price": f"{trigger:.2f}", "trigger_direction": direction, "order": order, "product_type": position["product"], "exchange": position["exchange"], "duration": "DAY"})
                gtt_id, action = response["smart_order_id"], "placed"
            _save(db, key, state, gtt_id=gtt_id, event=event.key, trigger_price=trigger)
            self.logger.warning("[%s] Groww recovery GTT %s at ₹%.2f (%s)", position["tradingsymbol"], action, trigger, event.key)
        except Exception as exc:
            self.logger.exception("[%s] Groww recovery GTT %s failed: %s", position["tradingsymbol"], event.key, exc)

    def _request(self, method: str, path: str, body: dict) -> dict:
        request = Request(f"{self.BASE_URL}{path}", data=json.dumps(body).encode(), method=method)
        request.add_header("Authorization", f"Bearer {self.access_token}")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-API-VERSION", "1.0")
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("message") or parsed.get("error") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise RuntimeError(f"Groww {path} failed (HTTP {exc.code}): {detail}") from exc
        if payload.get("status") != "SUCCESS":
            raise RuntimeError(payload.get("message") or "Groww GTT request failed")
        return payload["payload"]

    def _get(self, path: str, params: dict) -> dict:
        request = Request(f"{self.BASE_URL}{path}?{urlencode(params)}")
        request.add_header("Authorization", f"Bearer {self.access_token}")
        request.add_header("Accept", "application/json")
        request.add_header("X-API-VERSION", "1.0")
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read())
        if payload.get("status") != "SUCCESS":
            raise RuntimeError(payload.get("message") or "Groww request failed")
        return payload["payload"]


class GrowwRecoveryGttMonitor:
    """Poll active Groww long positions and arm recovery GTTs at most every 10s."""
    def __init__(self, logger, user_id: int, kite, interval_seconds: float = 10.0, allow_loss_recovery: bool = True):
        self.logger, self.user_id, self.kite, self.interval_seconds = logger, user_id, kite, interval_seconds
        self.allow_loss_recovery = allow_loss_recovery
        self._last_run = 0.0

    def run_if_due(self, db, monotonic_now: float) -> None:
        if monotonic_now - self._last_run < self.interval_seconds:
            return
        self._last_run = monotonic_now
        try:
            from trading.exchange_link import ExchangeLinkRepo
            link = ExchangeLinkRepo.get_for_user(db, self.user_id, provider="groww")
            if not link or not link.is_session_valid:
                return
            client = GrowwRecoveryGttExecutor(link.decrypt_session_token(db), self.logger, self.user_id, allow_loss_recovery=self.allow_loss_recovery)
            positions = []
            orders_by_segment = {}
            for segment in ("CASH", "FNO"):
                positions.extend(client._get("/positions/user", {"segment": segment}).get("positions", []))
                orders_by_segment[segment] = client._get(
                    "/order/list", {"segment": segment, "page": 0, "page_size": 100}
                ).get("order_list", [])
            open_rows = [row for row in positions if int(row.get("quantity") or row.get("net_quantity") or 0) > 0]
            prices = live_prices(self.kite, open_rows)
            for row in open_rows:
                quantity = int(row.get("quantity") or row.get("net_quantity") or 0)
                segment = str(row.get("segment") or "FNO").upper()
                symbol = row.get("trading_symbol") or row.get("symbol")
                exchange = row.get("exchange") or "NSE"
                entry = execution_entry_price(orders_by_segment.get(segment, []), row)
                if not symbol or entry is None or entry <= 0:
                    continue
                ltp = prices.get((str(exchange).upper(), segment, str(symbol)), 0)
                if ltp <= 0:
                    continue
                client.process(db, {"exchange": exchange, "segment": segment, "tradingsymbol": symbol, "product": row.get("product") or row.get("product_type") or ("NRML" if segment == "FNO" else "CNC"), "quantity": quantity}, entry, ltp)
        except Exception as exc:
            self.logger.warning("Groww recovery-GTT check failed: %s", exc)

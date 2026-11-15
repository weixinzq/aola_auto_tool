"""Account-local battle identity, entry readiness, and lifecycle state."""
from __future__ import annotations

import math
from collections import deque


class BattleLifecycle:
    """Persistent battle gate for one logged-in account."""

    def __init__(self) -> None:
        self.diagnostic_callback = None
        self.phase = "idle"
        self.battle_epoch = 0
        self.battle_id: int | None = None
        self.battle_unique_id: int | float | None = None
        self._retired_battle_unique_id: int | float | None = None
        self._unconfirmed_battle: tuple[
            int, int | float | None, int | None, int | None, set[str]
        ] | None = None
        self.end_request_sent = False
        self.current_turn: int | None = None
        self.room_id: int | None = None
        self.last_error = ""
        self.entry_responses: set[str] = set()
        self._abandoned_ids = deque(maxlen=64)
        self._abandoned_unique_ids = deque(maxlen=64)
        self._ignore_orphans = False
        # Matching outlives an execution round, including local timeouts/cancellation.
        self.match_pending = False
        self.match_reply: dict | None = None
        self.match_deadline: float | None = None
        self.prepare_deadline: float | None = None
        self.match_epoch: int | None = None
        self.match_cancel_requested = False
        self.match_cancelled = False
        # Settlement can arrive after 2403 or between script executions.
        self.settlement_serial = 0
        self.settlement_pending = False
        self.settlement_reply: dict | None = None
        self.settlement_ambiguous = False
        self.require_match_settlement = False

    def clear_match(self) -> None:
        self.match_pending = False
        self.match_reply = None
        self.match_deadline = None
        self.prepare_deadline = None
        self.match_epoch = None
        self.match_cancel_requested = False
        self.match_cancelled = False

    def discard_match_settlement(self) -> None:
        self.settlement_pending = False
        self.settlement_reply = None

    def is_stale(self, message: dict) -> bool:
        if message.get("battleId") is not None and message["battleId"] in self._abandoned_ids:
            return True
        if message.get("battleUniqueId") is not None and message["battleUniqueId"] in self._abandoned_unique_ids:
            return True
        return self._ignore_orphans and str(message.get("_cmd", "")) in {
            "2304", "2305", "2401", "2426", "2402", "2403", "2405", "2411", "2423",
        }

    def abandon(self) -> None:
        if self.battle_id is not None:
            self._abandoned_ids.append(self.battle_id)
        if self.battle_unique_id is not None:
            self._abandoned_unique_ids.append(self.battle_unique_id)
        if self._unconfirmed_battle is not None:
            self._abandoned_ids.append(self._unconfirmed_battle[0])
            if self._unconfirmed_battle[1] is not None:
                self._abandoned_unique_ids.append(self._unconfirmed_battle[1])
        self.finish_without_confirmation()
        self._unconfirmed_battle = None
        self.last_error = ""
        self._ignore_orphans = True

    @property
    def entry_ready(self) -> bool:
        return self.phase == "active" and {"2303", "2401", "2426", "2402"}.issubset(
            self.entry_responses
        )

    def begin_entry(self, room_id: int) -> None:
        if self.phase in {"entering", "unknown"}:
            raise RuntimeError(f"当前战斗状态为 {self.phase}，不能提交新的 54_22")
        self.match_cancelled = False
        if self.battle_unique_id is not None:
            self._retired_battle_unique_id = self.battle_unique_id
        self._unconfirmed_battle = None
        self.phase = "entering"
        self.battle_id = None
        self.battle_unique_id = None
        self.end_request_sent = False
        self.current_turn = None
        self.room_id = room_id
        self.last_error = ""
        self.entry_responses.clear()

    def mark_end_requested(self) -> None:
        self.end_request_sent = True
        self.last_error = ""

    def finish_without_confirmation(self) -> None:
        if self.phase in {"active", "ending"} and self.battle_id is not None and {
            "2303", "2401", "2426", "2402"
        }.issubset(self.entry_responses):
            self._unconfirmed_battle = (
                self.battle_id,
                self.battle_unique_id,
                self.current_turn,
                self.room_id,
                set(self.entry_responses),
            )
        else:
            self._unconfirmed_battle = None
        if self.battle_unique_id is not None:
            self._retired_battle_unique_id = self.battle_unique_id
        self.phase = "idle"
        self.battle_id = None
        self.battle_unique_id = None
        self.current_turn = None
        self.room_id = None
        self.end_request_sent = False
        self.entry_responses.clear()

    def mark_unknown(self, reason: str) -> None:
        self.phase = "unknown"
        self.last_error = reason
        self._unconfirmed_battle = None

    def observe(self, message: dict) -> None:
        if self.is_stale(message):
            return
        cmd = str(message.get("_cmd", ""))
        if cmd == "SA231229_CmdMatch":
            if self.match_reply is None:
                self.settlement_serial += 1
                if self.settlement_pending:
                    self.settlement_ambiguous = True
                self.settlement_pending = True
                self.settlement_reply = None
            self.match_cancelled = False
            if self.match_epoch is None:
                self.match_epoch = self.battle_epoch
            self.match_pending = True
            self.match_reply = dict(message)
            return
        if cmd == "SA231229_fe":
            if self.settlement_pending and not self.settlement_ambiguous and all(
                not isinstance(message.get(key), bool)
                and isinstance(message.get(key), (int, float))
                and (not isinstance(message[key], float) or math.isfinite(message[key]))
                for key in ("sc", "hsc", "wstc")
            ):
                self.settlement_reply = dict(message)
                self.settlement_pending = False
            return
        if cmd == "SA231229_match":
            # These replies reject the request; a success already observed wins.
            if message.get("r") in (-9, -10) and self.match_reply is None and self.phase == "idle":
                self.match_pending = False
            return
        if cmd == "SA231229_cancle":
            if (message.get("r") == 1 and self.match_cancel_requested
                    and self.phase == "idle" and self.battle_epoch == self.match_epoch):
                self.discard_match_settlement()
                self.clear_match()
                self.match_cancelled = True
            return
        if self.phase == "idle" and self._unconfirmed_battle is not None and cmd in {
            "2405", "2411", "2402", "2423"
        }:
            (
                self.battle_id,
                self.battle_unique_id,
                self.current_turn,
                self.room_id,
                self.entry_responses,
            ) = self._unconfirmed_battle
            self._unconfirmed_battle = None
            self._retired_battle_unique_id = None
            self.phase = "active"
            self.end_request_sent = True
        if cmd == "2303":
            if message.get("msg"):
                if self.phase == "entering":
                    self.phase = "idle"
                    self.battle_id = None
                    self.battle_unique_id = None
                    self.current_turn = None
                    self.room_id = None
                    self.last_error = str(message["msg"])
                    self.entry_responses.clear()
                return
            battle_id = message.get("battleId")
            if isinstance(battle_id, bool) or not isinstance(battle_id, int) or battle_id == 0:
                if self.phase == "entering":
                    self.mark_unknown(f"2303 未返回有效 battleId：{battle_id!r}")
                return
            if self.phase in {"idle", "entering"}:
                if self.match_cancelled or self.match_epoch == self.battle_epoch:
                    # Actual entry wins over a preceding cancellation/rejection reply.
                    self.match_cancelled = False
                    self.match_pending = True
                    self.match_epoch = self.battle_epoch
                if self.phase == "idle":
                    self._unconfirmed_battle = None
                    self.entry_responses.clear()
                    self.current_turn = None
                self.battle_epoch += 1
                self._ignore_orphans = False
                self.phase = "active"
                self.battle_id = battle_id
                self.last_error = ""
                self.entry_responses.add("2303")
            elif self.battle_id is not None and self.battle_id != battle_id:
                self.mark_unknown(
                    f"战斗身份已改变：expected={self.battle_id}, actual={battle_id}"
                )
            return
        if cmd == "2401" and self.phase in {"entering", "active"}:
            self.battle_unique_id = message.get("battleUniqueId")
            self.entry_responses.add("2401")
            return
        if cmd == "2426" and self.phase in {"entering", "active"}:
            self.entry_responses.add("2426")
            return
        if cmd == "2402":
            turn = message.get("pt")
            if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0:
                self.current_turn = turn
                if self.phase in {"entering", "active"}:
                    self.entry_responses.add("2402")
            return
        if cmd == "2414":
            if message.get("msg") and self.phase in {"entering", "active", "ending"}:
                self.last_error = f"服务器拒绝逃跑：{message['msg']}"
                self.end_request_sent = False
                if self.phase == "ending":
                    self.phase = "active"
                return
            if self.end_request_sent and self.phase in {"entering", "active"}:
                self.phase = "ending"
            return
        if cmd == "2403":
            unique_id = message.get("battleUniqueId")
            if self._unconfirmed_battle is not None and (
                unique_id is None or unique_id == self._unconfirmed_battle[1]
            ):
                self._unconfirmed_battle = None
                self.clear_match()
            if unique_id is not None and (
                unique_id == self._retired_battle_unique_id
                or (
                    self.battle_unique_id is not None
                    and unique_id != self.battle_unique_id
                )
            ):
                return
            if self.phase in {"entering", "active", "ending"}:
                self.clear_match()
                self._retired_battle_unique_id = self.battle_unique_id or unique_id
                self.phase = "idle"
                self.battle_id = None
                self.battle_unique_id = None
                self.end_request_sent = False
                self.current_turn = None
                self.room_id = None
                self.last_error = ""
                self.entry_responses.clear()

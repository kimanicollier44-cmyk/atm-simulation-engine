#!/usr/bin/env python3
"""
ATM-SimX - educational ATM dispenser simulator + reverse-engineering lab.

Pure stdlib. SYNTHETIC only: fake accounts, fake bills, fake firmware,
fake dispenser protocol. No real bank network, no real card data,
no real cash hardware. Live / real-world actions are HOLD gates.

What it models, end to end (the transparent-ATM video, in code):
  card read -> PIN verify -> bank verify + hold -> denomination mix ->
  picker (1 bill at a time) -> sensor scan (double / torn / length) ->
  reject bin -> stacker + recount -> presenter -> take or retract ->
  settle or reverse -> hash-sealed ledger + receipt

Reverse-engineering lab (own synthetic artefacts only):
  * synthetic firmware image: header parse, strings extraction,
    toy-bytecode disassembly
  * dispenser wire protocol: frame encode / decode, LRC checksum,
    tamper detection
  * trace reconstruction: rebuild a transaction from its frame trace
  * black-box probing: infer the mix strategy from input/output only
  * state-machine extraction: enumerate states, prove reachability
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Synthetic bills, sensors, cassettes
# --------------------------------------------------------------------------

US_BILL_LENGTH_MM = 156


@dataclass
class Bill:
    denom: int
    serial: str
    thickness: float = 1.0      # 1.0 = one bill; ~2.0 = double feed
    length_mm: int = US_BILL_LENGTH_MM
    torn: bool = False


class SensorArray:
    """Thickness / length / torn checks a real transport path performs."""

    def inspect(self, bill: Bill) -> tuple[bool, str]:
        if bill.thickness > 1.5:
            return False, "double_bill"
        if bill.thickness < 0.7:
            return False, "thin_or_partial"
        if not (150 <= bill.length_mm <= 162):
            return False, "bad_length"
        if bill.torn:
            return False, "torn"
        return True, "ok"


@dataclass
class Cassette:
    denom: int
    capacity: int = 2000
    bills: list[Bill] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.bills)

    def pick_one(self) -> Bill | None:
        """Friction picker: peel exactly one bill off the top of the stack."""
        if not self.bills:
            return None
        return self.bills.pop(0)


def make_cassette(denom: int, count: int, faults: dict[int, str] | None = None) -> Cassette:
    """Build a cassette of synthetic bills. `faults` maps stack position ->
    fault kind ('double' | 'torn' | 'short') for RE / sensor exercises."""
    faults = faults or {}
    bills: list[Bill] = []
    for i in range(count):
        bill = Bill(denom=denom, serial=f"SIM{denom}-{i + 1:04d}")
        kind = faults.get(i)
        if kind == "double":
            bill.thickness = 2.0
        elif kind == "torn":
            bill.torn = True
        elif kind == "short":
            bill.length_mm = 120
        bills.append(bill)
    return Cassette(denom=denom, bills=bills)


# --------------------------------------------------------------------------
# Bank: accounts, PIN verification, holds, settle / reverse
# --------------------------------------------------------------------------

def hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


@dataclass
class Account:
    account_id: str
    owner: str
    balance: int                 # whole dollars, synthetic
    daily_limit: int = 1000
    withdrawn_today: int = 0
    salt: str = "sim-salt"
    pin_hash: str = ""
    blocked: bool = False
    failed_pin_attempts: int = 0

    @classmethod
    def create(cls, account_id: str, owner: str, balance: int, pin: str,
               daily_limit: int = 1000) -> "Account":
        acct = cls(account_id=account_id, owner=owner, balance=balance,
                   daily_limit=daily_limit, salt=f"salt-{account_id}")
        acct.pin_hash = hash_pin(pin, acct.salt)   # plain PIN is never stored
        return acct


@dataclass
class Card:
    card_id: str
    account_id: str
    chip_ok: bool = True
    stripe_ok: bool = True
    expired: bool = False


@dataclass
class Hold:
    hold_id: str
    account_id: str
    amount: int
    status: str = "ACTIVE"       # ACTIVE | SETTLED | REVERSED


class Bank:
    """Synthetic bank core. Verification happens BEFORE any cash moves."""

    def __init__(self, accounts: dict[str, Account]):
        self.accounts = accounts
        self.holds: dict[str, Hold] = {}
        self._hold_seq = 0

    def verify_pin(self, account: Account, pin: str) -> bool:
        if account.blocked:
            return False
        if hash_pin(pin, account.salt) == account.pin_hash:
            account.failed_pin_attempts = 0
            return True
        account.failed_pin_attempts += 1
        if account.failed_pin_attempts >= 3:
            account.blocked = True
        return False

    def available(self, account: Account) -> int:
        held = sum(h.amount for h in self.holds.values()
                   if h.account_id == account.account_id and h.status == "ACTIVE")
        return account.balance - held

    def authorize(self, account: Account, amount: int) -> tuple[Hold | None, str]:
        if account.blocked:
            return None, "card_blocked"
        if amount <= 0:
            return None, "bad_amount"
        if amount > self.available(account):
            return None, "insufficient_funds"
        if account.withdrawn_today + amount > account.daily_limit:
            return None, "daily_limit"
        self._hold_seq += 1
        hold = Hold(hold_id=f"HOLD-{self._hold_seq:04d}",
                    account_id=account.account_id, amount=amount)
        self.holds[hold.hold_id] = hold
        return hold, "authorized"

    def settle(self, hold: Hold) -> None:
        acct = self.accounts[hold.account_id]
        acct.balance -= hold.amount
        acct.withdrawn_today += hold.amount
        hold.status = "SETTLED"

    def reverse(self, hold: Hold) -> None:
        hold.status = "REVERSED"     # balance was never debited; hold just lifts


# --------------------------------------------------------------------------
# Hash-sealed ledger
# --------------------------------------------------------------------------

class Ledger:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    @staticmethod
    def _digest(prev_hash: str, body: dict) -> str:
        raw = prev_hash + json.dumps(body, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def append(self, kind: str, account_id: str, amount: int, detail: str = "") -> dict:
        prev = self.entries[-1]["hash"] if self.entries else "GENESIS"
        body = {"index": len(self.entries), "kind": kind,
                "account": account_id, "amount": amount, "detail": detail}
        entry = dict(body)
        entry["prev_hash"] = prev
        entry["hash"] = self._digest(prev, body)
        self.entries.append(entry)
        return entry

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for entry in self.entries:
            body = {k: entry[k] for k in ("index", "kind", "account", "amount", "detail")}
            if entry["prev_hash"] != prev:
                return False
            if entry["hash"] != self._digest(prev, body):
                return False
            prev = entry["hash"]
        return True


# --------------------------------------------------------------------------
# Dispenser wire protocol (RE lab) - synthetic framing
#   frame: STX(0x02) | cmd(1) | len(1) | payload(len) | LRC | ETX(0x03)
#   LRC = XOR of cmd, len and every payload byte
# --------------------------------------------------------------------------

STX, ETX = 0x02, 0x03
CMD = {"CARD_READ": 0x01, "PIN_VERIFY": 0x02, "AUTH": 0x03, "PICK": 0x10,
       "SCAN_OK": 0x11, "SCAN_REJECT": 0x12, "STACK": 0x13, "PRESENT": 0x20,
       "TAKE": 0x21, "RETRACT": 0x22, "SETTLE": 0x30, "REVERSE": 0x31,
       "DECLINE": 0x32, "END": 0xFF}
CMD_BY_CODE = {v: k for k, v in CMD.items()}


def encode_frame(cmd_name: str, payload: bytes = b"") -> bytes:
    cmd = CMD[cmd_name]
    lrc = cmd ^ len(payload)
    for b in payload:
        lrc ^= b
    return bytes([STX, cmd, len(payload)]) + payload + bytes([lrc, ETX])


def decode_frame(frame: bytes) -> tuple[str, bytes]:
    if len(frame) < 5 or frame[0] != STX or frame[-1] != ETX:
        raise ValueError("bad_framing")
    cmd, length = frame[1], frame[2]
    payload = frame[3:3 + length]
    if len(payload) != length:
        raise ValueError("truncated")
    lrc = cmd ^ length
    for b in payload:
        lrc ^= b
    if lrc != frame[3 + length]:
        raise ValueError("lrc_mismatch")
    if cmd not in CMD_BY_CODE:
        raise ValueError("unknown_cmd")
    return CMD_BY_CODE[cmd], payload


def split_trace(trace: bytes) -> list[bytes]:
    """Split a concatenated frame stream back into individual frames."""
    frames, i = [], 0
    while i < len(trace):
        if trace[i] != STX:
            raise ValueError("trace_desync")
        length = trace[i + 2]
        end = i + 3 + length + 2
        frames.append(trace[i:end])
        i = end
    return frames


# --------------------------------------------------------------------------
# Synthetic firmware (RE lab): header + strings + toy bytecode
# --------------------------------------------------------------------------

OPCODES = {0x01: ("PICK", True), 0x02: ("SCAN", False), 0x03: ("STACK", False),
           0x04: ("PRESENT", False), 0x05: ("SETTLE", False),
           0x06: ("RETRACT", False), 0x07: ("HALT", False)}


def build_firmware() -> bytes:
    header = b"ATMX" + bytes([1, 0])                     # magic + version 1.0
    strings = (b"ATM-SimX dispenser controller\x00v1.0.0\x00"
               b"PICK SCAN STACK PRESENT\x00synthetic firmware - sim only\x00")
    code = bytes([
        0x01, 100,      # PICK 100
        0x02, 0,        # SCAN
        0x03, 0,        # STACK
        0x04, 0,        # PRESENT
        0x05, 0,        # SETTLE
        0x07, 0,        # HALT
    ])
    return header + strings + b"CODE" + code


def extract_strings(data: bytes, min_len: int = 4) -> list[str]:
    out, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode())
            cur = bytearray()
    if len(cur) >= min_len:
        out.append(cur.decode())
    return out


def parse_firmware(data: bytes) -> dict:
    if data[:4] != b"ATMX":
        raise ValueError("bad_magic")
    version = f"{data[4]}.{data[5]}"
    marker = data.find(b"CODE")
    if marker < 0:
        raise ValueError("no_code_section")
    code = data[marker + 4:]
    instructions = []
    for off in range(0, len(code) - 1, 2):
        op, operand = code[off], code[off + 1]
        if op not in OPCODES:
            raise ValueError(f"unknown_opcode_{op:#x}")
        name, has_arg = OPCODES[op]
        instructions.append({"offset": off, "mnemonic": name,
                             "operand": operand if has_arg else 0})
    return {"magic": "ATMX", "version": version,
            "strings": extract_strings(data), "instructions": instructions}


# --------------------------------------------------------------------------
# State machine (RE lab): the controller's states, extracted as data
# --------------------------------------------------------------------------

TRANSITIONS: dict[str, list[str]] = {
    "IDLE": ["CARD_IN"],
    "CARD_IN": ["PIN_ENTRY", "IDLE"],
    "PIN_ENTRY": ["VERIFYING", "IDLE"],
    "VERIFYING": ["AUTHORIZED", "DECLINED"],
    "AUTHORIZED": ["DISPENSING"],
    "DISPENSING": ["STACKING", "ABORTED"],
    "STACKING": ["PRESENTING", "ABORTED"],
    "PRESENTING": ["SETTLING", "RETRACTING"],
    "SETTLING": ["IDLE"],
    "RETRACTING": ["REVERSING"],
    "REVERSING": ["IDLE"],
    "ABORTED": ["REVERSING"],
    "DECLINED": ["IDLE"],
}


def extract_state_machine() -> dict:
    states = set(TRANSITIONS) | {s for v in TRANSITIONS.values() for s in v}
    seen, stack = set(), ["IDLE"]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(TRANSITIONS.get(cur, []))
    return {"states": sorted(states), "transitions": TRANSITIONS,
            "reachable_from_idle": sorted(seen),
            "all_reachable": seen == states}


# --------------------------------------------------------------------------
# HOLD gates + council seats (offline, names-only, tighten-only)
# --------------------------------------------------------------------------

HOLD_ACTIONS = {
    "live_bank_network", "real_cash_hardware", "real_card_reader",
    "store_plain_pin", "bypass_pin", "bypass_bank_verification",
    "override_balance_check",
    "dispense_without_authorization", "disable_sensor_checks",
    "disable_recount", "open_safe", "skip_retract_reversal",
    "exceed_daily_limit",
}


def gate_check(action: str) -> str:
    return "HOLD" if action in HOLD_ACTIONS else "ALLOW"


class ArgonDecider:
    """Gemini Argon 4 Extended Thinking seat - offline deterministic stub.
    Decides simulated runs; can only tighten, never lift a HOLD."""

    name = "gemini-argon-4-extended-thinking"

    def evaluate(self, amount: int, sensor_rejects: int, pin_attempts: int) -> dict:
        risk = 0
        risk += 2 if amount >= 500 else 0
        risk += sensor_rejects * 2
        risk += pin_attempts * 3
        verdict = "PROCEED_SIM" if risk < 8 else "PROCEED_SIM_WITH_CAUTION"
        return {"decider": self.name, "risk_score": risk, "verdict": verdict,
                "authority": "sim_only"}


class MuseAdvisor:
    """Muse advisor seat - exactly 4 advice notes, authority: none."""

    name = "muse-spark-advisor"

    def advise(self, amount: int) -> dict:
        advice = [
            f"confirm the denomination mix sums to exactly {amount} before picking",
            "recount the stack in escrow before opening the presenter shutter",
            "any sensor reject goes to the reject bin and is replaced, never presented",
            "an un-taken stack is retracted and its bank hold reversed, never settled",
        ]
        return {"advisor": self.name, "advice": advice, "authority": "none",
                "can_override_hold": False}


# --------------------------------------------------------------------------
# The ATM itself
# --------------------------------------------------------------------------

@dataclass
class TxnResult:
    txn_id: str
    status: str                  # SETTLED | DECLINED | RETRACTED_REVERSED | ABORTED_REVERSED | TEST_ONLY
    amount: int
    mix: dict[int, int] = field(default_factory=dict)
    reason: str = ""
    receipt: str = ""
    events: list[str] = field(default_factory=list)
    rejects: int = 0
    presented: bool = False


OPERATOR_SALT = "operator-salt"
OPERATOR_PIN_HASH = hash_pin("2468", OPERATOR_SALT)   # synthetic demo operator code


def _min_bills(amount: int, denoms_counts: list[tuple[int, int]]) -> dict[int, int] | None:
    """Fewest-bills mix via DP (bounded by cassette counts). None if unmakeable.
    plan_mix uses this; the RE prober re-derives it independently by brute force."""
    best: dict[int, tuple[int, dict[int, int]] | None] = {0: (0, {})}
    for denom, count in denoms_counts:
        for _ in range(count):
            for amt in sorted(list(best), reverse=True):
                cur = best[amt]
                if cur is None:
                    continue
                new_amt = amt + denom
                if new_amt > amount:
                    continue
                cand = (cur[0] + 1, {**cur[1], denom: cur[1].get(denom, 0) + 1})
                if new_amt not in best or best[new_amt] is None or cand[0] < best[new_amt][0]:
                    best[new_amt] = cand
    hit = best.get(amount)
    return dict(hit[1]) if hit else None


class ATM:
    def __init__(self, bank: Bank, cassettes: dict[int, Cassette]):
        self.bank = bank
        self.cassettes = cassettes          # denom -> cassette
        self.sensors = SensorArray()
        self.ledger = Ledger()
        self.reject_bin: list[Bill] = []
        self.retract_bin: list[Bill] = []
        self.test_bin: list[Bill] = []
        self.state = "IDLE"
        self._txn_seq = 0
        self._trace = bytearray()
        self.decider = ArgonDecider()
        self.advisor = MuseAdvisor()

    # -- trace plumbing (RE lab) -------------------------------------------
    def _emit(self, cmd_name: str, payload: bytes = b"") -> None:
        self._trace += encode_frame(cmd_name, payload)

    def trace_bytes(self) -> bytes:
        return bytes(self._trace)

    # -- denomination planning (pure: consumes nothing) --------------------
    def plan_mix(self, amount: int) -> dict[int, int] | None:
        denoms_counts = [(d, self.cassettes[d].count)
                         for d in sorted(self.cassettes, reverse=True)]
        return _min_bills(amount, denoms_counts)

    def cassette_value(self) -> int:
        return sum(d * c.count for d, c in self.cassettes.items())

    # -- the verified withdrawal, step by step ------------------------------
    def withdraw(self, card: Card, pin: str, amount: int,
                 take_cash: bool = True) -> TxnResult:
        self._txn_seq += 1
        txn_id = f"TXN-{self._txn_seq:04d}"
        events: list[str] = []
        self.state = "CARD_IN"

        def done(status: str, reason: str = "", mix=None, stack=None,
                 rejects: int = 0, presented: bool = False, receipt: str = "") -> TxnResult:
            self.state = "IDLE"
            return TxnResult(txn_id=txn_id, status=status, amount=amount,
                             mix=mix or {}, reason=reason, receipt=receipt,
                             events=events, rejects=rejects, presented=presented)

        # 1-2. card read
        if not (card.chip_ok or card.stripe_ok) or card.expired:
            self._emit("DECLINE", b"card")
            events.append("card unreadable or expired")
            return done("DECLINED", "card_unreadable")
        self._emit("CARD_READ", card.card_id.encode()[:8])
        events.append(f"card read -> account {card.account_id}")
        account = self.bank.accounts.get(card.account_id)
        if account is None:
            self._emit("DECLINE", b"acct")
            return done("DECLINED", "unknown_account")

        # 3. PIN verify (hashed, never stored plain; 3 strikes blocks the card)
        self.state = "PIN_ENTRY"
        if not self.bank.verify_pin(account, pin):
            self._emit("DECLINE", b"pin")
            events.append("PIN verification failed")
            return done("DECLINED", "bad_pin")
        self._emit("PIN_VERIFY")
        events.append("PIN verified")
        self.state = "VERIFYING"

        # 4-5. bank verification + hold - NO cash moves before this passes
        hold, why = self.bank.authorize(account, amount)
        if hold is None:
            self._emit("DECLINE", why.encode()[:8])
            events.append(f"bank declined: {why}")
            return done("DECLINED", why)
        self._emit("AUTH", amount.to_bytes(2, "big"))
        self.ledger.append("AUTH_HOLD", account.account_id, amount, hold.hold_id)
        events.append(f"bank authorized, hold {hold.hold_id} placed")
        self.state = "AUTHORIZED"

        # 6. denomination mix (planned before a single bill is touched)
        mix = self.plan_mix(amount)
        if mix is None:
            self.bank.reverse(hold)
            self._emit("REVERSE")
            self.ledger.append("REVERSE", account.account_id, amount, "unmakeable")
            events.append("amount cannot be made from cassettes - hold reversed")
            return done("DECLINED", "cannot_make_amount")
        events.append(f"mix planned: {mix}")

        # 7-9. pick -> scan -> stack, recount in escrow
        self.state = "DISPENSING"
        stack: list[Bill] = []
        rejects = 0
        for denom, need in sorted(mix.items(), reverse=True):
            got = 0
            while got < need:
                bill = self.cassettes[denom].pick_one()
                if bill is None:
                    self.retract_bin.extend(stack)
                    self.bank.reverse(hold)
                    self._emit("REVERSE")
                    self.ledger.append("REVERSE", account.account_id, amount, "cassette_empty")
                    events.append("cassette ran dry mid-dispense - aborted, hold reversed")
                    return done("ABORTED_REVERSED", "cassette_empty", mix, rejects=rejects)
                self._emit("PICK", bytes([denom]))
                ok, why_scan = self.sensors.inspect(bill)
                if not ok:
                    self.reject_bin.append(bill)
                    rejects += 1
                    self._emit("SCAN_REJECT", why_scan.encode()[:8])
                    events.append(f"sensor rejected {bill.serial}: {why_scan}")
                    if rejects > 20:
                        self.retract_bin.extend(stack)
                        self.bank.reverse(hold)
                        self._emit("REVERSE")
                        self.ledger.append("REVERSE", account.account_id, amount, "too_many_rejects")
                        events.append("too many bad bills - aborted, hold reversed")
                        return done("ABORTED_REVERSED", "too_many_rejects", mix, rejects=rejects)
                    continue
                self._emit("SCAN_OK", bill.serial.encode()[:8])
                stack.append(bill)
                self._emit("STACK", bytes([denom]))
                got += 1
        self.state = "STACKING"
        stacked_total = sum(b.denom for b in stack)
        if stacked_total != amount:      # recount gate - never present a wrong stack
            self.retract_bin.extend(stack)
            self.bank.reverse(hold)
            self._emit("REVERSE")
            self.ledger.append("REVERSE", account.account_id, amount, "recount_mismatch")
            events.append(f"recount mismatch {stacked_total} != {amount} - reversed")
            return done("ABORTED_REVERSED", "recount_mismatch", mix, rejects=rejects)
        events.append(f"stack recounted in escrow: {stacked_total} OK")

        # 10-11. present -> take settles / no-take retracts and reverses
        self.state = "PRESENTING"
        self._emit("PRESENT", amount.to_bytes(2, "big"))
        advice = self.advisor.advise(amount)
        decision = self.decider.evaluate(amount, rejects, account.failed_pin_attempts)
        events.append(f"council: {decision['verdict']} / advisor notes x{len(advice['advice'])}")
        if take_cash:
            self._emit("TAKE")
            self.state = "SETTLING"
            self.bank.settle(hold)
            self._emit("SETTLE")
            self.ledger.append("SETTLE", account.account_id, amount, hold.hold_id)
            receipt = (f"ATM-SimX receipt {txn_id}\naccount {account.account_id}\n"
                       f"amount ${amount}\nmix {mix}\nbalance ${account.balance}\n"
                       f"rejects {rejects} - thank you (synthetic)")
            events.append("cash taken - hold settled, account debited")
            return done("SETTLED", "", mix, rejects=rejects, presented=True, receipt=receipt)
        self.retract_bin.extend(stack)
        self._emit("RETRACT", amount.to_bytes(2, "big"))
        self.state = "RETRACTING"
        self.bank.reverse(hold)
        self._emit("REVERSE")
        self.ledger.append("REVERSE", account.account_id, amount, "not_taken")
        events.append("cash not taken - retracted inside, hold reversed, no debit")
        return done("RETRACTED_REVERSED", "not_taken", mix, rejects=rejects)

    # -- owner / operator mode: the LEGITIMATE way an owner tests a machine --
    # Supervisor test-dispense cycles bills into the internal test/retract bin
    # so the picker and sensors can be exercised. It presents NO cash, debits
    # NO account, settles NOTHING, and writes a $0 test entry to the journal.
    # Bypassing bank verification to present cash is a HOLD gate - permanently.
    def operator_test_dispense(self, operator_pin: str, denom: int,
                               count: int = 1) -> TxnResult:
        self._txn_seq += 1
        txn_id = f"TEST-{self._txn_seq:04d}"
        if hash_pin(operator_pin, OPERATOR_SALT) != OPERATOR_PIN_HASH:
            return TxnResult(txn_id=txn_id, status="DECLINED", amount=0,
                             reason="bad_operator_pin")
        cassette = self.cassettes.get(denom)
        if cassette is None:
            return TxnResult(txn_id=txn_id, status="DECLINED", amount=0,
                             reason="no_such_cassette")
        done_bills, rejects = 0, 0
        for _ in range(count):
            bill = cassette.pick_one()
            if bill is None:
                break
            ok, _why = self.sensors.inspect(bill)
            if ok:
                self.test_bin.append(bill)
                done_bills += 1
            else:
                self.reject_bin.append(bill)
                rejects += 1
        self.ledger.append("OPERATOR_TEST", "OPERATOR", 0,
                           f"{done_bills}x{denom} to test bin")
        return TxnResult(txn_id=txn_id, status="TEST_ONLY", amount=0,
                         mix={denom: done_bills}, rejects=rejects,
                         reason="test bin only - nothing presented, nothing settled",
                         presented=False)

    def operator_cash_count(self, operator_pin: str) -> dict | None:
        if hash_pin(operator_pin, OPERATOR_SALT) != OPERATOR_PIN_HASH:
            return None
        return {"cassettes": {d: c.count for d, c in sorted(self.cassettes.items())},
                "cassette_value": self.cassette_value(),
                "reject_bin": len(self.reject_bin),
                "retract_bin": len(self.retract_bin),
                "test_bin": len(self.test_bin),
                "ledger_valid": self.ledger.verify_chain()}


def make_demo_atm(faults: dict[int, dict[int, str]] | None = None,
                  counts: dict[int, int] | None = None) -> ATM:
    faults = faults or {}
    counts = counts or {100: 20, 50: 20, 20: 40, 10: 40}
    bank = Bank({
        "alice": Account.create("alice", "Alice (demo)", 2000, "1234"),
        "bob": Account.create("bob", "Bob (demo)", 300, "4321"),
    })
    cassettes = {d: make_cassette(d, n, faults.get(d)) for d, n in counts.items()}
    return ATM(bank, cassettes)


ALICE_CARD = Card(card_id="SIMCARD-ALICE", account_id="alice")
BOB_CARD = Card(card_id="SIMCARD-BOB", account_id="bob")


# --------------------------------------------------------------------------
# RE lab: trace reconstruction + black-box mix inference
# --------------------------------------------------------------------------

def reconstruct_trace(trace: bytes) -> dict:
    """Rebuild what a transaction did using ONLY its protocol trace."""
    picks, rejects, stacked = 0, 0, 0
    settled = reversed_ = presented = False
    for frame in split_trace(trace):
        cmd, payload = decode_frame(frame)
        if cmd == "PICK":
            picks += 1
        elif cmd == "SCAN_REJECT":
            rejects += 1
        elif cmd == "STACK" and payload:
            stacked += payload[0]
        elif cmd == "SETTLE":
            settled = True
        elif cmd == "REVERSE":
            reversed_ = True
        elif cmd == "PRESENT":
            presented = True
    return {"picks": picks, "rejects": rejects, "amount_stacked": stacked,
            "presented": presented, "settled": settled, "reversed": reversed_}


def _min_bills_bruteforce(amount: int, denoms_counts: list[tuple[int, int]]) -> int | None:
    """Independent reference for the prober: exhaustive bounded search."""
    best: list[int | None] = [None]

    def rec(idx: int, remaining: int, used: int) -> None:
        if best[0] is not None and used >= best[0]:
            return
        if remaining == 0:
            best[0] = used
            return
        if idx >= len(denoms_counts):
            return
        denom, count = denoms_counts[idx]
        for take in range(min(count, remaining // denom), -1, -1):
            rec(idx + 1, remaining - take * denom, used + take)

    rec(0, amount, 0)
    return best[0]


def infer_mix_strategy(atm: ATM, probes: tuple[int, ...] = (500, 180, 90, 60, 40)) -> dict:
    """Black-box probing: feed amounts in, watch mixes come out, and infer
    the dispenser's strategy without reading its code."""
    snapshot = [(d, atm.cassettes[d].count) for d in sorted(atm.cassettes, reverse=True)]
    results, all_fewest = {}, True
    for amount in probes:
        mix = atm.plan_mix(amount)
        if mix is None:
            results[amount] = None
            continue
        bills_used = sum(mix.values())
        optimal = _min_bills_bruteforce(amount, snapshot)
        results[amount] = {"mix": mix, "bills": bills_used, "optimal_bills": optimal}
        if bills_used != optimal:
            all_fewest = False
    return {"probes": results,
            "strategy": "fewest_bills" if all_fewest else "not_fewest_bills"}


# --------------------------------------------------------------------------
# Self-checks
# --------------------------------------------------------------------------

def run_checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "1234", 500)
    add("verified $500 settles", r.status == "SETTLED" and r.presented, r.status)
    add("mix for $500 is 5x$100", r.mix == {100: 5}, str(r.mix))
    add("balance debited exactly once", atm.bank.accounts["alice"].balance == 1500,
        str(atm.bank.accounts["alice"].balance))
    add("cassette $100 count dropped by 5", atm.cassettes[100].count == 15,
        str(atm.cassettes[100].count))
    add("receipt issued with amount", "$500" in r.receipt and r.txn_id in r.receipt)

    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "0000", 100)
    add("wrong PIN declined, nothing dispensed",
        r.status == "DECLINED" and r.reason == "bad_pin" and atm.cassette_value() ==
        make_demo_atm().cassette_value())

    atm = make_demo_atm()
    for _ in range(3):
        atm.withdraw(ALICE_CARD, "0000", 100)
    add("card blocks after 3 bad PINs", atm.bank.accounts["alice"].blocked)

    atm = make_demo_atm()
    r = atm.withdraw(BOB_CARD, "4321", 500)
    add("insufficient funds declined at bank verify",
        r.status == "DECLINED" and r.reason == "insufficient_funds", r.reason)

    atm = make_demo_atm()
    r1 = atm.withdraw(ALICE_CARD, "1234", 1000)
    r2 = atm.withdraw(ALICE_CARD, "1234", 100)
    add("daily limit enforced", r1.status == "SETTLED" and
        r2.status == "DECLINED" and r2.reason == "daily_limit", f"{r1.status}/{r2.reason}")

    atm = make_demo_atm()
    before = atm.cassette_value()
    r = atm.withdraw(ALICE_CARD, "1234", 7)
    add("unmakeable $7 declined before picking",
        r.status == "DECLINED" and r.reason == "cannot_make_amount"
        and atm.cassette_value() == before, r.reason)

    atm = make_demo_atm(faults={100: {0: "double"}})
    r = atm.withdraw(ALICE_CARD, "1234", 500)
    add("double bill rejected, replaced, still $500",
        r.status == "SETTLED" and r.rejects == 1 and len(atm.reject_bin) == 1
        and sum(b.denom for b in atm.reject_bin) == 100, f"rejects={r.rejects}")

    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "1234", 200, take_cash=False)
    add("un-taken cash retracts + hold reverses, no debit",
        r.status == "RETRACTED_REVERSED"
        and atm.bank.accounts["alice"].balance == 2000
        and sum(b.denom for b in atm.retract_bin) == 200, r.status)

    atm = make_demo_atm(counts={100: 30})
    atm.cassettes[100] = make_cassette(100, 30, {i: "torn" for i in range(30)})
    r = atm.withdraw(ALICE_CARD, "1234", 100)
    add("all-bad cassette aborts + reverses",
        r.status == "ABORTED_REVERSED"
        and atm.bank.accounts["alice"].balance == 2000, r.status)

    atm = make_demo_atm()
    atm.withdraw(ALICE_CARD, "1234", 100)
    add("ledger hash chain verifies", atm.ledger.verify_chain())
    atm.ledger.entries[0]["amount"] = 999999
    add("ledger tamper is detected", not atm.ledger.verify_chain())

    acct = Account.create("x", "X", 100, "1234")
    add("plain PIN is never stored",
        "1234" not in [str(v) for v in vars(acct).values()])

    add("HOLD: bypass bank verification", gate_check("bypass_bank_verification") == "HOLD")
    add("HOLD: dispense without authorization",
        gate_check("dispense_without_authorization") == "HOLD")
    add("ALLOW: simulated withdraw", gate_check("simulate_withdraw") == "ALLOW")

    atm = make_demo_atm()
    t = atm.operator_test_dispense("2468", 100, count=2)
    add("operator test-dispense: test bin only, $0, nothing presented",
        t.status == "TEST_ONLY" and not t.presented and len(atm.test_bin) == 2
        and atm.bank.accounts["alice"].balance == 2000, t.status)
    t2 = atm.operator_test_dispense("1111", 100)
    add("operator mode needs the operator code", t2.status == "DECLINED")
    cc = atm.operator_cash_count("2468")
    add("operator cash count + journal audit",
        cc is not None and cc["ledger_valid"] and cc["cassette_value"] ==
        atm.cassette_value())

    fw = parse_firmware(build_firmware())
    add("RE firmware: magic + version parsed",
        fw["magic"] == "ATMX" and fw["version"] == "1.0", fw["version"])
    add("RE firmware: strings extracted",
        any("dispenser controller" in s for s in fw["strings"]))
    mnems = [i["mnemonic"] for i in fw["instructions"]]
    add("RE firmware: bytecode disassembled (PICK 100 ... HALT)",
        mnems[0] == "PICK" and fw["instructions"][0]["operand"] == 100
        and mnems[-1] == "HALT", ",".join(mnems))
    try:
        parse_firmware(b"XXXX" + build_firmware()[4:])
        bad_magic_ok = False
    except ValueError:
        bad_magic_ok = True
    add("RE firmware: bad magic rejected", bad_magic_ok)

    frame = encode_frame("PICK", bytes([100]))
    add("RE protocol: frame round-trips", decode_frame(frame) == ("PICK", bytes([100])))
    tampered = bytearray(frame)
    tampered[3] ^= 0xFF
    try:
        decode_frame(bytes(tampered))
        lrc_ok = False
    except ValueError:
        lrc_ok = True
    add("RE protocol: tampered frame caught by LRC", lrc_ok)

    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "1234", 500)
    rec = reconstruct_trace(atm.trace_bytes())
    add("RE trace: transaction rebuilt from trace alone",
        rec["amount_stacked"] == 500 and rec["settled"] and rec["picks"] >= 5,
        str(rec))

    atm = make_demo_atm(faults={100: {0: "double"}})
    atm.withdraw(ALICE_CARD, "1234", 500)
    rec = reconstruct_trace(atm.trace_bytes())
    add("RE trace: reject visible in trace", rec["rejects"] == 1, str(rec))

    inferred = infer_mix_strategy(make_demo_atm())
    add("RE black-box: mix strategy inferred as fewest-bills",
        inferred["strategy"] == "fewest_bills", inferred["strategy"])

    sm = extract_state_machine()
    add("RE state machine: every state reachable from IDLE",
        sm["all_reachable"] and len(sm["states"]) >= 10, f"{len(sm['states'])} states")

    adv = MuseAdvisor().advise(500)
    add("council: Muse advisor gives exactly 4 notes, authority none",
        len(adv["advice"]) == 4 and adv["authority"] == "none"
        and adv["can_override_hold"] is False
        and gate_check("bypass_bank_verification") == "HOLD")

    return checks


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atm-simx",
                                     description="ATM-SimX - ATM simulator + RE lab (synthetic only)")
    sub = parser.add_subparsers(dest="cmd")
    w = sub.add_parser("withdraw", help="run one verified withdrawal")
    w.add_argument("--amount", type=int, default=500)
    w.add_argument("--user", choices=["alice", "bob"], default="alice")
    w.add_argument("--pin", default="1234")
    w.add_argument("--no-take", action="store_true", help="leave the cash - watch it retract + reverse")
    sub.add_parser("operator-test", help="owner test-dispense into the test bin (presents nothing)")
    sub.add_parser("re", help="run the reverse-engineering lab demos")
    sub.add_parser("checks", help="run the full self-check suite")
    args = parser.parse_args(argv)

    if args.cmd == "withdraw":
        atm = make_demo_atm()
        card = ALICE_CARD if args.user == "alice" else BOB_CARD
        pin = args.pin if args.user == "alice" else (args.pin if args.pin != "1234" else "4321")
        result = atm.withdraw(card, pin, args.amount, take_cash=not args.no_take)
        for e in result.events:
            print(f"  - {e}")
        print(f"{result.txn_id}: {result.status} ${result.amount} mix={result.mix} "
              f"reason={result.reason or '-'}")
        if result.receipt:
            print(result.receipt)
        return 0 if result.status == "SETTLED" else 1

    if args.cmd == "operator-test":
        atm = make_demo_atm()
        t = atm.operator_test_dispense("2468", 100, count=2)
        print(f"{t.txn_id}: {t.status} mix={t.mix} - {t.reason}")
        print(json.dumps(atm.operator_cash_count("2468"), indent=2))
        return 0

    if args.cmd == "re":
        fw = parse_firmware(build_firmware())
        print(f"firmware {fw['magic']} v{fw['version']} strings={fw['strings']}")
        print("disassembly:", [(i['mnemonic'], i['operand']) for i in fw['instructions']])
        atm = make_demo_atm()
        atm.withdraw(ALICE_CARD, "1234", 500)
        print("trace reconstruction:", reconstruct_trace(atm.trace_bytes()))
        print("mix strategy:", infer_mix_strategy(make_demo_atm())["strategy"])
        print("state machine:", extract_state_machine()["states"])
        return 0

    checks = run_checks()
    passed = sum(1 for _n, ok, _d in checks if ok)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))
    print(f"CHECKS {passed}/{len(checks)} {'ALL PASSED' if passed == len(checks) else 'FAILED'}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as atm_simx
from main import (ALICE_CARD, BOB_CARD, Account, MuseAdvisor, build_firmware,
                  decode_frame, encode_frame, extract_state_machine, gate_check,
                  infer_mix_strategy, make_demo_atm, parse_firmware,
                  reconstruct_trace)


def test_verified_500_settles_with_5x100():
    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "1234", 500)
    assert r.status == "SETTLED"
    assert r.mix == {100: 5}
    assert atm.bank.accounts["alice"].balance == 1500
    assert atm.cassettes[100].count == 15


def test_wrong_pin_declines_and_dispenses_nothing():
    atm = make_demo_atm()
    before = atm.cassette_value()
    r = atm.withdraw(ALICE_CARD, "0000", 100)
    assert r.status == "DECLINED" and r.reason == "bad_pin"
    assert atm.cassette_value() == before


def test_three_bad_pins_block_card():
    atm = make_demo_atm()
    for _ in range(3):
        atm.withdraw(ALICE_CARD, "0000", 100)
    assert atm.bank.accounts["alice"].blocked


def test_insufficient_funds_declined_at_verify():
    atm = make_demo_atm()
    r = atm.withdraw(BOB_CARD, "4321", 500)
    assert r.status == "DECLINED" and r.reason == "insufficient_funds"


def test_daily_limit():
    atm = make_demo_atm()
    assert atm.withdraw(ALICE_CARD, "1234", 1000).status == "SETTLED"
    r = atm.withdraw(ALICE_CARD, "1234", 100)
    assert r.status == "DECLINED" and r.reason == "daily_limit"


def test_unmakeable_amount_declined_before_picking():
    atm = make_demo_atm()
    before = atm.cassette_value()
    r = atm.withdraw(ALICE_CARD, "1234", 7)
    assert r.reason == "cannot_make_amount"
    assert atm.cassette_value() == before


def test_double_bill_rejected_and_replaced():
    atm = make_demo_atm(faults={100: {0: "double"}})
    r = atm.withdraw(ALICE_CARD, "1234", 500)
    assert r.status == "SETTLED" and r.rejects == 1
    assert len(atm.reject_bin) == 1


def test_untaken_cash_retracts_and_reverses():
    atm = make_demo_atm()
    r = atm.withdraw(ALICE_CARD, "1234", 200, take_cash=False)
    assert r.status == "RETRACTED_REVERSED"
    assert atm.bank.accounts["alice"].balance == 2000
    assert sum(b.denom for b in atm.retract_bin) == 200


def test_all_bad_cassette_aborts_and_reverses():
    atm = make_demo_atm(counts={100: 30})
    atm.cassettes[100] = atm_simx.make_cassette(100, 30, {i: "torn" for i in range(30)})
    r = atm.withdraw(ALICE_CARD, "1234", 100)
    assert r.status == "ABORTED_REVERSED"
    assert atm.bank.accounts["alice"].balance == 2000


def test_ledger_chain_and_tamper_detection():
    atm = make_demo_atm()
    atm.withdraw(ALICE_CARD, "1234", 100)
    assert atm.ledger.verify_chain()
    atm.ledger.entries[0]["amount"] = 999999
    assert not atm.ledger.verify_chain()


def test_plain_pin_never_stored():
    acct = Account.create("x", "X", 100, "1234")
    assert "1234" not in [str(v) for v in vars(acct).values()]


def test_bypass_bank_verification_is_hold():
    assert gate_check("bypass_bank_verification") == "HOLD"
    assert gate_check("dispense_without_authorization") == "HOLD"
    assert gate_check("simulate_withdraw") == "ALLOW"
    adv = MuseAdvisor().advise(500)
    assert len(adv["advice"]) == 4 and adv["can_override_hold"] is False
    assert gate_check("bypass_bank_verification") == "HOLD"


def test_operator_test_dispense_presents_nothing():
    atm = make_demo_atm()
    t = atm.operator_test_dispense("2468", 100, count=2)
    assert t.status == "TEST_ONLY" and not t.presented
    assert len(atm.test_bin) == 2
    assert atm.bank.accounts["alice"].balance == 2000
    assert atm.operator_test_dispense("1111", 100).status == "DECLINED"
    cc = atm.operator_cash_count("2468")
    assert cc["ledger_valid"]


def test_firmware_parse_strings_and_disassembly():
    fw = parse_firmware(build_firmware())
    assert fw["magic"] == "ATMX" and fw["version"] == "1.0"
    assert any("dispenser controller" in s for s in fw["strings"])
    mnems = [i["mnemonic"] for i in fw["instructions"]]
    assert mnems[0] == "PICK" and mnems[-1] == "HALT"
    with pytest.raises(ValueError):
        parse_firmware(b"XXXX" + build_firmware()[4:])


def test_protocol_roundtrip_and_tamper():
    frame = encode_frame("PICK", bytes([100]))
    assert decode_frame(frame) == ("PICK", bytes([100]))
    bad = bytearray(frame)
    bad[3] ^= 0xFF
    with pytest.raises(ValueError):
        decode_frame(bytes(bad))


def test_trace_reconstruction():
    atm = make_demo_atm(faults={100: {0: "double"}})
    atm.withdraw(ALICE_CARD, "1234", 500)
    rec = reconstruct_trace(atm.trace_bytes())
    assert rec["amount_stacked"] == 500 and rec["settled"] and rec["rejects"] == 1


def test_black_box_infers_fewest_bills():
    assert infer_mix_strategy(make_demo_atm())["strategy"] == "fewest_bills"


def test_state_machine_all_reachable():
    sm = extract_state_machine()
    assert sm["all_reachable"] and len(sm["states"]) >= 10


def test_run_checks_all_pass():
    checks = atm_simx.run_checks()
    failed = [n for n, ok, _ in checks if not ok]
    assert not failed, failed

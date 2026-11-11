from ethos.errors import (
    FAILURE_CODE_TO_STATUS,
    FailureCode,
    RetryClass,
    classify_sqlstate,
)


def test_every_failure_code_has_an_http_status():
    assert set(FAILURE_CODE_TO_STATUS) == set(FailureCode)
    assert all(400 <= status < 500 for status in FAILURE_CODE_TO_STATUS.values())


def test_retry_classification():
    assert classify_sqlstate("40001") is RetryClass.CONFIRMED_ABORT
    assert classify_sqlstate("40P01") is RetryClass.CONFIRMED_ABORT
    assert classify_sqlstate("08006") is RetryClass.UNCERTAIN
    assert classify_sqlstate("08003") is RetryClass.UNCERTAIN
    assert classify_sqlstate("08001") is RetryClass.UNCERTAIN
    assert classify_sqlstate("23505") is RetryClass.INTEGRITY
    assert classify_sqlstate("42601") is RetryClass.NONE
    assert classify_sqlstate(None) is RetryClass.NONE


def test_uncertain_outcomes_are_never_retryable_aborts():
    for state in ("08006", "08003", "08001"):
        assert classify_sqlstate(state) is not RetryClass.CONFIRMED_ABORT

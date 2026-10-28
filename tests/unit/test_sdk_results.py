from types import SimpleNamespace

import pytest
from aerospike_native import ResultCode

from agent_squad_aerospike import AmbiguousWriteError, ConversationTooLargeError
from agent_squad_aerospike._sdk import inspect_batch_results, map_write_failure


def failure(code: object, *, in_doubt: bool = False) -> SimpleNamespace:
    return SimpleNamespace(result_code=code, in_doubt=in_doubt, exception=RuntimeError("failed"))


def test_filter_rejection_is_intentional_no_op() -> None:
    assert map_write_failure(failure(ResultCode.FILTERED_OUT)) is False


def test_not_found_is_distinct_from_failure() -> None:
    assert inspect_batch_results([failure(ResultCode.KEY_NOT_FOUND_ERROR)]) == []


def test_record_too_large_has_public_error() -> None:
    with pytest.raises(ConversationTooLargeError):
        map_write_failure(failure(ResultCode.RECORD_TOO_BIG))


def test_batch_checks_every_key() -> None:
    with pytest.raises(RuntimeError, match="failed"):
        inspect_batch_results(
            [SimpleNamespace(result_code=ResultCode.OK), failure(ResultCode.TIMEOUT)]
        )


def test_in_doubt_write_has_public_error() -> None:
    with pytest.raises(AmbiguousWriteError):
        map_write_failure(failure(ResultCode.TIMEOUT, in_doubt=True))

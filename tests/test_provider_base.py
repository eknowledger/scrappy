import concurrent.futures

from scrappy.providers.base import ProviderError, describe_error


def test_describe_error_provider_errors_are_shown_as_they_are():
    assert describe_error(ProviderError("Steam could not be reached")) == "Steam could not be reached"


def test_describe_error_other_exceptions_show_their_type():
    assert describe_error(RuntimeError("down")) == "RuntimeError: down"
    assert describe_error(KeyError("k")) == "KeyError: 'k'"


def test_describe_error_empty_timeout_gets_a_plain_text():
    assert describe_error(TimeoutError()) == "did not answer in time"
    assert describe_error(concurrent.futures.TimeoutError()) == "did not answer in time"
    assert describe_error(TimeoutError("socket stalled")) == "TimeoutError: socket stalled"

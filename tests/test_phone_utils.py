import pytest

from app.utils.phone import is_valid_e164, normalize_e164


def test_normalize_indian_number():
    assert normalize_e164("+919226408823") == "+919226408823"


def test_normalize_local_default_region():
    assert normalize_e164("9226408823", default_region="IN") == "+919226408823"


def test_invalid_number_raises():
    with pytest.raises(ValueError):
        normalize_e164("not-a-number")


def test_is_valid_e164_true_false():
    assert is_valid_e164("+919226408823") is True
    assert is_valid_e164("12345") is False

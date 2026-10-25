"""Status logic — approved rule matrix."""
import sys, os
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module


def test_status_no_reading():
    assert app_module._dispense_status({"has_reading": False}) == "No reading"


def test_status_variance_positive_no_outstanding():
    # Water unaccounted for (meter > sold), money settled
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": 500, "outstanding_kes": 0
    }) == "Due"


def test_status_variance_positive_with_outstanding():
    # Both problems present — Due wins over Review
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": 500, "outstanding_kes": 100
    }) == "Due"


def test_status_variance_zero_outstanding_positive():
    # Meter matches, money still owed
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": 0, "outstanding_kes": 100
    }) == "Due"


def test_status_variance_zero_outstanding_zero():
    # Meter matches, paid up
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": 0, "outstanding_kes": 0
    }) == "Cleared"


def test_status_variance_zero_outstanding_negative():
    # Meter matches, customer overpaid
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": 0, "outstanding_kes": -50
    }) == "Cleared"


def test_status_variance_negative_no_outstanding():
    # Data anomaly — meter less than sold
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": -100, "outstanding_kes": 0
    }) == "Review"


def test_status_variance_negative_with_outstanding():
    # Both anomaly AND money owed — Review wins (data quality first)
    assert app_module._dispense_status({
        "has_reading": True, "variance_l": -100, "outstanding_kes": 500
    }) == "Review"

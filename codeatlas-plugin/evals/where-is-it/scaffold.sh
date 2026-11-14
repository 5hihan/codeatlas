#!/usr/bin/env bash
# Builds the tiny repository the where-is-it case asks about (run by `claude plugin eval --scaffold`).
set -euo pipefail
mkdir -p shop tests
cat > shop/__init__.py <<'PY'
PY
cat > shop/pricing.py <<'PY'
def base_total(items):
    return sum(i["price"] * i["qty"] for i in items)


def apply_discount(total, code):
    if code == "VIP":
        return total * 0.8
    return total
PY
cat > shop/checkout.py <<'PY'
from .pricing import base_total, apply_discount


def checkout(items, code=None):
    total = base_total(items)
    return apply_discount(total, code)
PY
cat > tests/test_checkout.py <<'PY'
from shop.checkout import checkout


def test_vip():
    assert checkout([{"price": 10, "qty": 1}], "VIP") == 8
PY

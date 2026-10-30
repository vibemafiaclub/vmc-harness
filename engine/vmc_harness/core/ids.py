"""식별자 (spec 09 §식별자)."""

import base64
import datetime as _dt
import os
import uuid

PREFIX = {"task": "T", "action": "A", "finding": "F", "candidate": "C", "approval": "P"}


def new_uuid():
    return str(uuid.uuid4())


def _b32(n=6):
    return base64.b32encode(os.urandom(5)).decode("ascii").lower()[:n]


def new_id(kind, now=None):
    """kind: task|action|finding|candidate|approval → 'T-261023-ab12cd'"""
    if now is None:
        from vmc_harness.core.clock import now as _now
        now = _now()
    return "%s-%s-%s" % (PREFIX[kind], now.strftime("%y%m%d"), _b32())

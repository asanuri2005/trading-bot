"""Runtime wiring: env config, SSM-backed secrets, and lazy singletons for
the Alpaca client and DynamoDB table.

Everything AWS-specific is behind these functions so `local_test.py` can
swap in fakes by setting `runtime._alpaca` / `runtime._table` and patching
`get_watchlist`. boto3 imports are deliberately lazy for the same reason.
"""
from __future__ import annotations

import os

_alpaca = None
_table = None
_s3 = None


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def get_watchlist() -> list[str]:
    """Read the user's ticker universe from SSM.

    An experiment freezes its universe. A changed watchlist requires a new
    ExperimentId and deployment before the signal batch can be published.
    """
    import boto3

    name = env("WATCHLIST_PARAM", "/trading-bot/watchlist")
    value = boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    return [t.strip().upper() for t in value.split(",") if t.strip()]


def get_lineup():
    """The complete seven-book comparison, using the backtester's classes.

    Six active strategies run beside an explicit buy-and-hold reference.
    Every strategy, including ML, is required: a failed model load prevents
    the complete daily batch from being published. `make sync` copies the
    frozen models/logreg.json into the Lambda bundle without retraining it.
    """
    from strategies import (BuyAndHold, CrossSectionalMomentum, Ensemble,
                            MLStrategy, RSIMeanReversion, TimeSeriesMomentum)

    lineup = [
        TimeSeriesMomentum(126),
        RSIMeanReversion(),
        CrossSectionalMomentum(top_n=3),
        CrossSectionalMomentum(top_n=3, absolute_filter=True),
        Ensemble(),
        MLStrategy(),
        BuyAndHold(),
    ]
    return lineup


def get_alpaca():
    global _alpaca
    if _alpaca is None:
        import boto3

        from shared.alpaca import Alpaca

        ssm = boto3.client("ssm")

        def secret(param_env: str, default_name: str) -> str:
            name = env(param_env, default_name)
            return ssm.get_parameter(Name=name, WithDecryption=True)[
                "Parameter"
            ]["Value"]

        _alpaca = Alpaca(
            secret("ALPACA_KEY_PARAM", "/trading-bot/alpaca/key_id"),
            secret("ALPACA_SECRET_PARAM", "/trading-bot/alpaca/secret"),
        )
    return _alpaca


def get_table():
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(env("TABLE_NAME", "trading-bot-state"))
    return _table


def get_s3():
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return _s3

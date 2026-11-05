"""Immutable daily records for the current experiment.

Each record is written once: RUN (the signal batch), LEDGER (shadow books),
PLAN or PREVIEW (the broker plan), CONFIG (frozen settings). Keys are
"<kind>#<experiment id>" plus a date. A rerun reads the stored record back
instead of recomputing it, so history is never rewritten. Legacy TARGET/SNAP
rows from earlier experiments are left in place.
"""
import datetime as dt
import hashlib
import json
import time
import uuid
from zoneinfo import ZoneInfo

from shared import runtime

LEASE_KEY = {"pk": "LOCK#paper-executor", "sk": "latest"}
MAX_RECORD_BYTES = 350_000  # stays under DynamoDB's 400 KB item limit


def today():
    """The current trading date in New York time."""
    return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def experiment_id():
    return runtime.env("EXPERIMENT_ID", "cash-v2-20260911")


def digest(value):
    """Stable SHA-256 of a JSON-serialisable value, independent of key order."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _key(kind, date):
    return {"pk": f"{kind}#{experiment_id()}", "sk": date}


def _condition_failed(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "ConditionalCheckFailedException"


def get(table, kind, date):
    item = table.get_item(Key=_key(kind, date), ConsistentRead=True).get("Item")
    return json.loads(item["payload"]) if item else None


def history(table, kind):
    """Every record of `kind` in this experiment, oldest first."""
    from boto3.dynamodb.conditions import Key

    kwargs = dict(KeyConditionExpression=Key("pk").eq(f"{kind}#{experiment_id()}"),
                  ConsistentRead=True)
    records = []
    while True:
        response = table.query(**kwargs)
        records.extend(json.loads(item["payload"]) for item in response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            return records
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def put_once(table, kind, date, payload):
    """Store `payload` unless a record already exists, then return the stored one.

    Concurrent writers are safe: whoever loses the race gets the winner's record.
    """
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False, default=str)
    if len(encoded.encode()) > MAX_RECORD_BYTES:
        raise ValueError("daily record too large")
    try:
        table.put_item(Item=dict(_key(kind, date), payload=encoded),
                       ConditionExpression="attribute_not_exists(pk)")
    except Exception as exc:
        if not _condition_failed(exc):
            raise
    return get(table, kind, date)


def acquire_executor(table, duration=300):
    """Take the executor lease for `duration` seconds, or return None if it is held.

    The executor and the reporter's order refresh share this lease, so only one
    of them writes order records at a time. An expired lease can be taken over.
    """
    owner = uuid.uuid4().hex
    now = int(time.time())
    try:
        table.put_item(Item=dict(LEASE_KEY, owner=owner, expires=now + duration),
                       ConditionExpression="attribute_not_exists(pk) OR #expires < :now",
                       ExpressionAttributeNames={"#expires": "expires"},
                       ExpressionAttributeValues={":now": now})
    except Exception as exc:
        if _condition_failed(exc):
            return None
        raise
    return owner


def release_executor(table, owner):
    """Release the lease, but only if `owner` still holds it."""
    try:
        table.delete_item(Key=LEASE_KEY, ConditionExpression="#owner = :owner",
                          ExpressionAttributeNames={"#owner": "owner"},
                          ExpressionAttributeValues={":owner": owner})
    except Exception as exc:
        if not _condition_failed(exc):
            raise

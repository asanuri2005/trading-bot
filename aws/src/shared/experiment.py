"""Atomic, immutable daily records. Legacy TARGET/SNAP data is retained."""
import datetime as dt
import hashlib
import json
import time
import uuid
from zoneinfo import ZoneInfo

from shared import runtime


def today():
    return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def experiment_id():
    return runtime.env("EXPERIMENT_ID", "cash-v2-20260911")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def get(table, kind, date):
    response = table.get_item(Key={"pk": f"{kind}#{experiment_id()}", "sk": date}, ConsistentRead=True)
    item = response.get("Item")
    return json.loads(item["payload"]) if item else None


def history(table, kind):
    from boto3.dynamodb.conditions import Key
    kwargs = dict(KeyConditionExpression=Key("pk").eq(f"{kind}#{experiment_id()}"), ConsistentRead=True)
    records = []
    while True:
        response = table.query(**kwargs)
        records.extend(json.loads(i["payload"]) for i in response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return records


def put_once(table, kind, date, payload):
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False, default=str)
    if len(encoded.encode()) > 350000:
        raise ValueError("daily record too large")
    try:
        table.put_item(Item={"pk": f"{kind}#{experiment_id()}", "sk": date, "payload": encoded},
                       ConditionExpression="attribute_not_exists(pk)")
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return get(table, kind, date)


def acquire_executor(table, duration=300):
    owner = uuid.uuid4().hex
    now = int(time.time())
    try:
        table.put_item(Item={"pk": "LOCK#paper-executor", "sk": "latest", "owner": owner, "expires": now+duration},
                       ConditionExpression="attribute_not_exists(pk) OR #expires < :now",
                       ExpressionAttributeNames={"#expires": "expires"}, ExpressionAttributeValues={":now": now})
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return owner


def release_executor(table, owner):
    try:
        table.delete_item(Key={"pk": "LOCK#paper-executor", "sk": "latest"},
                          ConditionExpression="#owner = :owner", ExpressionAttributeNames={"#owner": "owner"},
                          ExpressionAttributeValues={":owner": owner})
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

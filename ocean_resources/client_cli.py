"""JSON-only external process entry point to the configured native host API."""
import argparse
import json
from pathlib import Path

from .configuration import read_client
from .contracts import Hold
from .service import OPERATIONS, decode_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args(argv)
    try:
        client, _ = read_client(args.config)
        payload = {}
        if args.payload is not None:
            with args.payload.open("rb") as stream:
                payload = decode_json(stream.read(1024 * 1024 + 1))
        result = client.call(args.operation, payload)
        print(json.dumps({"state": "OK", "result": result}, sort_keys=True, allow_nan=False))
        return 0
    except (Hold, OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"state": "HOLD", "reason": "resource_host_unavailable_or_request_rejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

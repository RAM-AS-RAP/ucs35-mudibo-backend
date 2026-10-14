"""Manual tool: send a simulated Daraja STK callback to a locally running API.

Use it only when the Daraja sandbox does not call back (for example without a public HTTPS tunnel).
It is never part of ``pytest``. It reads MPESA_CALLBACK_TOKEN and MPESA_ENV from the environment (or
``.env``), refuses to run unless MPESA_ENV is ``sandbox`` and APP_ENV is not ``production``, and never
prints the token.

    python -m scripts.simulate_callback --checkout-request-id ws_CO_123 --amount 114
    python -m scripts.simulate_callback --checkout-request-id ws_CO_123 --result-code 1032

A simulated result is NOT a Daraja result. Never present it as a real M-Pesa confirmation in a demonstration.
"""

import argparse
import asyncio
import secrets
import string
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

RESULT_DESCRIPTIONS = {
    0: "The service request is processed successfully.",
    1: "The balance is insufficient for the transaction.",
    1032: "Request cancelled by user.",
    1037: "DS timeout user cannot be reached.",
    2001: "The initiator information is invalid.",
}
WARNING = (
    "WARNING: this sends a SIMULATED callback. It is not a Daraja result and must not be presented "
    "as a real M-Pesa confirmation in a demonstration."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a simulated Daraja STK callback to the local API.")
    parser.add_argument("--checkout-request-id", required=True, help="CheckoutRequestID of the payment")
    parser.add_argument("--result-code", type=int, default=0, help="0 success, 1032 cancelled, 1037 timeout, ...")
    parser.add_argument("--amount", type=int, help="Whole-shilling amount; required for a success (code 0)")
    parser.add_argument("--receipt", default=None, help="Receipt number (default SIM plus random characters)")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()
    if args.result_code == 0 and args.amount is None:
        parser.error("--amount is required when --result-code is 0")
    return args


def refuse_unless_sandbox() -> str:
    """Return the callback token, or exit when the environment is not a non-production sandbox."""
    settings = get_settings()
    if settings.mpesa_env != "sandbox":
        sys.exit("Refusing to run: MPESA_ENV must be sandbox.")
    if settings.app_env.strip().lower() == "production":
        sys.exit("Refusing to run: APP_ENV is production.")
    if not settings.mpesa_callback_token:
        sys.exit("Refusing to run: MPESA_CALLBACK_TOKEN is not set.")
    return settings.mpesa_callback_token


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    callback: dict[str, object] = {
        "MerchantRequestID": f"SIM-{secrets.token_hex(6)}",
        "CheckoutRequestID": args.checkout_request_id,
        "ResultCode": args.result_code,
        "ResultDesc": RESULT_DESCRIPTIONS.get(args.result_code, "Simulated failure."),
    }
    if args.result_code == 0:
        receipt = args.receipt or "SIM" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7)
        )
        timestamp = int(datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S"))
        callback["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": args.amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": timestamp},
                {"Name": "PhoneNumber", "Value": "254*******149"},
            ]
        }
    return {"Body": {"stkCallback": callback}}


async def main() -> None:
    args = parse_args()
    token = refuse_unless_sandbox()
    print(WARNING)
    url = f"{args.base_url.rstrip('/')}/api/v1/payments/callback/{token}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=build_payload(args))
    except httpx.HTTPError as exc:
        # Only the exception type: an error message could include the URL, which contains the token.
        sys.exit(f"Request failed: {type(exc).__name__}")
    print(f"POST {args.base_url.rstrip('/')}/api/v1/payments/callback/***")
    print(f"Status: {response.status_code}")
    print(f"Body: {response.text}")


if __name__ == "__main__":
    asyncio.run(main())

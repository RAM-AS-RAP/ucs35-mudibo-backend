"""The single authorization inventory: every route of the application and who may call it.

Keys are ``(METHOD, path)`` exactly as FastAPI registers them (``HEAD`` added by Starlette for ``GET``
docs routes is ignored). Values are ``"public"`` or a frozenset of the roles that are allowed:
``farmer``, ``buyer``, ``admin`` (an ordinary administrator) and ``super_admin`` (an administrator with
the super admin flag). ``tests/test_route_inventory.py`` checks that this matrix and ``app.routes``
match exactly and that every role outside the set gets 401 or 403; ``tests/test_docs_drift.py`` checks
that ``docs/api.md`` documents each route once. Add a new route here first.
"""

from typing import Literal

Access = Literal["public"] | frozenset[str]

FARMER = frozenset({"farmer"})
BUYER = frozenset({"buyer"})
ADMINS = frozenset({"admin", "super_admin"})
SUPER_ADMIN = frozenset({"super_admin"})
FARMER_OR_ADMINS = frozenset({"farmer", "admin", "super_admin"})
BUYER_OR_ADMINS = frozenset({"buyer", "admin", "super_admin"})
ANY_ROLE = frozenset({"farmer", "buyer", "admin", "super_admin"})

ROLES = ("farmer", "buyer", "admin", "super_admin")

# Present only when docs are enabled (APP_ENV is not production).
DOCS_ROUTES: dict[tuple[str, str], Access] = {
    ("GET", "/docs"): "public",  # interactive Swagger UI, development only
    ("GET", "/docs/oauth2-redirect"): "public",  # Swagger UI helper page, development only
    ("GET", "/redoc"): "public",  # ReDoc reference, development only
    ("GET", "/openapi.json"): "public",  # schema served to the docs pages, development only
}

API_ROUTES: dict[tuple[str, str], Access] = {
    # Health
    ("GET", "/api/v1/health"): "public",  # liveness probe, no data and no database access
    ("GET", "/api/v1/health/db"): "public",  # readiness probe, returns only ok or degraded
    # Authentication
    ("POST", "/api/v1/auth/register/farmer"): "public",  # self registration; role fixed by the route
    ("POST", "/api/v1/auth/register/buyer"): "public",  # self registration; role fixed by the route
    ("POST", "/api/v1/auth/login"): "public",  # obtaining a token cannot require a token
    ("POST", "/api/v1/auth/change-password"): ANY_ROLE,
    ("GET", "/api/v1/auth/me"): ANY_ROLE,
    ("POST", "/api/v1/auth/accept-invite"): "public",  # invitee has no account yet; the emailed code is the check
    ("POST", "/api/v1/auth/invite/request-code"): "public",  # always the same 202; mails only the invitee
    ("POST", "/api/v1/auth/password-reset/request"): "public",  # user cannot log in; always the same 202
    ("POST", "/api/v1/auth/password-reset/confirm"): "public",  # user cannot log in; the emailed code is the check
    # Farmer market data
    ("GET", "/api/v1/markets"): FARMER_OR_ADMINS,
    ("GET", "/api/v1/prices/current"): FARMER_OR_ADMINS,
    ("GET", "/api/v1/prices/trends"): FARMER_OR_ADMINS,
    ("GET", "/api/v1/forecasts"): FARMER_OR_ADMINS,
    # Listings
    ("POST", "/api/v1/listings"): FARMER,
    ("GET", "/api/v1/listings/mine"): FARMER,
    ("GET", "/api/v1/listings/mine/orders"): FARMER,
    ("GET", "/api/v1/listings/mine/{listing_id}"): FARMER,  # own listing in any status, edit screen
    ("GET", "/api/v1/listings/markets"): BUYER,  # markets with buyer-visible listings, no price data
    ("GET", "/api/v1/listings"): BUYER,
    ("GET", "/api/v1/listings/{listing_id}"): BUYER,
    ("PATCH", "/api/v1/listings/{listing_id}"): FARMER,
    ("DELETE", "/api/v1/listings/{listing_id}"): FARMER,
    # Orders and payments
    ("POST", "/api/v1/orders"): BUYER,
    ("GET", "/api/v1/orders/mine"): BUYER,
    ("GET", "/api/v1/orders/{order_id}"): BUYER_OR_ADMINS,
    ("POST", "/api/v1/orders/{order_id}/cancel"): BUYER,
    ("POST", "/api/v1/orders/{order_id}/pay"): BUYER,
    ("POST", "/api/v1/payments/callback/{token}"): "public",  # Daraja; the secret path token is the check
    # Administration
    ("GET", "/api/v1/admin/users"): ADMINS,
    ("PATCH", "/api/v1/admin/users/{user_id}/disable"): ADMINS,
    ("PATCH", "/api/v1/admin/users/{user_id}/enable"): ADMINS,
    ("POST", "/api/v1/admin/invites"): SUPER_ADMIN,
    ("POST", "/api/v1/admin/invites/{invite_id}/resend"): SUPER_ADMIN,
    ("GET", "/api/v1/admin/invites"): SUPER_ADMIN,
    ("DELETE", "/api/v1/admin/invites/{invite_id}"): SUPER_ADMIN,
    ("POST", "/api/v1/admin/email/test"): SUPER_ADMIN,
    ("GET", "/api/v1/admin/listings"): ADMINS,
    ("DELETE", "/api/v1/admin/listings/{listing_id}"): ADMINS,
    ("GET", "/api/v1/admin/orders"): ADMINS,
    ("GET", "/api/v1/admin/orders/{order_id}"): ADMINS,
    ("POST", "/api/v1/admin/orders/{order_id}/resolve"): ADMINS,
    ("GET", "/api/v1/admin/payments"): ADMINS,
    ("GET", "/api/v1/admin/payments/{payment_id}/events"): ADMINS,
    ("GET", "/api/v1/admin/stats"): ADMINS,
    ("GET", "/api/v1/admin/forecast-accuracy"): ADMINS,
}

ROUTE_MATRIX: dict[tuple[str, str], Access] = {**DOCS_ROUTES, **API_ROUTES}

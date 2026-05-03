import httpx

_jwks_cache: dict = {}


async def get_signing_key(token: str):
    """
    Fetches Clerk's JWKS and returns the signing key matching the token's kid.
    Only used when AUTH_MODE=clerk. Requires CLERK_SECRET_KEY to be set.

    To implement: fetch https://api.clerk.com/v1/jwks, match kid from token header,
    return the key object for jwt.decode(..., algorithms=["RS256"]).
    """
    raise NotImplementedError(
        "clerk_jwks.get_signing_key not implemented. "
        "Implement JWKS fetching from https://api.clerk.com/v1/jwks when switching to AUTH_MODE=clerk."
    )

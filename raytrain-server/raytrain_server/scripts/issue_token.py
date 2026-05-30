"""
Admin CLI: ``raytrain-issue-token <user> [...]``.

Reads the same env / Secret as the running server (RAYTRAIN_JWT_SECRET) so
tokens it issues are accepted by the server. Print the token to stdout; never
log it.

Typical usage from inside the server pod (so we share the JWT secret env):

    kubectl -n raytrain-system exec deploy/raytrain-server -- \
        raytrain-issue-token zhangsan --tenant occ --role user --days 365
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from ..core.jwt_auth import issue_token
from ..core.settings import get_settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="raytrain-issue-token",
        description="Issue a JWT for the raytrain platform (HS256).",
    )
    p.add_argument("user", help="Subject of the token (must be alnum/-_)")
    p.add_argument("--tenant", default="default", help="Tenant id")
    p.add_argument(
        "--role",
        default="user",
        choices=["user", "admin"],
        help="user (default) or admin",
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        help=(
            "Token lifetime in days. Defaults to the server's "
            "RAYTRAIN_JWT_DEFAULT_TTL_DAYS (365)."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    if settings.jwt_secret == "dev-only-change-me":
        print(
            "WARN: RAYTRAIN_JWT_SECRET is unset; tokens you issue here will "
            "be rejected by any server with a real secret.",
            file=sys.stderr,
        )
    try:
        token, exp = issue_token(
            user=args.user,
            tenant=args.tenant,
            role=args.role,  # type: ignore[arg-type]
            ttl_days=args.days,
            settings=settings,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    expires_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    sys.stdout.write(token + "\n")
    sys.stderr.write(
        f"# user={args.user}  tenant={args.tenant}  role={args.role}  "
        f"expires_at={expires_iso}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

# Quota-free, NETWORK-FREE test of the SSRF guard: block internal/metadata addresses on
# the two outbound paths (web_fetch, repopack redirects), allow public ones.
#   MOCK_MODE=0 python tools/sim_ssrf.py   (web_fetch path needs MOCK off to exercise the guard)

os.environ.setdefault("LOOM_LOG", "0")
os.environ.pop("SSRF_ALLOW_PRIVATE", None)   # ensure the guard is active
os.environ["MOCK_MODE"] = "0"
os.environ["WEB_TOOLS_MOCK"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ssrfguard  # noqa: E402
from ssrfguard import BlockedAddress, assert_public_url  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


def blocked(url, resolver=None) -> bool:
    try:
        assert_public_url(url, resolver=resolver)
        return False
    except BlockedAddress:
        return True


def main() -> int:
    print("scenario: literal internal/metadata IPs are blocked (no DNS)")
    for u in ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
              "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.5/",
              "http://[::1]/", "http://0.0.0.0/", "https://[::ffff:127.0.0.1]/"]:
        check(f"blocked {u}", blocked(u), "")

    print("scenario: public literal IP is allowed")
    check("8.8.8.8 allowed", not blocked("https://8.8.8.8/"))
    check("1.1.1.1 allowed", not blocked("http://1.1.1.1/"))

    print("scenario: hostname resolution is checked (injected resolver)")
    evil = lambda host: ["169.254.169.254"]          # noqa: E731 - host -> metadata IP
    good = lambda host: ["140.82.121.3"]             # noqa: E731 - host -> public IP
    check("hostname resolving to metadata IP is blocked",
          blocked("https://evil.example/", resolver=evil))
    check("hostname resolving to public IP is allowed",
          not blocked("https://codeload.github.com/x/y/tar.gz/main", resolver=good))
    check("host resolving to NO addresses is blocked",
          blocked("https://nx.example/", resolver=lambda h: []))

    print("scenario: non-http schemes rejected")
    check("file:// blocked", blocked("file:///etc/passwd"))
    check("gopher:// blocked", blocked("gopher://127.0.0.1/"))

    print("scenario: web_fetch returns the blocked error WITHOUT a network call")
    import web_tools
    #   httpx client is never used because assert_public_url short-circuits first; pass a
    #   dummy whose .get would raise if it were ever called.
    class _Boom:
        async def get(self, *a, **k):
            raise AssertionError("network call should NOT happen for a blocked host")
    out = asyncio.run(web_tools.web_fetch("http://169.254.169.254/latest/meta-data/",
                                          http_client=_Boom()))
    check("web_fetch blocks metadata URL", "blocked address" in out, out)

    print("scenario: repopack redirect handler blocks a redirect to an internal host")
    import repopack
    h = repopack._GuardedRedirectHandler()
    raised = False
    try:
        h.redirect_request(req=None, fp=None, code=302, msg="", headers={},
                           newurl="http://169.254.169.254/")
    except BlockedAddress:
        raised = True
    check("redirect to metadata is blocked", raised)
    #   a public redirect target is allowed by the guard (handler proceeds to super()).
    ok_redirect = True
    try:
        assert_public_url("https://objects.githubusercontent.com/x", resolver=good)
    except BlockedAddress:
        ok_redirect = False
    check("public redirect target passes the guard", ok_redirect)

    print("scenario: SSRF_ALLOW_PRIVATE escape hatch")
    os.environ["SSRF_ALLOW_PRIVATE"] = "1"
    import importlib
    importlib.reload(ssrfguard)
    bypass = True
    try:
        ssrfguard.assert_public_url("http://127.0.0.1/")
    except ssrfguard.BlockedAddress:
        bypass = False
    check("escape hatch allows localhost when set", bypass)
    os.environ.pop("SSRF_ALLOW_PRIVATE", None)
    importlib.reload(ssrfguard)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL SSRF SCENARIOS PASSED (zero network calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())

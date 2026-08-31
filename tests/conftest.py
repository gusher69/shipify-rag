"""Reset the process-wide read caches added for the latency P0
(2026-08-31) before every test, so one test's cached Business Action /
prompt / policy / profile rows never leak into the next test's assertions.
Production relies on the short TTLs / explicit clears on these; tests need
a hard reset between cases."""
import pytest


@pytest.fixture(autouse=True)
def _reset_runtime_caches():
    try:
        import services.business_action_registry as _bar
        _bar._CONFIG_CACHE.clear()
        _bar._instance = None
    except Exception:
        pass
    try:
        import services.prompt_builder as _pb
        _pb._PROMPT_CACHE.clear()
    except Exception:
        pass
    try:
        import services.policy_studio_service as _ps
        _ps._DEFAULT_SET_CACHE["ts"] = 0.0
        _ps._DEFAULT_SET_CACHE["val"] = None
    except Exception:
        pass
    try:
        import profiles.manager as _pm
        _pm._PROFILE_CACHE.clear()
    except Exception:
        pass
    yield

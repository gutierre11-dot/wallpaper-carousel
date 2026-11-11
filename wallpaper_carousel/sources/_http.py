"""Helpers HTTP compartilhados das fontes (requests.Session + retry)."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Status que valem retry (rate-limit e falhas transitórias do servidor).
_RETRY_STATUSES = (403, 429, 500, 502, 503, 504)


def make_session(*, retries: int = 3, backoff_factor: float = 1.0) -> requests.Session:
    """Session com timeout-friendly adapter e retry exponencial (urllib3).

    - ``retries``: número de retentativas após a primeira tentativa.
    - ``backoff_factor``: base do backoff exponencial do urllib3
      (sleep ≈ backoff_factor * 2**tentativa, com jitter).
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff_factor,
        status_forcelist=_RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

"""
sites/ — One file per supported site.

Every extractor follows the same contract:
    extract(url: str, session, state) -> None

It prints progress, downloads files, and calls summary.report() at the end.
It never touches globals — all state via AppState.
"""

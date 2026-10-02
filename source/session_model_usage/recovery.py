"""Compatibility entry point; recovery now belongs to the persistent manager."""
from .supervisor import run_supervisor


def run_recovery(run_id: str) -> int:
    return run_supervisor(run_id, open_requested=True)

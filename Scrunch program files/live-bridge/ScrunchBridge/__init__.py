"""Scrunch-owned Ableton Remote Script bridge (not installed by Scrunch).

Selecting this control surface only starts a loopback request server. It never
runs a self-test, creates tracks or writes to the Set on startup.
"""
from .bridge import ScrunchBridge


def create_instance(c_instance):
    return ScrunchBridge(c_instance)

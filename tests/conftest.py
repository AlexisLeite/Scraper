"""Select the separate local Jev credential for pytest runs."""
from _credentials import activate_test_key


def pytest_sessionstart(session):
    activate_test_key()

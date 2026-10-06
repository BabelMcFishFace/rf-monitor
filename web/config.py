#!/usr/bin/env python3

"""
RF Monitor
==========

Central configuration for the web application.

Keeping paths, URLs, and timing values here means the rest of the
application does not need hard-coded configuration scattered through
multiple files.
"""

from pathlib import Path


# ============================================================================
# Project paths
# ============================================================================

WEB_DIR = Path(__file__).resolve().parent

BASE_DIR = WEB_DIR.parent

DB_PATH = BASE_DIR / "rf_monitor.db"


# ============================================================================
# Offline database directory
# ============================================================================

DATA_DIR = BASE_DIR / "data"

IEEE_DIR = DATA_DIR / "ieee"


# ============================================================================
# Local offline database files
# ============================================================================

BLUETOOTH_COMPANY_DATABASE = (
    DATA_DIR / "company_ids.json"
)

IEEE_MA_L_DATABASE = (
    IEEE_DIR / "ma-l.csv"
)

IEEE_MA_M_DATABASE = (
    IEEE_DIR / "ma-m.csv"
)


# ============================================================================
# Default download URLs
# ============================================================================

DEFAULT_BLUETOOTH_COMPANY_URL = (
    "https://raw.githubusercontent.com/"
    "nordicsemi/bluetooth-numbers-database/"
    "master/v1/company_ids.json"
)


DEFAULT_IEEE_MA_L_URL = (
    "https://standards-oui.ieee.org/"
    "oui/oui.csv"
)


DEFAULT_IEEE_MA_M_URL = (
    "https://standards-oui.ieee.org/"
    "oui28/mam.csv"
)


# ============================================================================
# Manufacturer lookup behaviour
# ============================================================================

MANUFACTURER_HTTP_TIMEOUT = 5

MANUFACTURER_RETRY_HOURS = 24


# ============================================================================
# Database download behaviour
# ============================================================================

DATABASE_DOWNLOAD_TIMEOUT = 30

DATABASE_DOWNLOAD_RETRIES = 3


# ============================================================================
# Dashboard behaviour
# ============================================================================

DEVICE_REFRESH_SECONDS = 10


# ============================================================================
# Application
# ============================================================================

FLASK_HOST = "0.0.0.0"

FLASK_PORT = 5000


# ============================================================================
# Directory setup
# ============================================================================

def ensure_data_directories():
    """
    Ensure all local offline database directories exist.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    IEEE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

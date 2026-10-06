#!/usr/bin/env python3

"""
RF Monitor - Offline Manufacturer Databases
============================================

Handles local copies of:

    - Bluetooth Company Identifiers
    - IEEE MA-L assignments
    - IEEE MA-M assignments

The dashboard can therefore identify manufacturers without Internet access.

The files are downloaded by the web application's Settings page.
"""

import csv
import json
from pathlib import Path


# ===========================================================================
# Paths
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

IEEE_DIR = DATA_DIR / "ieee"

COMPANY_IDS_FILE = DATA_DIR / "company_ids.json"

MA_L_FILE = IEEE_DIR / "ma-l.csv"

MA_M_FILE = IEEE_DIR / "ma-m.csv"


# ===========================================================================
# Database definitions
# ===========================================================================

DATABASES = {
    "company_ids": {
        "name": "Bluetooth Company IDs",
        "filename": COMPANY_IDS_FILE,
        "url": (
            "https://raw.githubusercontent.com/"
            "nordicsemi/bluetooth-numbers-database/"
            "master/v1/company_ids.json"
        ),
    },

    "ma_l": {
        "name": "IEEE MA-L",
        "filename": MA_L_FILE,
        "url": (
            "https://standards-oui.ieee.org/"
            "oui/oui.csv"
        ),
    },

    "ma_m": {
        "name": "IEEE MA-M",
        "filename": MA_M_FILE,
        "url": (
            "https://standards-oui.ieee.org/"
            "oui28/mam.csv"
        ),
    },
}


# ===========================================================================
# Directory setup
# ===========================================================================

def ensure_directories():
    """
    Ensure all local database directories exist.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    IEEE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


# ===========================================================================
# Helpers
# ===========================================================================

def normalise_hex(value):
    """
    Convert a MAC/OUI value into uppercase hexadecimal characters only.
    """

    if value is None:
        return ""

    value = str(value).strip().upper()

    for separator in (
        ":",
        "-",
        ".",
        " ",
    ):
        value = value.replace(
            separator,
            ""
        )

    return value


def mac_to_oui(mac):
    """
    Return the first six hexadecimal characters of a MAC address.
    """

    value = normalise_hex(mac)

    if len(value) < 6:
        return None

    return value[:6]


def mac_to_ma_m(mac):
    """
    Return the first seven hexadecimal characters used by MA-M.

    MA-M assignments use 28 bits, therefore seven hex digits.
    """

    value = normalise_hex(mac)

    if len(value) < 7:
        return None

    return value[:7]


# ===========================================================================
# Bluetooth Company ID database
# ===========================================================================

def lookup_company_id(manufacturer_id):
    """
    Look up a Bluetooth Company Identifier locally.

    Returns the manufacturer name or None.
    """

    if manufacturer_id is None:
        return None

    try:
        manufacturer_id = int(manufacturer_id)

    except (TypeError, ValueError):
        return None

    if not COMPANY_IDS_FILE.exists():
        return None

    try:

        with COMPANY_IDS_FILE.open(
            "r",
            encoding="utf-8"
        ) as handle:

            data = json.load(handle)

    except (
        OSError,
        ValueError,
        json.JSONDecodeError
    ):
        return None

    if not isinstance(data, list):
        return None

    for entry in data:

        if not isinstance(entry, dict):
            continue

        code = entry.get("code")

        try:
            code = int(code)

        except (TypeError, ValueError):
            continue

        if code != manufacturer_id:
            continue

        name = entry.get("name")

        if name:
            return str(name).strip()

    return None


# ===========================================================================
# IEEE MA-L
# ===========================================================================

def lookup_ma_l(mac):
    """
    Look up the first 24 bits of a MAC address in IEEE MA-L.
    """

    oui = mac_to_oui(mac)

    if not oui:
        return None

    if not MA_L_FILE.exists():
        return None

    try:

        with MA_L_FILE.open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline=""
        ) as handle:

            reader = csv.reader(handle)

            for row in reader:

                if len(row) < 3:
                    continue

                assignment = normalise_hex(row[1])

                if assignment == oui:

                    organization = row[2].strip()

                    if organization:
                        return organization

    except OSError:
        return None

    return None


# ===========================================================================
# IEEE MA-M
# ===========================================================================

def lookup_ma_m(mac):
    """
    Look up the first 28 bits of a MAC address in IEEE MA-M.

    The exact IEEE CSV format can change, so this parser deliberately
    examines the assignment field rather than depending on a fixed
    number of columns.
    """

    prefix = mac_to_ma_m(mac)

    if not prefix:
        return None

    if not MA_M_FILE.exists():
        return None

    try:

        with MA_M_FILE.open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline=""
        ) as handle:

            reader = csv.reader(handle)

            for row in reader:

                if len(row) < 3:
                    continue

                assignment = normalise_hex(row[1])

                if not assignment:
                    continue

                if assignment == prefix:

                    organization = row[2].strip()

                    if organization:
                        return organization

    except OSError:
        return None

    return None


# ===========================================================================
# Combined MAC lookup
# ===========================================================================

def lookup_mac(mac):
    """
    Look up a MAC address against the local IEEE databases.

    MA-L is checked first because it covers the traditional 24-bit
    OUI allocation.

    Returns:

        {
            "name": "...",
            "source": "IEEE MA-L"
        }

    or None.
    """

    if not mac:
        return None

    name = lookup_ma_l(mac)

    if name:

        return {
            "name": name,
            "source": "IEEE MA-L"
        }

    name = lookup_ma_m(mac)

    if name:

        return {
            "name": name,
            "source": "IEEE MA-M"
        }

    return None


# ===========================================================================
# Database status
# ===========================================================================

def count_company_ids():
    """
    Count Bluetooth Company ID entries.
    """

    if not COMPANY_IDS_FILE.exists():
        return 0

    try:

        with COMPANY_IDS_FILE.open(
            "r",
            encoding="utf-8"
        ) as handle:

            data = json.load(handle)

        if isinstance(data, list):
            return len(data)

    except (
        OSError,
        ValueError,
        json.JSONDecodeError
    ):
        pass

    return 0


def count_csv_rows(path):
    """
    Count data rows in an IEEE CSV file.
    """

    if not path.exists():
        return 0

    count = 0

    try:

        with path.open(
            "r",
            encoding="utf-8-sig",
            errors="replace"
        ) as handle:

            for line in handle:

                if line.strip():
                    count += 1

    except OSError:
        return 0

    return count


def database_status():

    ensure_directories()

    return {
        "company_ids": {
            "name": DATABASES["company_ids"]["name"],
            "path": str(COMPANY_IDS_FILE),
            "exists": COMPANY_IDS_FILE.exists(),
            "records": count_company_ids(),
        },

        "ma_l": {
            "name": DATABASES["ma_l"]["name"],
            "path": str(MA_L_FILE),
            "exists": MA_L_FILE.exists(),
            "records": count_csv_rows(MA_L_FILE),
        },

        "ma_m": {
            "name": DATABASES["ma_m"]["name"],
            "path": str(MA_M_FILE),
            "exists": MA_M_FILE.exists(),
            "records": count_csv_rows(MA_M_FILE),
        },
    }


# ===========================================================================
# Command-line test
# ===========================================================================

if __name__ == "__main__":

    ensure_directories()

    print("RF Monitor offline manufacturer databases")
    print()
    print(f"Data directory: {DATA_DIR}")
    print()

    status = database_status()

    for key, item in status.items():

        print(
            f"{item['name']}: "
            f"{item['records']} records "
            f"({'available' if item['exists'] else 'missing'})"
        )

    print()

    test_mac = "E8:06:90:2C:76:E9"

    print(
        f"MAC test: {test_mac}"
    )

    print(
        "MA-L:",
        lookup_ma_l(test_mac)
    )

    print(
        "MA-M:",
        lookup_ma_m(test_mac)
    )

    print(
        "Combined:",
        lookup_mac(test_mac)
    )

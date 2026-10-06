#!/usr/bin/env python3

"""
RF Monitor manufacturer identification.

Lookup order:

1. Bluetooth Company ID database
2. IEEE MA-L
3. IEEE MA-M
4. Optional internet fallback

The IEEE databases are loaded into memory once and then reused.
This avoids repeatedly scanning multi-megabyte CSV files for every
device displayed by the dashboard.
"""

import csv
import json
import urllib.error
import urllib.request

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

IEEE_DIR = DATA_DIR / "ieee"


COMPANY_IDS_PATH = DATA_DIR / "company_ids.json"

MA_L_PATH = IEEE_DIR / "ma-l.csv"

MA_M_PATH = IEEE_DIR / "ma-m.csv"


COMPANY_IDS_URL = (
    "https://raw.githubusercontent.com/"
    "nordicsemi/bluetooth-numbers-database/"
    "master/v1/company_ids.json"
)


HTTP_TIMEOUT = 10

USER_AGENT = "RF-Monitor/1.0 Raspberry-Pi"


# ---------------------------------------------------------------------------
# In-memory caches
# ---------------------------------------------------------------------------

_company_ids_cache = None

_ma_l_cache = None

_ma_m_cache = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_mac(mac):
    """
    Convert a MAC address to uppercase hexadecimal with separators removed.
    """

    if mac is None:
        return ""

    return "".join(
        character
        for character in str(mac).upper()
        if character in "0123456789ABCDEF"
    )


def mac_prefix(mac, length):
    """
    Return the requested number of hexadecimal characters from a MAC.
    """

    value = normalise_mac(mac)

    return value[:length]


# ---------------------------------------------------------------------------
# Bluetooth Company IDs
# ---------------------------------------------------------------------------

def load_company_ids():
    """
    Load the local Bluetooth Company ID database once.
    """

    global _company_ids_cache

    if _company_ids_cache is not None:
        return _company_ids_cache

    result = {}

    try:

        with COMPANY_IDS_PATH.open(
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(data, list):

            for entry in data:

                if not isinstance(entry, dict):
                    continue

                code = entry.get("code")

                name = entry.get("name")

                if not isinstance(code, int):
                    continue

                if not name:
                    continue

                result[code] = str(name).strip()

    except (
        OSError,
        ValueError,
        json.JSONDecodeError
    ):

        result = {}

    _company_ids_cache = result

    return result


def lookup_company_id(manufacturer_id):
    """
    Look up a Bluetooth Company Identifier in the official local database.

    Custom Company IDs are deliberately NOT checked here.

    The caller can therefore enforce the lookup order:

        1. Official database
        2. Custom database
        3. Internet fallback
    """

    try:
        manufacturer_id = int(manufacturer_id)

    except (TypeError, ValueError):
        return None

    company_ids = load_company_ids()

    return company_ids.get(
        manufacturer_id
    )


def lookup_custom_company_id(db, manufacturer_id):
    """
    Look up a user-defined Bluetooth Company Identifier.

    Returns the custom company name or None.

    Custom entries are only consulted after the official Company ID
    database has failed to identify the manufacturer.
    """

    if db is None:
        return None

    try:
        manufacturer_id = int(manufacturer_id)

    except (TypeError, ValueError):
        return None

    try:
        row = db.execute(
            """
            SELECT company_name
            FROM custom_company_ids
            WHERE company_id = ?
            """,
            (manufacturer_id,),
        ).fetchone()

    except Exception:
        return None

    if row is None:
        return None

    try:
        name = row["company_name"]
    except (TypeError, KeyError, IndexError):
        try:
            name = row[0]
        except (TypeError, IndexError):
            return None

    if not name:
        return None

    return str(name).strip() or None


# ---------------------------------------------------------------------------
# IEEE MA-L
# ---------------------------------------------------------------------------

def load_ma_l():
    """
    Load the IEEE MA-L database once.

    The resulting dictionary maps:

        E80690 -> Espressif Inc.
    """

    global _ma_l_cache

    if _ma_l_cache is not None:
        return _ma_l_cache

    result = {}

    try:

        with MA_L_PATH.open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline=""
        ) as file:

            reader = csv.reader(file)

            for row in reader:

                if len(row) < 3:
                    continue

                prefix = (
                    row[1]
                    .strip()
                    .replace("-", "")
                    .replace(":", "")
                    .upper()
                )

                name = row[2].strip()

                if len(prefix) != 6:
                    continue

                if not name:
                    continue

                result[prefix] = name

    except OSError:

        result = {}

    _ma_l_cache = result

    return result


# ---------------------------------------------------------------------------
# IEEE MA-M
# ---------------------------------------------------------------------------

def load_ma_m():
    """
    Load the IEEE MA-M database once.

    MA-M uses 28-bit / 7-hex-character prefixes.
    """

    global _ma_m_cache

    if _ma_m_cache is not None:
        return _ma_m_cache

    result = {}

    try:

        with MA_M_PATH.open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline=""
        ) as file:

            reader = csv.reader(file)

            for row in reader:

                if len(row) < 3:
                    continue

                prefix = (
                    row[1]
                    .strip()
                    .replace("-", "")
                    .replace(":", "")
                    .upper()
                )

                name = row[2].strip()

                if len(prefix) != 7:
                    continue

                if not name:
                    continue

                result[prefix] = name

    except OSError:

        result = {}

    _ma_m_cache = result

    return result


# ---------------------------------------------------------------------------
# IEEE lookup
# ---------------------------------------------------------------------------

def lookup_ieee_mac(mac_address):
    """
    Identify a MAC address using the local IEEE databases.

    MA-L is checked first, followed by MA-M.

    Returns None when no local match exists.
    """

    mac = normalise_mac(
        mac_address
    )

    if len(mac) < 6:
        return None

    # MA-L
    prefix = mac[:6]

    ma_l = load_ma_l()

    name = ma_l.get(prefix)

    if name:

        return {
            "name": name,
            "database": "MA-L",
            "mac_prefix": prefix,
        }

    # MA-M
    if len(mac) >= 7:

        prefix = mac[:7]

        ma_m = load_ma_m()

        name = ma_m.get(prefix)

        if name:

            return {
                "name": name,
                "database": "MA-M",
                "mac_prefix": prefix,
            }

    return None


# ---------------------------------------------------------------------------
# Internet fallback
# ---------------------------------------------------------------------------

def online_company_lookup(manufacturer_id):
    """
    Optional internet lookup for Bluetooth Company IDs.

    This is deliberately separate from the normal offline path.
    """

    try:

        request = urllib.request.Request(
            COMPANY_IDS_URL,
            headers={
                "User-Agent": USER_AGENT
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT
        ) as response:

            data = response.read()

        entries = json.loads(
            data.decode("utf-8")
        )

        if not isinstance(entries, list):
            return None

        for entry in entries:

            if not isinstance(entry, dict):
                continue

            if entry.get("code") != manufacturer_id:
                continue

            name = entry.get("name")

            if name:
                return str(name).strip()

    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError
    ):

        pass

    return None


# ---------------------------------------------------------------------------
# Main identification function
# ---------------------------------------------------------------------------

def identify_manufacturer(
    db,
    manufacturer_id=None,
    mac_address=None,
    allow_internet=True,
):
    """
    Identify a manufacturer using local databases first.

    Returns a dictionary containing:

        id
        name
        source

    IEEE matches additionally contain:

        database
        mac_prefix
    """

    # ------------------------------------------------------------------
    # IEEE MAC lookup
    # ------------------------------------------------------------------

    if (
        mac_address
        and manufacturer_id is None
    ):

        ieee_result = lookup_ieee_mac(
            mac_address
        )

        if ieee_result:

            return {
                "id": manufacturer_id,
                "name": ieee_result["name"],
                "source": "Local IEEE MAC/OUI database",
                "database": ieee_result["database"],
                "mac_prefix": ieee_result["mac_prefix"],
            }

    # ------------------------------------------------------------------
    # Bluetooth Company ID lookup
    # ------------------------------------------------------------------

    if manufacturer_id is not None:

        # --------------------------------------------------------------
        # 1. Official Bluetooth Company ID database
        # --------------------------------------------------------------

        name = lookup_company_id(
            manufacturer_id
        )

        if name:

            return {
                "id": manufacturer_id,
                "name": name,
                "source": (
                    "Local Bluetooth Company ID database"
                ),
            }

        # --------------------------------------------------------------
        # 2. User-defined/custom Company ID database
        #
        # This is deliberately checked ONLY after the official
        # database has failed.
        # --------------------------------------------------------------

        name = lookup_custom_company_id(
            db,
            manufacturer_id
        )

        if name:

            return {
                "id": manufacturer_id,
                "name": name,
                "source": (
                    "Custom Company ID"
                ),
            }

    # ------------------------------------------------------------------
    # 3. Optional internet fallback
    # ------------------------------------------------------------------

    if (
        allow_internet
        and manufacturer_id is not None
    ):

        name = online_company_lookup(
            manufacturer_id
        )

        if name:

            return {
                "id": manufacturer_id,
                "name": name,
                "source": (
                    "Nordic Bluetooth Numbers Database"
                ),
            }

    # ------------------------------------------------------------------
    # Unknown
    # ------------------------------------------------------------------

    return {
        "id": manufacturer_id,
        "name": None,
        "source": None,
    }


# ---------------------------------------------------------------------------
# Dashboard compatibility
# ---------------------------------------------------------------------------

def get_manufacturers_for_observation(
    db,
    observation,
    allow_internet=False,
):
    """
    Resolve all manufacturer IDs contained in one BLE observation.

    Supports both normal dictionaries and sqlite3.Row objects.
    """

    try:
        manufacturer_data = observation["manufacturer_data"]
    except (KeyError, TypeError, IndexError):
        manufacturer_data = None

    if isinstance(
        manufacturer_data,
        str
    ):

        try:
            manufacturer_data = json.loads(
                manufacturer_data
            )

        except (
            ValueError,
            json.JSONDecodeError
        ):

            manufacturer_data = {}

    if not isinstance(
        manufacturer_data,
        dict
    ):
        manufacturer_data = {}

    try:
        address = observation["address"]
    except (KeyError, TypeError, IndexError):
        address = None

    results = []

    seen = set()

    # First identify using the MAC/OUI.
    ieee_result = identify_manufacturer(
        db,
        manufacturer_id=None,
        mac_address=address,
        allow_internet=False,
    )

    if ieee_result["name"]:

        key = (
            ieee_result["id"],
            ieee_result["name"],
            ieee_result.get("database"),
        )

        seen.add(key)

        results.append(
            ieee_result
        )

    # Then identify Bluetooth Company IDs in the advertisement.
    for key in manufacturer_data.keys():

        try:
            manufacturer_id = int(key)

        except (
            TypeError,
            ValueError
        ):

            continue

        result = identify_manufacturer(
            db,
            manufacturer_id=manufacturer_id,
            mac_address=address,
            allow_internet=allow_internet,
        )

        result_key = (
            result.get("id"),
            result.get("name"),
            result.get("database"),
        )

        if (
            result["name"]
            and result_key not in seen
        ):

            seen.add(result_key)

            results.append(
                result
            )

    results.sort(
        key=lambda item: (
            item["name"] is None,
            item["name"] or "",
            item["id"]
            if item["id"] is not None
            else -1,
        )
    )

    return results


# ---------------------------------------------------------------------------
# Cache control
# ---------------------------------------------------------------------------

def clear_lookup_caches():
    """
    Clear the in-memory lookup caches.

    Useful after updating the offline databases from Settings.
    """

    global _company_ids_cache
    global _ma_l_cache
    global _ma_m_cache

    _company_ids_cache = None
    _ma_l_cache = None
    _ma_m_cache = None


def preload_lookup_databases():
    """
    Load all local lookup databases into memory.

    This can be called when the Flask application starts so the first
    dashboard request doesn't have to pay the loading cost.
    """

    load_company_ids()
    load_ma_l()
    load_ma_m()


if __name__ == "__main__":

    print(
        "RF Monitor manufacturer lookup test"
    )

    preload_lookup_databases()

    result = identify_manufacturer(
        None,
        manufacturer_id=46517,
        mac_address="E8:06:90:2C:76:E9",
        allow_internet=False,
    )

    print(result)

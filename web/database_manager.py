#!/usr/bin/env python3

"""
RF Monitor Offline Database Manager

Manages downloadable offline lookup databases used by RF Monitor.

The databases themselves are stored locally on the Raspberry Pi.

Internet access is only required when the user explicitly requests
an update from the Settings page.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

IEEE_DIR = DATA_DIR / "ieee"


DATABASES = {
    "company_ids": {
        "name": "Bluetooth Company IDs",
        "description": (
            "Bluetooth SIG / Nordic Bluetooth Numbers Company Identifier database."
        ),
        "path": DATA_DIR / "company_ids.json",
        "url": (
            "https://raw.githubusercontent.com/"
            "nordicsemi/bluetooth-numbers-database/"
            "master/v1/company_ids.json"
        ),
    },

    "ieee_ma_l": {
        "name": "IEEE MA-L",
        "description": (
            "IEEE MAC Address Block Large / OUI database."
        ),
        "path": IEEE_DIR / "ma-l.csv",
        "url": (
            "https://standards-oui.ieee.org/"
            "oui/oui.csv"
        ),
    },

    "ieee_ma_m": {
        "name": "IEEE MA-M",
        "description": (
            "IEEE MAC Address Block Medium database."
        ),
        "path": IEEE_DIR / "ma-m.csv",
        "url": (
            "https://standards-oui.ieee.org/"
            "oui28/mam.csv"
        ),
    },
}


HTTP_TIMEOUT = 15

USER_AGENT = "RF-Monitor/1.0 Raspberry-Pi"


def ensure_directories():
    """
    Ensure the local data directories exist.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    IEEE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


def get_database_config(database_id):
    """
    Return the configuration for one database.
    """

    return DATABASES.get(database_id)


def get_database_status(database_id):
    """
    Return information about one local database.
    """

    config = get_database_config(
        database_id
    )

    if config is None:
        return None

    path = config["path"]

    exists = path.exists()

    size = (
        path.stat().st_size
        if exists
        else 0
    )

    modified = None

    if exists:
        try:
            modified = path.stat().st_mtime

        except OSError:
            modified = None

    return {
        "id": database_id,
        "name": config["name"],
        "description": config["description"],
        "path": str(path),
        "url": config["url"],
        "exists": exists,
        "size": size,
        "modified": modified,
    }


def get_all_database_status():
    """
    Return status information for all managed databases.
    """

    ensure_directories()

    return [
        get_database_status(database_id)
        for database_id in DATABASES
    ]


def validate_company_database(path):
    """
    Validate the downloaded Bluetooth Company ID JSON.
    """

    with path.open(
        "r",
        encoding="utf-8"
    ) as file:

        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(
            "Bluetooth company database is not a JSON list."
        )

    if not data:
        raise ValueError(
            "Bluetooth company database is empty."
        )

    valid_entries = 0

    for entry in data:

        if not isinstance(entry, dict):
            continue

        if (
            isinstance(
                entry.get("code"),
                int
            )
            and entry.get("name")
        ):
            valid_entries += 1

    if valid_entries == 0:
        raise ValueError(
            "Bluetooth company database contains no valid entries."
        )

    return valid_entries


def validate_csv_database(path):
    """
    Perform basic validation of an IEEE CSV database.

    We deliberately don't require a specific number of rows because
    IEEE may change the exact format over time.
    """

    with path.open(
        "r",
        encoding="utf-8-sig",
        errors="replace"
    ) as file:

        first_lines = []

        for _ in range(10):

            line = file.readline()

            if not line:
                break

            first_lines.append(
                line.strip()
            )

    if not first_lines:
        raise ValueError(
            "Downloaded IEEE database is empty."
        )

    content = "\n".join(
        first_lines
    ).upper()

    if "IEEE" not in content and "MA-L" not in content:
        # Some IEEE files don't contain these exact strings in the
        # first lines, so this is intentionally only a soft check.
        pass

    return True


def validate_download(database_id, path):
    """
    Validate a downloaded database.
    """

    if database_id == "company_ids":
        return validate_company_database(
            path
        )

    if database_id in (
        "ieee_ma_l",
        "ieee_ma_m",
    ):
        return validate_csv_database(
            path
        )

    raise ValueError(
        f"Unknown database: {database_id}"
    )


def download_database(
    database_id,
    url=None
):
    """
    Download and validate one database.

    The existing database is preserved if the download or validation
    fails.

    Returns a result dictionary.
    """

    config = get_database_config(
        database_id
    )

    if config is None:
        return {
            "success": False,
            "error": "Unknown database."
        }

    ensure_directories()

    target = config["path"]

    download_url = (
        url.strip()
        if url
        else config["url"]
    )

    if not download_url:
        return {
            "success": False,
            "error": "No download URL configured."
        }

    temporary = target.with_suffix(
        target.suffix + ".download"
    )

    try:

        request = urllib.request.Request(
            download_url,
            headers={
                "User-Agent": USER_AGENT
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT
        ) as response:

            data = response.read()

        if not data:
            raise ValueError(
                "Downloaded file was empty."
            )

        with temporary.open(
            "wb"
        ) as file:

            file.write(data)

        validate_download(
            database_id,
            temporary
        )

        temporary.replace(
            target
        )

        return {
            "success": True,
            "database": get_database_status(
                database_id
            ),
        }

    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:

        try:
            temporary.unlink(
                missing_ok=True
            )

        except OSError:
            pass

        return {
            "success": False,
            "error": str(exc),
        }

    except Exception as exc:

        try:
            temporary.unlink(
                missing_ok=True
            )

        except OSError:
            pass

        return {
            "success": False,
            "error": (
                f"Unexpected error: {exc}"
            ),
        }

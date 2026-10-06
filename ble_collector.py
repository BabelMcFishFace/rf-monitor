#!/usr/bin/env python3

"""
BLE Environment Collector - v2

Purpose
-------
Continuously listen for Bluetooth Low Energy advertisements and maintain
a local SQLite database containing:

    * Known BLE devices
    * BLE fingerprints
    * Selected BLE observations
    * RSSI statistics

Privacy
-------
All collected information stays on this Raspberry Pi.

The Bluetooth address (MAC) is stored locally in SQLite because it is useful
for correlating observations from the same device.

The address is NOT printed by the collector. This makes it safer to paste
collector output into a support chat without accidentally exposing device
addresses.

Design
------
A BLE device may advertise many times per second. Recording every packet
creates unnecessary SD-card writes.

Instead, we:

    1. Update the device's current state for every advertisement.
    2. Build a fingerprint from the advertisement contents.
    3. Store a fingerprint when it has not been seen before.
    4. Store a detailed observation periodically, or when the fingerprint
       changes.
    5. Keep RSSI statistics for the device.

Existing v1 tables/data are preserved.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

from bleak import BleakScanner


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SQLite database is kept in the project directory.
DB_PATH = "rf_monitor.db"


def get_bluetooth_adapter(db):
    """
    Read the Bluetooth adapter selected in the web settings.

    'auto' means use Bleak's default BlueZ adapter.
    """

    try:

        row = db.execute("""
            SELECT value
            FROM app_settings
            WHERE key = 'bluetooth_adapter'
        """).fetchone()

    except sqlite3.OperationalError:

        # Older databases/installations may not have the table yet.
        return "auto"

    if row is None:
        return "auto"

    value = row[0]

    if value is None:
        return "auto"

    value = str(value).strip()

    return value or "auto"

# How often to store a normal RSSI observation for a device even when the
# advertisement fingerprint has not changed.
#
# Example:
#   A device advertises every 100 ms.
#   We do NOT want 10 database writes every second.
#
# Instead, once every 30 seconds is enough to build a useful RSSI history.
OBSERVATION_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now():
    """
    Return the current UTC time in ISO-8601 format.

    Using UTC means timestamps remain unambiguous if the Pi's timezone
    changes later or if the database is copied to another machine.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data conversion helpers
# ---------------------------------------------------------------------------

def json_safe_manufacturer_data(data):
    """
    Convert Bleak's manufacturer_data dictionary into JSON-safe data.

    Bleak gives us something similar to:

        {
            1234: b'\\x01\\x02\\x03'
        }

    SQLite will store JSON text, so binary payloads are converted to hex.

    The manufacturer/company ID is converted to a string because JSON object
    keys must be strings.
    """
    return {
        str(key): value.hex()
        for key, value in data.items()
    }


def json_safe_service_data(data):
    """
    Convert Bleak's service_data dictionary into JSON-safe data.

    BLE service-data payloads are binary, so we store them as hexadecimal
    strings.
    """
    return {
        str(key): value.hex()
        for key, value in data.items()
    }


def normalise_list(values):
    """
    Return a sorted list of strings.

    Advertisement data can sometimes arrive in a different order even though
    the actual information is the same.

    Sorting makes our fingerprint stable.

    Example:

        ["UUID-B", "UUID-A"]

    becomes:

        ["UUID-A", "UUID-B"]
    """
    return sorted(str(value) for value in values)


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def build_fingerprint(advertisement, name):
    """
    Build a deterministic fingerprint from advertisement characteristics.

    The fingerprint intentionally does NOT include:

        * MAC address
        * RSSI
        * timestamp

    Those values describe the observation, not the advertisement identity.

    The fingerprint DOES include:

        * advertised name
        * service UUIDs
        * manufacturer data
        * service data
        * TX power

    This lets us distinguish between:

        Device identity
            and
        Advertisement characteristics

    A device can therefore have more than one fingerprint if its advertised
    data changes.
    """

    manufacturer_data = json_safe_manufacturer_data(
        advertisement.manufacturer_data
    )

    service_data = json_safe_service_data(
        advertisement.service_data
    )

    fingerprint_data = {
        "name": name or "",
        "service_uuids": normalise_list(
            advertisement.service_uuids
        ),
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "tx_power": advertisement.tx_power,
    }

    # JSON with sorted keys gives us a deterministic representation.
    canonical = json.dumps(
        fingerprint_data,
        sort_keys=True,
        separators=(",", ":")
    )

    # SHA-256 gives us a compact identifier for the fingerprint.
    fingerprint_hash = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    return fingerprint_hash, fingerprint_data


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def init_db():
    """
    Create the database and tables if they do not already exist.

    IMPORTANT:
    ----------
    This function deliberately uses CREATE TABLE IF NOT EXISTS.

    It does not delete or recreate the existing database.

    That means the observations already collected by v1 remain available.
    """

    db = sqlite3.connect(DB_PATH)

    # ---------------------------------------------------------------
    # Existing device table
    # ---------------------------------------------------------------
    #
    # One row represents one Bluetooth address observed by the Pi.
    #
    # RSSI statistics are added to the existing table using ALTER TABLE
    # below. SQLite does not support IF NOT EXISTS for ADD COLUMN, so
    # we check the existing schema first.
    #
    db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db.execute("""
        INSERT OR IGNORE INTO app_settings (
            key,
            value
        )
        VALUES (
            'bluetooth_adapter',
            'auto'
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS ble_devices (
            address TEXT PRIMARY KEY,
            name TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_rssi INTEGER,
            sightings INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ---------------------------------------------------------------
    # Existing raw observation table
    # ---------------------------------------------------------------
    #
    # This table is intentionally retained.
    #
    # Older versions of the collector may have recorded every advertisement.
    # We don't delete those records.
    #
    db.execute("""
        CREATE TABLE IF NOT EXISTS ble_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            rssi INTEGER,
            name TEXT,
            service_uuids TEXT,
            manufacturer_data TEXT,
            service_data TEXT,
            tx_power INTEGER
        )
    """)

    # ---------------------------------------------------------------
    # Fingerprint table
    # ---------------------------------------------------------------
    #
    # A single BLE device can advertise different data at different times.
    #
    # Therefore fingerprints are stored separately from devices.
    #
    db.execute("""
        CREATE TABLE IF NOT EXISTS ble_fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            sightings INTEGER NOT NULL DEFAULT 1,
            name TEXT,
            service_uuids TEXT,
            manufacturer_data TEXT,
            service_data TEXT,
            tx_power INTEGER,
            UNIQUE(address, fingerprint)
        )
    """)

    # ---------------------------------------------------------------
    # Indexes
    # ---------------------------------------------------------------

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ble_observations_address
        ON ble_observations(address)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ble_observations_timestamp
        ON ble_observations(timestamp)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ble_fingerprints_address
        ON ble_fingerprints(address)
    """)

    # ---------------------------------------------------------------
    # Add RSSI statistics to the device table.
    #
    # These columns won't exist in a v1 database, so check first.
    # ---------------------------------------------------------------

    existing_columns = {
        row[1]
        for row in db.execute(
            "PRAGMA table_info(ble_devices)"
        ).fetchall()
    }

    if "min_rssi" not in existing_columns:
        db.execute("""
            ALTER TABLE ble_devices
            ADD COLUMN min_rssi INTEGER
        """)

    if "max_rssi" not in existing_columns:
        db.execute("""
            ALTER TABLE ble_devices
            ADD COLUMN max_rssi INTEGER
        """)

    if "avg_rssi" not in existing_columns:
        db.execute("""
            ALTER TABLE ble_devices
            ADD COLUMN avg_rssi REAL
        """)

    if "rssi_samples" not in existing_columns:
        db.execute("""
            ALTER TABLE ble_devices
            ADD COLUMN rssi_samples INTEGER NOT NULL DEFAULT 0
        """)

    db.commit()

    return db


# ---------------------------------------------------------------------------
# Device state
# ---------------------------------------------------------------------------

# This dictionary exists only while the collector is running.
#
# It prevents us from having to query SQLite for every BLE advertisement.
#
# Example:
#
#     last_observation[address] = monotonic_time
#
last_observation = {}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(db, device, advertisement):
    """
    Process one BLE advertisement.

    This function:

        * Updates the device record
        * Updates RSSI statistics
        * Calculates the advertisement fingerprint
        * Records new fingerprints
        * Periodically records detailed observations
    """

    timestamp = now()

    address = device.address

    # Prefer the advertisement's local name because it is what was actually
    # present in the BLE advertisement.
    #
    # Fall back to device.name if necessary.
    name = advertisement.local_name or device.name

    rssi = advertisement.rssi

    service_uuids = normalise_list(
        advertisement.service_uuids
    )

    manufacturer_data = json_safe_manufacturer_data(
        advertisement.manufacturer_data
    )

    service_data = json_safe_service_data(
        advertisement.service_data
    )

    tx_power = advertisement.tx_power

    # ---------------------------------------------------------------
    # Build advertisement fingerprint
    # ---------------------------------------------------------------

    fingerprint_hash, fingerprint_data = build_fingerprint(
        advertisement,
        name
    )

    # ---------------------------------------------------------------
    # Update the main device record
    # ---------------------------------------------------------------

    db.execute("""
        INSERT INTO ble_devices (
            address,
            name,
            first_seen,
            last_seen,
            last_rssi,
            sightings,
            min_rssi,
            max_rssi,
            avg_rssi,
            rssi_samples
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1)

        ON CONFLICT(address) DO UPDATE SET

            -- Only replace the stored name if the new advertisement
            -- actually provides one.
            name = COALESCE(excluded.name, ble_devices.name),

            last_seen = excluded.last_seen,
            last_rssi = excluded.last_rssi,

            sightings = ble_devices.sightings + 1,

            min_rssi =
                CASE
                    WHEN ble_devices.min_rssi IS NULL
                         OR excluded.last_rssi < ble_devices.min_rssi
                    THEN excluded.last_rssi
                    ELSE ble_devices.min_rssi
                END,

            max_rssi =
                CASE
                    WHEN ble_devices.max_rssi IS NULL
                         OR excluded.last_rssi > ble_devices.max_rssi
                    THEN excluded.last_rssi
                    ELSE ble_devices.max_rssi
                END,

            avg_rssi =
                CASE
                    WHEN excluded.last_rssi IS NULL
                    THEN ble_devices.avg_rssi

                    WHEN ble_devices.avg_rssi IS NULL
                    THEN excluded.last_rssi

                    ELSE (
                        (
                            ble_devices.avg_rssi *
                            ble_devices.rssi_samples
                        )
                        +
                        excluded.last_rssi
                    )
                    /
                    (ble_devices.rssi_samples + 1)
                END,

            rssi_samples =
                ble_devices.rssi_samples + 1
    """, (
        address,
        name,
        timestamp,
        timestamp,
        rssi,
        rssi,
        rssi,
        rssi
    ))

    # ---------------------------------------------------------------
    # Check whether this fingerprint already exists for this device.
    # ---------------------------------------------------------------

    existing = db.execute("""
        SELECT id
        FROM ble_fingerprints
        WHERE address = ?
          AND fingerprint = ?
    """, (
        address,
        fingerprint_hash
    )).fetchone()

    if existing:

        # We've seen this exact advertisement fingerprint before.
        #
        # Update its last-seen time and sighting count.
        db.execute("""
            UPDATE ble_fingerprints
            SET
                last_seen = ?,
                sightings = sightings + 1
            WHERE id = ?
        """, (
            timestamp,
            existing[0]
        ))

        new_fingerprint = False

    else:

        # First time this particular advertisement fingerprint has been
        # associated with this device.
        db.execute("""
            INSERT INTO ble_fingerprints (
                address,
                fingerprint,
                first_seen,
                last_seen,
                sightings,
                name,
                service_uuids,
                manufacturer_data,
                service_data,
                tx_power
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """, (
            address,
            fingerprint_hash,
            timestamp,
            timestamp,
            name,
            json.dumps(service_uuids),
            json.dumps(manufacturer_data),
            json.dumps(service_data),
            tx_power
        ))

        new_fingerprint = True

    # ---------------------------------------------------------------
    # Decide whether to store a detailed observation.
    # ---------------------------------------------------------------
    #
    # We always store the first observation for a device.
    #
    # After that, we normally wait OBSERVATION_INTERVAL seconds.
    #
    # A NEW fingerprint is also stored immediately because a change in
    # advertisement data is interesting.
    # ---------------------------------------------------------------

    current_monotonic = time.monotonic()

    previous_time = last_observation.get(address)

    should_record = (
        previous_time is None
        or new_fingerprint
        or (current_monotonic - previous_time) >= OBSERVATION_INTERVAL
    )

    if should_record:

        db.execute("""
            INSERT INTO ble_observations (
                address,
                timestamp,
                rssi,
                name,
                service_uuids,
                manufacturer_data,
                service_data,
                tx_power
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            address,
            timestamp,
            rssi,
            name,
            json.dumps(service_uuids),
            json.dumps(manufacturer_data),
            json.dumps(service_data),
            tx_power
        ))

        last_observation[address] = current_monotonic

    # Commit after processing the advertisement.

    db.commit()

    return new_fingerprint, should_record


# ---------------------------------------------------------------------------
# BLE callback
# ---------------------------------------------------------------------------

def detection_callback(device, advertisement):
    """
    Called by Bleak whenever a BLE advertisement is received.

    IMPORTANT:
    ----------
    This callback should remain lightweight because BLE advertisements can
    arrive frequently.
    """

    new_fingerprint, stored_observation = record(
        detection_callback.db,
        device,
        advertisement
    )

    name = advertisement.local_name or device.name or ""

    # Deliberately do NOT print the Bluetooth address.
    #
    # This makes terminal output safe to share without exposing the
    # addresses of nearby devices.

    flags = []

    if new_fingerprint:
        flags.append("NEW-FINGERPRINT")

    if stored_observation:
        flags.append("OBSERVED")

    flag_text = ",".join(flags) if flags else "-"

    print(
        f"RSSI={advertisement.rssi:4} "
        f"name={name!r} "
        f"services={len(advertisement.service_uuids)} "
        f"manufacturer={len(advertisement.manufacturer_data)} "
        f"service_data={len(advertisement.service_data)} "
        f"[{flag_text}]"
    )


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

async def main():
    """
    Start the BLE scanner and keep it running until Ctrl-C.
    """

    db = init_db()

    # Make the database available to the callback.
    detection_callback.db = db

    bluetooth_adapter = get_bluetooth_adapter(
        db
    )

    print(
        f"Bluetooth adapter: {bluetooth_adapter}"
    )

    scanner_kwargs = {
        "detection_callback":
            detection_callback
    }

    if bluetooth_adapter != "auto":

        scanner_kwargs["adapter"] = (
            bluetooth_adapter
        )

    scanner = BleakScanner(
        **scanner_kwargs
    )

    print("BLE collector v2 starting")
    print(f"Database: {DB_PATH}")
    print(
        f"Observation interval: "
        f"{OBSERVATION_INTERVAL:g} seconds"
    )
    print("MAC addresses are stored locally but not printed.")
    print("Press Ctrl-C to stop.")
    print()

    await scanner.start()

    try:

        # Keep the asyncio event loop alive while Bleak receives
        # advertisements.
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:

        print("\nStopping...")

    finally:

        await scanner.stop()
        db.close()

        print("BLE collector stopped.")


# ---------------------------------------------------------------------------
# Program entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())

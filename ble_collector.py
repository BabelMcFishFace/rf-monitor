#!/usr/bin/env python3

"""
BLE Environment Collector - v3

Features
--------
- Preserves existing v1/v2 SQLite tables and data.
- Avoids duplicate observations for unchanged advertisements.
- Stores new fingerprints only when advertisement data changes.
- Maintains RSSI statistics without writing every advertisement to disk.
- Batches database updates to reduce SD-card writes.
- Keeps Bluetooth addresses out of terminal output.
- Supports Bluetooth adapter selection through app_settings.

Observation policy
------------------
A detailed observation is stored when:
    1. A device is first discovered.
    2. Its advertisement fingerprint changes.

RSSI-only changes do not create observations.

Database updates are periodically flushed to SQLite.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

from bleak import BleakScanner


# ============================================================
# Configuration
# ============================================================

DB_PATH = "rf_monitor.db"

# How frequently pending device/fingerprint changes are saved.
FLUSH_INTERVAL = 5.0

# RSSI-only changes are ignored when deciding whether to record
# an observation. Set True to enable threshold-based observations.
RECORD_SIGNIFICANT_RSSI_CHANGES = False

# Used only when RECORD_SIGNIFICANT_RSSI_CHANGES is True.
RSSI_CHANGE_THRESHOLD = 10

# Keep terminal output manageable.
PRINT_EVENTS = True


# ============================================================
# Time and conversion helpers
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def json_safe_manufacturer_data(data):
    return {
        str(key): value.hex()
        for key, value in data.items()
    }


def json_safe_service_data(data):
    return {
        str(key): value.hex()
        for key, value in data.items()
    }


def normalise_list(values):
    return sorted(str(value) for value in (values or []))


def get_bluetooth_adapter(db):
    try:
        row = db.execute("""
            SELECT value
            FROM app_settings
            WHERE key = 'bluetooth_adapter'
        """).fetchone()
    except sqlite3.OperationalError:
        return "auto"

    if not row or not row[0]:
        return "auto"

    return str(row[0]).strip() or "auto"


# ============================================================
# Fingerprinting
# ============================================================

def build_fingerprint(advertisement, name):
    fingerprint_data = {
        "name": name or "",
        "service_uuids": normalise_list(
            advertisement.service_uuids
        ),
        "manufacturer_data": json_safe_manufacturer_data(
            advertisement.manufacturer_data
        ),
        "service_data": json_safe_service_data(
            advertisement.service_data
        ),
        "tx_power": advertisement.tx_power,
    }

    canonical = json.dumps(
        fingerprint_data,
        sort_keys=True,
        separators=(",", ":"),
    )

    fingerprint_hash = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    return fingerprint_hash, fingerprint_data


# ============================================================
# Database setup
# ============================================================

def init_db():
    db = sqlite3.connect(DB_PATH)

    # Reduce overhead while retaining normal SQLite durability.
    db.execute("PRAGMA busy_timeout = 5000")

    db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('bluetooth_adapter', 'auto')
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

    # Add RSSI columns to older databases without deleting data.
    existing_columns = {
        row[1]
        for row in db.execute(
            "PRAGMA table_info(ble_devices)"
        ).fetchall()
    }

    migrations = {
        "min_rssi": "INTEGER",
        "max_rssi": "INTEGER",
        "avg_rssi": "REAL",
        "rssi_samples": "INTEGER NOT NULL DEFAULT 0",
    }

    for column, definition in migrations.items():
        if column not in existing_columns:
            db.execute(
                f"ALTER TABLE ble_devices "
                f"ADD COLUMN {column} {definition}"
            )

    db.commit()

    return db


# ============================================================
# In-memory state
# ============================================================

# State is loaded from SQLite when the collector starts.
# Values are updated for every advertisement, but SQLite writes
# are deferred until flush_pending().

device_state = {}
pending_devices = set()
pending_fingerprints = {}
last_observation_fingerprint = {}
last_observation_rssi = {}

# Existing fingerprints are tracked so repeated advertisements do
# not repeatedly query or insert the same fingerprint.
known_fingerprints = set()

# Number of advertisements received for each fingerprint since
# the last flush. Used to batch the fingerprint sightings counter.
pending_fingerprint_sightings = {}


def load_state(db):
    for row in db.execute("""
        SELECT address, name, first_seen, last_seen, last_rssi,
               sightings, min_rssi, max_rssi, avg_rssi, rssi_samples
        FROM ble_devices
    """):
        (
            address, name, first_seen, last_seen, last_rssi,
            sightings, min_rssi, max_rssi, avg_rssi, rssi_samples
        ) = row

        device_state[address] = {
            "name": name,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "last_rssi": last_rssi,
            "sightings": sightings,
            "min_rssi": min_rssi,
            "max_rssi": max_rssi,
            "avg_rssi": avg_rssi,
            "rssi_samples": rssi_samples,
            "dirty": False,
        }

    for address, fingerprint in db.execute("""
        SELECT address, fingerprint
        FROM ble_fingerprints
    """):
        known_fingerprints.add((address, fingerprint))

    # Determine the most recently observed fingerprint for each
    # device so restarting the collector doesn't create a duplicate
    # observation for an unchanged advertisement.
    for address, fingerprint in db.execute("""
        SELECT o.address, f.fingerprint
        FROM ble_observations AS o
        JOIN ble_fingerprints AS f
          ON f.address = o.address
        WHERE NOT EXISTS (
            SELECT 1
            FROM ble_observations AS newer
            WHERE newer.address = o.address
              AND (
                  newer.timestamp > o.timestamp
                  OR (
                      newer.timestamp = o.timestamp
                      AND newer.id > o.id
                  )
              )
        )
    """):
        last_observation_fingerprint[address] = fingerprint

    # The previous observation's RSSI is useful only for the optional
    # threshold mode. Load it from the latest observation.
    for address, rssi in db.execute("""
        SELECT o.address, o.rssi
        FROM ble_observations AS o
        WHERE NOT EXISTS (
            SELECT 1
            FROM ble_observations AS newer
            WHERE newer.address = o.address
              AND (
                  newer.timestamp > o.timestamp
                  OR (
                      newer.timestamp = o.timestamp
                      AND newer.id > o.id
                  )
              )
        )
    """):
        last_observation_rssi[address] = rssi


# ============================================================
# Advertisement processing
# ============================================================

def record(db, device, advertisement):
    timestamp = now()
    address = device.address

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

    fingerprint_hash, fingerprint_data = build_fingerprint(
        advertisement,
        name,
    )

    fingerprint_key = (address, fingerprint_hash)
    state = device_state.get(address)
    new_device = state is None

    # --------------------------------------------------------
    # Initialise a newly discovered device in memory.
    # --------------------------------------------------------

    if new_device:
        state = {
            "name": name,
            "first_seen": timestamp,
            "last_seen": timestamp,
            "last_rssi": rssi,
            "sightings": 0,
            "min_rssi": None,
            "max_rssi": None,
            "avg_rssi": None,
            "rssi_samples": 0,
            "dirty": True,
        }

        device_state[address] = state

    # --------------------------------------------------------
    # Update device statistics in memory.
    # --------------------------------------------------------

    state["last_seen"] = timestamp
    state["last_rssi"] = rssi

    if name:
        state["name"] = name

    state["sightings"] += 1

    if rssi is not None:
        if state["min_rssi"] is None:
            state["min_rssi"] = rssi
        else:
            state["min_rssi"] = min(
                state["min_rssi"], rssi
            )

        if state["max_rssi"] is None:
            state["max_rssi"] = rssi
        else:
            state["max_rssi"] = max(
                state["max_rssi"], rssi
            )

        samples = state["rssi_samples"]
        avg = state["avg_rssi"]

        if avg is None or samples == 0:
            state["avg_rssi"] = float(rssi)
        else:
            state["avg_rssi"] = (
                avg * samples + rssi
            ) / (samples + 1)

        state["rssi_samples"] += 1

    state["dirty"] = True
    pending_devices.add(address)

    # --------------------------------------------------------
    # Fingerprint tracking.
    # --------------------------------------------------------

    new_fingerprint = fingerprint_key not in known_fingerprints

    if new_fingerprint:
        known_fingerprints.add(fingerprint_key)

        pending_fingerprints[fingerprint_key] = {
            "address": address,
            "fingerprint": fingerprint_hash,
            "first_seen": timestamp,
            "last_seen": timestamp,
            "sightings": 1,
            "name": name,
            "service_uuids": json.dumps(service_uuids),
            "manufacturer_data": json.dumps(
                manufacturer_data
            ),
            "service_data": json.dumps(service_data),
            "tx_power": tx_power,
        }

    else:
        pending_fingerprint_sightings[
            fingerprint_key
        ] = pending_fingerprint_sightings.get(
            fingerprint_key, 0
        ) + 1

    # --------------------------------------------------------
    # Decide whether to create a detailed observation.
    # --------------------------------------------------------

    previous_fingerprint = (
        last_observation_fingerprint.get(address)
    )

    should_record = (
        new_device
        or previous_fingerprint != fingerprint_hash
    )

    if (
        not should_record
        and RECORD_SIGNIFICANT_RSSI_CHANGES
        and rssi is not None
    ):
        previous_rssi = last_observation_rssi.get(address)

        if (
            previous_rssi is not None
            and abs(rssi - previous_rssi)
            >= RSSI_CHANGE_THRESHOLD
        ):
            should_record = True

    if should_record:
        # Store the pending observation in memory. It will be
        # written to SQLite during the next flush.
        pending_observation = {
            "address": address,
            "timestamp": timestamp,
            "rssi": rssi,
            "name": name,
            "service_uuids": json.dumps(service_uuids),
            "manufacturer_data": json.dumps(
                manufacturer_data
            ),
            "service_data": json.dumps(service_data),
            "tx_power": tx_power,
        }

        pending_observations.append(pending_observation)

        last_observation_fingerprint[address] = (
            fingerprint_hash
        )

        last_observation_rssi[address] = rssi

    if PRINT_EVENTS and (new_device or new_fingerprint or should_record):
        flags = []

        if new_device:
            flags.append("NEW-DEVICE")

        if new_fingerprint:
            flags.append("NEW-FINGERPRINT")

        if should_record:
            flags.append("OBSERVED")

        print(
            f"RSSI={str(rssi):>4} "
            f"name={(name or '')!r} "
            f"services={len(service_uuids)} "
            f"manufacturer={len(manufacturer_data)} "
            f"service_data={len(service_data)} "
            f"[{','.join(flags)}]"
        )


pending_observations = []


# ============================================================
# Batched database writes
# ============================================================

def flush_pending(db):
    global pending_observations

    if not (
        pending_devices
        or pending_fingerprints
        or pending_fingerprint_sightings
        or pending_observations
    ):
        return

    try:
        db.execute("BEGIN")

        # ----------------------------------------------------
        # Save changed device state.
        # ----------------------------------------------------

        for address in pending_devices:
            state = device_state[address]

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(address) DO UPDATE SET
                    name = COALESCE(
                        excluded.name,
                        ble_devices.name
                    ),
                    last_seen = excluded.last_seen,
                    last_rssi = excluded.last_rssi,
                    sightings = excluded.sightings,
                    min_rssi = excluded.min_rssi,
                    max_rssi = excluded.max_rssi,
                    avg_rssi = excluded.avg_rssi,
                    rssi_samples = excluded.rssi_samples
            """, (
                address,
                state["name"],
                state["first_seen"],
                state["last_seen"],
                state["last_rssi"],
                state["sightings"],
                state["min_rssi"],
                state["max_rssi"],
                state["avg_rssi"],
                state["rssi_samples"],
            ))

        # ----------------------------------------------------
        # Insert genuinely new fingerprints.
        # ----------------------------------------------------

        for item in pending_fingerprints.values():
            db.execute("""
                INSERT OR IGNORE INTO ble_fingerprints (
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item["address"],
                item["fingerprint"],
                item["first_seen"],
                item["last_seen"],
                item["sightings"],
                item["name"],
                item["service_uuids"],
                item["manufacturer_data"],
                item["service_data"],
                item["tx_power"],
            ))

        # ----------------------------------------------------
        # Batch sightings and last_seen updates for fingerprints.
        # ----------------------------------------------------

        for (address, fingerprint), count in (
            pending_fingerprint_sightings.items()
        ):
            db.execute("""
                UPDATE ble_fingerprints
                SET
                    last_seen = ?,
                    sightings = sightings + ?
                WHERE address = ?
                  AND fingerprint = ?
            """, (
                now(),
                count,
                address,
                fingerprint,
            ))

        # ----------------------------------------------------
        # Insert observations selected by the fingerprint logic.
        # ----------------------------------------------------

        db.executemany("""
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
        """, [
            (
                item["address"],
                item["timestamp"],
                item["rssi"],
                item["name"],
                item["service_uuids"],
                item["manufacturer_data"],
                item["service_data"],
                item["tx_power"],
            )
            for item in pending_observations
        ])

        db.commit()

    except Exception:
        db.rollback()
        raise

    pending_devices.clear()
    pending_fingerprints.clear()
    pending_fingerprint_sightings.clear()
    pending_observations.clear()


# ============================================================
# BLE callback
# ============================================================

def detection_callback(device, advertisement):
    try:
        record(
            detection_callback.db,
            device,
            advertisement,
        )
    except Exception as exc:
        # Do not print the device address.
        print(
            f"BLE processing error: "
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# Main program
# ============================================================

async def main():
    db = init_db()
    load_state(db)

    detection_callback.db = db

    bluetooth_adapter = get_bluetooth_adapter(db)

    print(f"Bluetooth adapter: {bluetooth_adapter}")
    print("BLE collector v3 starting")
    print(f"Database: {DB_PATH}")
    print(f"Database flush interval: {FLUSH_INTERVAL:g} seconds")
    print("Unchanged advertisements will not create observations.")
    print("RSSI-only changes will not create observations.")
    print("Bluetooth addresses are not printed.")
    print("Press Ctrl-C to stop.")
    print()

    scanner_kwargs = {
        "detection_callback": detection_callback,
    }

    if bluetooth_adapter != "auto":
        scanner_kwargs["adapter"] = bluetooth_adapter

    scanner = BleakScanner(**scanner_kwargs)

    try:
        await scanner.start()

        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            flush_pending(db)

    except asyncio.CancelledError:
        raise

    except KeyboardInterrupt:
        print("\nStopping...")

    finally:
        try:
            await scanner.stop()
        except Exception:
            pass

        # Save anything still waiting in memory before exiting.
        try:
            flush_pending(db)
        finally:
            db.close()

        print("BLE collector stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

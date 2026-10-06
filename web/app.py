#!/usr/bin/env python3

"""
RF Monitor Web Dashboard
========================

Local Flask dashboard for the Raspberry Pi BLE/RF monitor.

Responsibilities:

    - BLE device list
    - BLE device details
    - fingerprint information
    - local device-signature identification
    - manufacturer identification
    - offline manufacturer databases
    - database/settings management
    - device deletion from the GUI
    - collector start/stop/status control

The application is designed to continue working without internet access.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)


# ===========================================================================
# Application
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = BASE_DIR / "rf_monitor.db"

COLLECTOR_PATH = BASE_DIR / "ble_collector.py"

PYTHON_PATH = BASE_DIR / ".venv" / "bin" / "python"

app = Flask(__name__)


# ===========================================================================
# Collector process state
# ===========================================================================

collector_process = None


def collector_is_running():
    """
    Return True when the collector process started by this dashboard
    is still running.
    """

    global collector_process

    if collector_process is None:
        return False

    if collector_process.poll() is None:
        return True

    collector_process = None

    return False


def collector_status():
    """
    Return the current collector status.
    """

    running = collector_is_running()

    if running:

        return {
            "running": True,
            "pid": collector_process.pid,
            "collector": str(COLLECTOR_PATH),
        }

    return {
        "running": False,
        "pid": None,
        "collector": str(COLLECTOR_PATH),
    }


def start_collector():
    """
    Start ble_collector.py if it is not already running.
    """

    global collector_process

    if collector_is_running():

        return {
            "success": True,
            "already_running": True,
            **collector_status(),
        }

    if not COLLECTOR_PATH.exists():

        return {
            "success": False,
            "error": (
                "Collector script not found: "
                + str(COLLECTOR_PATH)
            ),
        }

    if not PYTHON_PATH.exists():

        return {
            "success": False,
            "error": (
                "Virtual environment Python not found: "
                + str(PYTHON_PATH)
            ),
        }

    try:

        collector_process = subprocess.Popen(
            [
                str(PYTHON_PATH),
                str(COLLECTOR_PATH),
            ],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Give the process a moment to fail immediately if there is
        # an environment/Bluetooth startup problem.
        time.sleep(0.5)

        if collector_process.poll() is not None:

            return_code = (
                collector_process.returncode
            )

            collector_process = None

            return {
                "success": False,
                "error": (
                    "Collector exited immediately "
                    f"with return code {return_code}."
                ),
            }

        return {
            "success": True,
            "already_running": False,
            **collector_status(),
        }

    except Exception as exc:

        collector_process = None

        app.logger.exception(
            "Unable to start collector"
        )

        return {
            "success": False,
            "error": str(exc),
        }


def stop_collector():
    """
    Stop the collector started by this dashboard.

    SIGINT is used first so ble_collector.py receives the same
    signal it normally receives from Ctrl-C.
    """

    global collector_process

    if not collector_is_running():

        return {
            "success": True,
            "already_stopped": True,
            **collector_status(),
        }

    process = collector_process

    try:

        os.killpg(
            process.pid,
            signal.SIGINT,
        )

        try:

            process.wait(
                timeout=5
            )

        except subprocess.TimeoutExpired:

            os.killpg(
                process.pid,
                signal.SIGTERM,
            )

            try:

                process.wait(
                    timeout=3
                )

            except subprocess.TimeoutExpired:

                os.killpg(
                    process.pid,
                    signal.SIGKILL,
                )

                process.wait(
                    timeout=2
                )

        collector_process = None

        return {
            "success": True,
            "already_stopped": False,
            "running": False,
            "pid": None,
            "collector": str(COLLECTOR_PATH),
        }

    except ProcessLookupError:

        collector_process = None

        return {
            "success": True,
            "already_stopped": True,
            **collector_status(),
        }

    except Exception as exc:

        app.logger.exception(
            "Unable to stop collector"
        )

        return {
            "success": False,
            "error": str(exc),
            **collector_status(),
        }


# ===========================================================================
# Project modules
# ===========================================================================

from web.manufacturer_lookup import (
    get_manufacturers_for_observation,
    preload_lookup_databases,
    clear_lookup_caches,
)

from web.database_manager import (
    get_all_database_status,
    get_database_config,
    download_database,
)


# Preload the offline manufacturer databases once at application startup.
preload_lookup_databases()


# ===========================================================================
# Database
# ===========================================================================

def get_db():
    """
    Open a SQLite connection to the local RF monitor database.

    Each request receives its own connection.
    """

    db = sqlite3.connect(DB_PATH)

    db.row_factory = sqlite3.Row

    return db


def ensure_database_schema(db):
    """
    Ensure tables required by the dashboard exist.

    Existing tables are not modified.
    """

    db.execute("""
        CREATE TABLE IF NOT EXISTS manufacturer_cache (
            manufacturer_id INTEGER PRIMARY KEY,
            manufacturer_name TEXT,
            source TEXT NOT NULL,
            last_updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_attempt TEXT
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_manufacturer_cache_name
        ON manufacturer_cache(manufacturer_name)
    """)

    db.commit()


# ===========================================================================
# JSON helpers
# ===========================================================================

def load_json(value, default):
    """
    Safely decode JSON stored in SQLite.
    """

    if not value:
        return default

    try:

        return json.loads(value)

    except (
        TypeError,
        json.JSONDecodeError,
    ):

        return default


# ===========================================================================
# Signature matching
# ===========================================================================

def normalise_hex(value):
    """
    Normalise hexadecimal data for comparisons.
    """

    if value is None:
        return ""

    value = str(value).strip().lower()

    for separator in (
        ":",
        "-",
        " ",
        "0x",
    ):

        value = value.replace(
            separator,
            "",
        )

    return value


def prefix_matches(data, prefix):
    """
    Return True if data begins with prefix.
    """

    if not prefix:
        return False

    data = normalise_hex(data)

    prefix = normalise_hex(prefix)

    return (
        bool(data)
        and bool(prefix)
        and data.startswith(prefix)
    )


def rssi_matches(rssi, signature):
    """
    Apply optional RSSI limits.

    RSSI is supporting evidence only.
    """

    if rssi is None:
        return True

    minimum = signature["min_rssi"]

    maximum = signature["max_rssi"]

    if (
        minimum is not None
        and rssi < minimum
    ):

        return False

    if (
        maximum is not None
        and rssi > maximum
    ):

        return False

    return True


def match_signature(
    observation,
    signature,
):
    """
    Compare one BLE observation with one configured signature.
    """

    manufacturer_data = load_json(
        observation["manufacturer_data"],
        {},
    )

    service_uuids = load_json(
        observation["service_uuids"],
        [],
    )

    service_data = load_json(
        observation["service_data"],
        {},
    )

    rssi = observation["rssi"]

    manufacturer_id = (
        signature["manufacturer_id"]
    )

    service_uuid = (
        signature["service_uuid"]
    )

    manufacturer_prefix = (
        signature["manufacturer_prefix"]
    )

    service_data_prefix = (
        signature["service_data_prefix"]
    )

    score = 0

    evidence = []

    manufacturer_key = None

    if manufacturer_id is not None:

        manufacturer_key = str(
            manufacturer_id
        )

    has_manufacturer_id = (
        manufacturer_key is not None
        and manufacturer_key in manufacturer_data
    )

    if has_manufacturer_id:

        score += 10

        evidence.append(
            f"manufacturer ID {manufacturer_id}"
        )

    if manufacturer_prefix:

        if not has_manufacturer_id:

            return 0, []

        manufacturer_payload = (
            manufacturer_data.get(
                manufacturer_key,
                "",
            )
        )

        if not prefix_matches(
            manufacturer_payload,
            manufacturer_prefix,
        ):

            return 0, []

        score += 80

        evidence.append(
            "manufacturer data prefix "
            + manufacturer_prefix
        )

    if service_uuid:

        wanted_uuid = (
            service_uuid.lower()
        )

        uuid_match = any(
            str(uuid).lower()
            == wanted_uuid
            for uuid in service_uuids
        )

        if not uuid_match:

            return 0, []

        score += 50

        evidence.append(
            f"service UUID {service_uuid}"
        )

    if service_data_prefix:

        matched_service_data = False

        for payload in service_data.values():

            if prefix_matches(
                payload,
                service_data_prefix,
            ):

                matched_service_data = True

                break

        if not matched_service_data:

            return 0, []

        score += 80

        evidence.append(
            "service data prefix "
            + service_data_prefix
        )

    if not rssi_matches(
        rssi,
        signature,
    ):

        return 0, []

    if (
        rssi is not None
        and (
            signature["min_rssi"] is not None
            or signature["max_rssi"] is not None
        )
    ):

        score += 5

        evidence.append(
            "RSSI within configured range"
        )

    has_specific_match = (
        bool(manufacturer_prefix)
        or bool(service_uuid)
        or bool(service_data_prefix)
    )

    if not has_specific_match:

        return 0, []

    score = min(
        score,
        100,
    )

    return score, evidence


def find_matches(
    db,
    observation,
):
    """
    Find all enabled signatures matching an observation.
    """

    signatures = db.execute("""
        SELECT
            id,
            name,
            device_type,
            description,
            manufacturer_id,
            service_uuid,
            manufacturer_prefix,
            service_data_prefix,
            min_rssi,
            max_rssi,
            confidence,
            enabled
        FROM device_signatures
        WHERE enabled = 1
        ORDER BY confidence DESC, id
    """).fetchall()

    matches = []

    for signature in signatures:

        score, evidence = match_signature(
            observation,
            signature,
        )

        if score <= 0:
            continue

        configured_confidence = (
            signature["confidence"]
        )

        final_confidence = round(
            (
                score
                * configured_confidence
            ) / 100
        )

        final_confidence = min(
            final_confidence,
            100,
        )

        matches.append({
            "id": signature["id"],
            "name": signature["name"],
            "device_type": signature["device_type"],
            "description": signature["description"],
            "confidence": final_confidence,
            "evidence": evidence,
        })

    matches.sort(
        key=lambda item: (
            item["confidence"],
            item["name"],
        ),
        reverse=True,
    )

    return matches


# ===========================================================================
# Device query helpers
# ===========================================================================

def get_device(
    db,
    address,
):

    return db.execute("""
        SELECT
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
        FROM ble_devices
        WHERE address = ?
    """, (
        address,
    )).fetchone()


def get_fingerprints(
    db,
    address,
):

    return db.execute("""
        SELECT
            id,
            fingerprint,
            first_seen,
            last_seen,
            sightings,
            name,
            service_uuids,
            manufacturer_data,
            service_data,
            tx_power
        FROM ble_fingerprints
        WHERE address = ?
        ORDER BY last_seen DESC
    """, (
        address,
    )).fetchall()


def get_recent_observation(
    db,
    address,
):

    return db.execute("""
        SELECT
            id,
            address,
            timestamp,
            rssi,
            name,
            service_uuids,
            manufacturer_data,
            service_data,
            tx_power
        FROM ble_observations
        WHERE address = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (
        address,
    )).fetchone()


def get_observations(
    db,
    address,
    limit=100,
):

    return db.execute("""
        SELECT
            timestamp,
            rssi,
            name
        FROM ble_observations
        WHERE address = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (
        address,
        limit,
    )).fetchall()


# ===========================================================================
# Device deletion
# ===========================================================================

def delete_device(
    db,
    address,
):
    """
    Delete one BLE device and all data belonging to it.
    """

    device = get_device(
        db,
        address,
    )

    if device is None:
        return False

    db.execute("""
        DELETE FROM ble_observations
        WHERE address = ?
    """, (
        address,
    ))

    db.execute("""
        DELETE FROM ble_fingerprints
        WHERE address = ?
    """, (
        address,
    ))

    db.execute("""
        DELETE FROM ble_devices
        WHERE address = ?
    """, (
        address,
    ))

    db.commit()

    return True


@app.route(
    "/device/<path:address>/delete",
    methods=["POST"],
)
def device_delete(address):
    """
    Delete a device from the GUI.
    """

    db = get_db()

    try:

        ensure_database_schema(db)

        deleted = delete_device(
            db,
            address,
        )

        if not deleted:

            return (
                "Device not found.",
                404,
            )

        return redirect(
            url_for("index")
        )

    except Exception:

        db.rollback()

        app.logger.exception(
            "Unable to delete device: %s",
            address,
        )

        return (
            "Unable to delete device.",
            500,
        )

    finally:

        db.close()


# ===========================================================================
# Device list
# ===========================================================================

def build_device_list(db):

    devices = db.execute("""
        SELECT
            d.address,
            d.name,
            d.first_seen,
            d.last_seen,
            d.last_rssi,
            d.sightings,
            d.min_rssi,
            d.max_rssi,
            d.avg_rssi,
            (
                SELECT COUNT(*)
                FROM ble_fingerprints f
                WHERE f.address = d.address
            ) AS fingerprint_count
        FROM ble_devices d
        ORDER BY d.last_seen DESC
    """).fetchall()

    output = []

    for device in devices:

        observation = get_recent_observation(
            db,
            device["address"],
        )

        matches = []

        manufacturers = []

        if observation is not None:

            matches = find_matches(
                db,
                observation,
            )

            manufacturers = (
                get_manufacturers_for_observation(
                    db,
                    observation,
                )
            )

        row = dict(device)

        row["matches"] = matches

        row["manufacturers"] = manufacturers

        if matches:

            row["identified_as"] = (
                matches[0]["name"]
            )

            row["identified_type"] = (
                matches[0]["device_type"]
            )

            row["confidence"] = (
                matches[0]["confidence"]
            )

        else:

            row["identified_as"] = None

            row["identified_type"] = None

            row["confidence"] = None

        output.append(row)

    return output


# ===========================================================================
# Main dashboard
# ===========================================================================

@app.route("/")
def index():

    db = get_db()

    try:

        ensure_database_schema(db)

        devices = build_device_list(
            db,
        )

        return render_template(
            "index.html",
            devices=devices,
        )

    finally:

        db.close()


# ===========================================================================
# Device detail
# ===========================================================================

@app.route("/device/<path:address>")
def device_detail(address):

    db = get_db()

    try:

        ensure_database_schema(db)

        device = get_device(
            db,
            address,
        )

        if device is None:

            abort(404)

        fingerprints = get_fingerprints(
            db,
            address,
        )

        observations = get_observations(
            db,
            address,
        )

        latest_observation = (
            get_recent_observation(
                db,
                address,
            )
        )

        matches = []

        manufacturers = []

        if latest_observation is not None:

            matches = find_matches(
                db,
                latest_observation,
            )

            manufacturers = (
                get_manufacturers_for_observation(
                    db,
                    latest_observation,
                )
            )

        return render_template(
            "device.html",
            device=device,
            fingerprints=fingerprints,
            observations=observations,
            matches=matches,
            manufacturers=manufacturers,
        )

    finally:

        db.close()


# ===========================================================================
# API: device list
# ===========================================================================

@app.route("/api/devices")
def api_devices():

    db = get_db()

    try:

        ensure_database_schema(db)

        devices = build_device_list(
            db,
        )

        return jsonify(devices)

    finally:

        db.close()


# ===========================================================================
# API: device details
# ===========================================================================

@app.route("/api/device/<path:address>")
def api_device(address):

    db = get_db()

    try:

        ensure_database_schema(db)

        device = get_device(
            db,
            address,
        )

        if device is None:

            abort(404)

        fingerprints = get_fingerprints(
            db,
            address,
        )

        observations = get_observations(
            db,
            address,
        )

        latest_observation = (
            get_recent_observation(
                db,
                address,
            )
        )

        matches = []

        manufacturers = []

        if latest_observation is not None:

            matches = find_matches(
                db,
                latest_observation,
            )

            manufacturers = (
                get_manufacturers_for_observation(
                    db,
                    latest_observation,
                )
            )

        return jsonify({
            "device": dict(device),

            "matches": matches,

            "manufacturers": manufacturers,

            "fingerprints": [
                dict(row)
                for row in fingerprints
            ],

            "observations": [
                dict(row)
                for row in observations
            ],
        })

    finally:

        db.close()


# ===========================================================================
# Collector API
# ===========================================================================

@app.route(
    "/api/collector/status",
    methods=["GET"],
)
def api_collector_status():

    return jsonify(
        collector_status()
    )


@app.route(
    "/api/collector/start",
    methods=["POST"],
)
def api_collector_start():

    result = start_collector()

    if not result.get("success"):

        return jsonify(result), 500

    return jsonify(result)


@app.route(
    "/api/collector/stop",
    methods=["POST"],
)
def api_collector_stop():

    result = stop_collector()

    if not result.get("success"):

        return jsonify(result), 500

    return jsonify(result)


# ===========================================================================
# Settings
# ===========================================================================

@app.route("/settings")
def settings():

    databases = get_all_database_status()

    return render_template(
        "settings.html",
        databases=databases,
    )


# ===========================================================================
# Settings API
# ===========================================================================

@app.route("/api/settings/databases")
def api_database_status():

    try:

        databases = get_all_database_status()

        return jsonify({
            "databases": databases,
        })

    except Exception as exc:

        app.logger.exception(
            "Unable to obtain database status"
        )

        return jsonify({
            "error": str(exc),
        }), 500


# ===========================================================================
# Database configuration API
# ===========================================================================

@app.route(
    "/api/settings/database/<database_id>",
    methods=["GET"],
)
def api_database_config(database_id):

    try:

        config = get_database_config(
            database_id,
        )

        if config is None:

            return jsonify({
                "error": "Unknown database",
            }), 404

        return jsonify(config)

    except Exception as exc:

        app.logger.exception(
            "Unable to obtain database configuration"
        )

        return jsonify({
            "error": str(exc),
        }), 500


# ===========================================================================
# Database update
# ===========================================================================

def perform_database_update(database_id):
    """
    Download a database and refresh the in-memory lookup cache.
    """

    payload = request.get_json(
        silent=True
    )

    if not isinstance(payload, dict):
        payload = {}

    custom_url = payload.get("url")

    if custom_url is not None:

        custom_url = str(
            custom_url
        ).strip()

        if not custom_url:
            custom_url = None

    result = download_database(
        database_id,
        url=custom_url,
    )

    if (
        isinstance(result, dict)
        and result.get("success")
    ):

        clear_lookup_caches()

        preload_lookup_databases()

        result["cache_refreshed"] = True

    return result


@app.route(
    "/api/settings/database/<database_id>/update",
    methods=["POST"],
)
def api_update_database(database_id):

    try:

        result = perform_database_update(
            database_id
        )

        if isinstance(result, dict):

            if result.get("success") is False:

                return jsonify(result), 400

            return jsonify(result)

        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as exc:

        app.logger.exception(
            "Database update failed: %s",
            database_id,
        )

        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


# ===========================================================================
# Application information
# ===========================================================================

@app.route("/api/settings")
def api_settings():

    try:

        databases = get_all_database_status()

        return jsonify({
            "base_dir": str(BASE_DIR),
            "database": str(DB_PATH),
            "databases": databases,
        })

    except Exception as exc:

        app.logger.exception(
            "Unable to obtain settings"
        )

        return jsonify({
            "error": str(exc),
        }), 500


# ===========================================================================
# Health check
# ===========================================================================

@app.route("/api/health")
def api_health():

    return jsonify({
        "status": "ok",
        "database": str(DB_PATH),
        "base_dir": str(BASE_DIR),
    })


# ===========================================================================
# Application entry point
# ===========================================================================

if __name__ == "__main__":

    print(
        "RF Monitor web dashboard"
    )

    print(
        f"Base directory: {BASE_DIR}"
    )

    print(
        f"Database: {DB_PATH}"
    )

    print(
        f"Collector: {COLLECTOR_PATH}"
    )

    print(
        f"Python: {PYTHON_PATH}"
    )

    print(
        "Manufacturer lookup: offline databases first"
    )

    print(
        "Manufacturer lookup: MAC/OUI fallback"
    )

    print(
        "Manufacturer lookup: optional internet fallback"
    )

    print(
        "Settings: /settings"
    )

    print(
        "Health check: /api/health"
    )

    print(
        "Collector control: /api/collector/status"
    )

    print(
        "Listening on all interfaces"
    )

    print(
        "Press Ctrl-C to stop"
    )

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )

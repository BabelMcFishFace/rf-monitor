#!/usr/bin/env python3

"""
BLE Device Signature Matcher
============================

This program compares stored BLE observations against locally
defined device signatures.

The BLE collector remains responsible for collecting raw data.

This program is responsible for interpreting that data.

Data flow:

    BLE advertisement
          |
          v
    ble_observations
          |
          v
    signature matcher
          |
          v
    possible device identity


IMPORTANT:

A signature is an indicator, not absolute proof of identity.

BLE devices can change addresses and advertisements can change
over time. The matcher therefore uses several pieces of evidence
where available:

    - Manufacturer ID
    - Manufacturer payload prefix
    - Service UUID
    - Service-data prefix
    - Optional RSSI range

The original BLE observations are never modified by this program.

All processing is local to the Raspberry Pi.
"""


import json
import sqlite3


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

# SQLite database containing the BLE collector data and signatures.
DB_PATH = "rf_monitor.db"


# -------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------

def hex_starts_with(value, prefix):
    """
    Return True when a hexadecimal value starts with a given prefix.

    Both values are converted to lowercase before comparison.

    Example:

        value  = "13523630315a4b..."
        prefix = "135236"

    Result:

        True
    """

    if not value or not prefix:
        return False

    return value.lower().startswith(
        prefix.lower()
    )


# -------------------------------------------------------------------
# Signature database
# -------------------------------------------------------------------

def load_signatures(db):
    """
    Load all enabled device signatures.

    Signatures are stored in SQLite so that new device types can
    be added without modifying the collector or this program.
    """

    rows = db.execute("""
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
            confidence
        FROM device_signatures
        WHERE enabled = 1
        ORDER BY confidence DESC
    """).fetchall()

    return rows


# -------------------------------------------------------------------
# Signature matching
# -------------------------------------------------------------------

def matches_signature(
    signature,
    manufacturer_data,
    service_uuids,
    service_data,
    rssi
):
    """
    Compare one BLE advertisement against one device signature.

    The important rule here is:

        If a signature specifies a manufacturer payload prefix,
        the prefix MUST match.

    This prevents several devices from the same manufacturer from
    being incorrectly identified as the same device.

    Example:

        EF-R00026
        HW51-1812
        R33-0002

    may all use manufacturer ID 46517.

    The manufacturer ID alone therefore isn't enough to identify
    them.

    Returns:

        {
            "matched": True/False,
            "score": integer,
            "reasons": [...]
        }
    """

    (
        signature_id,
        name,
        device_type,
        description,
        manufacturer_id,
        service_uuid,
        manufacturer_prefix,
        service_data_prefix,
        min_rssi,
        max_rssi,
        base_confidence,
    ) = signature

    score = 0

    reasons = []

    # ---------------------------------------------------------------
    # Manufacturer ID
    # ---------------------------------------------------------------
    #
    # If a signature specifies a manufacturer ID, the advertisement
    # must contain that ID.
    #

    if manufacturer_id is not None:

        manufacturer_key = str(
            manufacturer_id
        )

        if manufacturer_key in manufacturer_data:

            score += 20

            reasons.append(
                f"manufacturer ID {manufacturer_id}"
            )

        else:

            # Required manufacturer ID wasn't present.

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    # ---------------------------------------------------------------
    # Manufacturer payload prefix
    # ---------------------------------------------------------------
    #
    # This is usually the most useful discriminator when multiple
    # devices share the same manufacturer ID.
    #

    if manufacturer_prefix:

        prefix_match = False

        for key, value in manufacturer_data.items():

            if hex_starts_with(
                value,
                manufacturer_prefix
            ):

                prefix_match = True

                score += 60

                reasons.append(
                    "manufacturer data prefix "
                    f"{manufacturer_prefix}"
                )

                break

        # A signature that specifies a prefix MUST match it.

        if not prefix_match:

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    # ---------------------------------------------------------------
    # Service UUID
    # ---------------------------------------------------------------

    if service_uuid:

        service_uuid_lower = (
            service_uuid.lower()
        )

        uuid_match = any(
            uuid.lower() == service_uuid_lower
            for uuid in service_uuids
        )

        if uuid_match:

            score += 30

            reasons.append(
                f"service UUID {service_uuid}"
            )

        else:

            # A signature specifying a service UUID requires the
            # UUID to be present in the advertisement.

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    # ---------------------------------------------------------------
    # Service-data prefix
    # ---------------------------------------------------------------

    if service_data_prefix:

        service_prefix_match = False

        for key, value in service_data.items():

            if hex_starts_with(
                value,
                service_data_prefix
            ):

                service_prefix_match = True

                score += 40

                reasons.append(
                    "service data prefix "
                    f"{service_data_prefix}"
                )

                break

        if not service_prefix_match:

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    # ---------------------------------------------------------------
    # RSSI range
    # ---------------------------------------------------------------
    #
    # RSSI is deliberately not used as primary identity evidence.
    #
    # It varies with:
    #
    #   - distance
    #   - walls
    #   - orientation
    #   - interference
    #   - antenna position
    #
    # It is therefore only an optional filter.
    #

    if min_rssi is not None and rssi is not None:

        if rssi < min_rssi:

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    if max_rssi is not None and rssi is not None:

        if rssi > max_rssi:

            return {
                "matched": False,
                "score": 0,
                "reasons": []
            }

    # ---------------------------------------------------------------
    # No identifying evidence
    # ---------------------------------------------------------------

    if score == 0:

        return {
            "matched": False,
            "score": 0,
            "reasons": []
        }

    # ---------------------------------------------------------------
    # Calculate final confidence
    # ---------------------------------------------------------------
    #
    # The signature has a base confidence.
    #
    # Observed matching evidence increases it.
    #
    # Never allow confidence above 100%.
    #

    final_score = min(
        100,
        base_confidence + (score // 2)
    )

    return {
        "matched": True,
        "score": final_score,
        "reasons": reasons
    }


# -------------------------------------------------------------------
# Observation handling
# -------------------------------------------------------------------

def identify_observation(db, observation):
    """
    Compare one stored BLE observation against every enabled
    signature.

    Returns a list of matching signatures ordered by confidence.
    """

    (
        address,
        rssi,
        service_uuids_json,
        manufacturer_data_json,
        service_data_json,
    ) = observation

    # Convert JSON strings stored in SQLite back into Python
    # objects.

    try:

        service_uuids = json.loads(
            service_uuids_json or "[]"
        )

    except (json.JSONDecodeError, TypeError):

        service_uuids = []

    try:

        manufacturer_data = json.loads(
            manufacturer_data_json or "{}"
        )

    except (json.JSONDecodeError, TypeError):

        manufacturer_data = {}

    try:

        service_data = json.loads(
            service_data_json or "{}"
        )

    except (json.JSONDecodeError, TypeError):

        service_data = {}

    # Load all enabled signatures.

    signatures = load_signatures(
        db
    )

    matches = []

    # Test every signature against this observation.

    for signature in signatures:

        result = matches_signature(
            signature,
            manufacturer_data,
            service_uuids,
            service_data,
            rssi
        )

        if result["matched"]:

            matches.append({
                "signature_id": signature[0],
                "name": signature[1],
                "device_type": signature[2],
                "description": signature[3],
                "score": result["score"],
                "reasons": result["reasons"],
            })

    # Highest confidence first.

    matches.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    return matches


# -------------------------------------------------------------------
# Test recent observations
# -------------------------------------------------------------------

def test_recent_observations(db):
    """
    Run the matcher against the most recent BLE observations.

    This is a development/testing function.

    The MAC address is displayed locally on the Pi. Nothing is
    transmitted anywhere by this program.
    """

    rows = db.execute("""
        SELECT
            address,
            rssi,
            service_uuids,
            manufacturer_data,
            service_data
        FROM ble_observations
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()

    if not rows:

        print()
        print(
            "No BLE observations found."
        )

        return

    match_count = 0

    for row in rows:

        matches = identify_observation(
            db,
            row
        )

        if not matches:

            continue

        match_count += 1

        print()
        print("=" * 60)

        print(
            f"Device: {row[0]}"
        )

        print(
            f"RSSI:   {row[1]}"
        )

        print()

        print(
            "MATCHES:"
        )

        for match in matches:

            print(
                f"  {match['device_type']} "
                f"({match['name']}) "
                f"confidence={match['score']}%"
            )

            if match["description"]:

                print(
                    f"    {match['description']}"
                )

            print(
                "    Evidence:"
            )

            for reason in match["reasons"]:

                print(
                    f"      - {reason}"
                )

    print()

    print("=" * 60)

    print(
        f"Observations with matches: {match_count}"
    )

    print("=" * 60)


# -------------------------------------------------------------------
# Main program
# -------------------------------------------------------------------

def main():
    """
    Program entry point.
    """

    print(
        "BLE Signature Matcher"
    )

    print(
        "---------------------"
    )

    print(
        f"Database: {DB_PATH}"
    )

    # Open the existing RF monitor database.

    db = sqlite3.connect(
        DB_PATH
    )

    try:

        # Count currently enabled signatures.

        signature_count = db.execute("""
            SELECT COUNT(*)
            FROM device_signatures
            WHERE enabled = 1
        """).fetchone()[0]

        print(
            f"Enabled signatures: {signature_count}"
        )

        # Nothing to match yet.

        if signature_count == 0:

            print()

            print(
                "No signatures are defined yet."
            )

            print(
                "The matcher is ready, but there is "
                "nothing to match against."
            )

            return

        # Run the test against recent observations.

        test_recent_observations(
            db
        )

    finally:

        # Always close the database cleanly.

        db.close()


# -------------------------------------------------------------------
# Standard Python entry point
# -------------------------------------------------------------------

if __name__ == "__main__":

    main()

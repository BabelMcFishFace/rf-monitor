# RF Monitor — Development Notes

## Project

Repository:

    https://github.com/bablefish42/rf-monitor

Local project:

    ~/rf-monitor

Main branch:

    main

Run the application from the project root with:

    cd ~/rf-monitor
    source .venv/bin/activate
    python -m web.app

Do NOT run:

    python web/app.py

because the package imports expect the project root to be on the Python module path.

---

## Current development state

The manufacturer identification system has been extended to support custom Bluetooth Company IDs.

The purpose is to allow the user to manually identify devices where the official Bluetooth Company ID database does not contain the manufacturer ID.

Custom mappings are stored in the local SQLite database and are managed from the Settings page.

---

## Required lookup order

The required lookup order is:

1. Official/local Bluetooth Company ID database
2. Custom Company ID database
3. Existing IEEE MAC/OUI lookup where applicable
4. Optional internet lookup
5. Unknown

Official Company ID results MUST take priority over custom entries.

Example:

    Official:
        12345 -> Acme Corporation

    Custom:
        12345 -> My Custom Device

Result:

    Acme Corporation

If the official database does not contain the ID:

    Official:
        56789 -> not found

    Custom:
        56789 -> My Known Device

Result:

    My Known Device

---

## Files changed

The current custom Company ID work involves:

    web/app.py
    web/manufacturer_lookup.py
    web/templates/settings.html

Temporary backup files were also created:

    web/app.py.backup_20261006_160752
    web/manufacturer_lookup.py.backup_20261006_160752
    web/templates/settings.html.backup_20261006_160752

The backup files should NOT normally be committed.

---

## SQLite custom Company ID table

web/app.py creates this table automatically:

    custom_company_ids

Schema:

    company_id INTEGER PRIMARY KEY
    company_name TEXT NOT NULL
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

The table is created from ensure_database_schema() using CREATE TABLE IF NOT EXISTS.

This means existing installations should continue to work without a manual database migration.

Custom mappings persist across application restarts.

---

## Manufacturer lookup

The main manufacturer lookup function is:

    identify_manufacturer(
        db,
        manufacturer_id=None,
        mac_address=None,
        allow_internet=True,
    )

The observation-level lookup is:

    get_manufacturers_for_observation(
        db,
        observation,
        allow_internet=False,
    )

The official Bluetooth Company ID lookup is:

    lookup_company_id(manufacturer_id)

The official database is:

    data/company_ids.json

When changing manufacturer identification, make sure the custom lookup is actually reached from the normal observation path.

The intended flow is:

    BLE observation
        |
        v
    get_manufacturers_for_observation()
        |
        v
    identify_manufacturer()
        |
        v
    official Bluetooth Company ID
        |
        +---- found ----> return official result
        |
        +---- not found
                  |
                  v
          custom Company ID
                  |
                  +---- found ----> return custom result
                  |
                  +---- not found
                              |
                              v
                       existing fallback logic

---

## Source names

Official Bluetooth Company ID results should continue to use:

    Local Bluetooth Company ID database

Custom mappings should use:

    Custom Company ID

This allows the dashboard to show where the manufacturer identification came from.

---

## Settings page

The Settings page is:

    web/templates/settings.html

It already contains:

- Bluetooth adapter configuration
- Offline manufacturer database controls
- Database download/update controls
- Offline lookup information

The Custom Company ID section should allow the user to:

- View existing custom mappings
- Add a Company ID
- Enter a human-readable manufacturer/device name
- Save the mapping
- Delete a mapping

The UI should clearly explain that official database results take priority over custom mappings.

---

## Existing manufacturer databases

Bluetooth Company IDs:

    data/company_ids.json

IEEE MA-L:

    data/ieee/ma-l.csv

IEEE MA-M:

    data/ieee/ma-m.csv

The application is designed to continue working offline using these local databases.

---

## Existing application database

The runtime SQLite database is:

    rf_monitor.db

It is deliberately not stored in Git.

The application automatically creates the required schema.

Existing important tables include:

    app_settings
    manufacturer_cache
    ble_observations
    ble_fingerprints

New table:

    custom_company_ids

---

## Running the application

Always run from the project root:

    cd ~/rf-monitor
    source .venv/bin/activate
    python -m web.app

Do not run:

    python web/app.py

The latter can produce:

    ModuleNotFoundError: No module named 'web'

because of the way the Python package imports are structured.

---

## Testing

After making changes:

    cd ~/rf-monitor
    source .venv/bin/activate
    python -m web.app

Open:

    http://RASPBERRY_PI_IP:5000/settings

Test adding a custom Company ID.

Then verify:

1. An official Company ID still displays the official name even if a custom entry exists for the same ID.
2. An ID missing from the official database can be identified using the custom mapping.
3. Deleting the custom mapping removes the custom identification.
4. Restarting the web application does not lose custom mappings.

---

## Git status at the last checkpoint

The last status showed:

    modified:   web/app.py
    modified:   web/manufacturer_lookup.py
    modified:   web/templates/settings.html

Untracked backup files:

    web/app.py.backup_20261006_160752
    web/manufacturer_lookup.py.backup_20261006_160752
    web/templates/settings.html.backup_20261006_160752

The changes had NOT yet been committed or pushed at that checkpoint.

---

## Before committing

Run:

    cd ~/rf-monitor

    git status

    git diff --check

    git diff --stat

Do not commit the backup files unless specifically required.

Recommended files to commit:

    web/app.py
    web/manufacturer_lookup.py
    web/templates/settings.html
    DEVELOPMENT_NOTES.md

Suggested commit:

    git add web/app.py web/manufacturer_lookup.py web/templates/settings.html DEVELOPMENT_NOTES.md

    git commit -m "Add custom Bluetooth Company ID mappings"

    git push origin main

---

## Important continuation note

If development continues in a new chat, read both:

    README.md
    DEVELOPMENT_NOTES.md

Then run:

    git status
    git diff

before making further changes.

The most important requirement is:

    OFFICIAL COMPANY ID LOOKUP FIRST
    CUSTOM COMPANY ID LOOKUP SECOND

Custom entries must never override a valid official Company ID result.

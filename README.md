# RF Monitor

RF Monitor is a Raspberry Pi Bluetooth Low Energy monitoring application with a Flask web dashboard.

## Features

- Bluetooth Low Energy device discovery
- Device history stored in SQLite
- Manufacturer identification
- Offline manufacturer databases
- Device detail pages
- Device deletion
- Collector start and stop controls
- Manufacturer database update controls
- Web dashboard

## Requirements

- Raspberry Pi
- Bluetooth hardware
- Raspberry Pi OS / Linux
- Python 3
- Git
- Bluetooth / BlueZ

The current development hardware has been tested successfully with Python 3.13.

## Fresh installation

Clone the repository:

    cd ~
    git clone https://github.com/bablefish42/rf-monitor.git
    cd rf-monitor

Install the required system packages:

    sudo apt update
    sudo apt install -y python3 python3-venv python3-pip bluetooth bluez

Create the Python virtual environment:

    python3 -m venv .venv

Activate it:

    source .venv/bin/activate

Upgrade pip:

    python -m pip install --upgrade pip

Install the Python dependencies:

    pip install -r requirements.txt

## Start the web dashboard

From the project directory:

    cd ~/rf-monitor
    source .venv/bin/activate
    python -m web.app

The dashboard runs on port 5000.

From another computer on the same network, open:

    http://RASPBERRY_PI_IP:5000

Replace RASPBERRY_PI_IP with the IP address of the Raspberry Pi.

For example:

    http://192.168.1.100:5000

## Collector

The BLE collector is controlled from the web dashboard.

Open the dashboard and use the Collector controls to start and stop collection.

The collector does not need to be running for the web dashboard itself to work.

## Manufacturer databases

The repository contains the offline manufacturer databases:

    data/company_ids.json
    data/ieee/ma-l.csv
    data/ieee/ma-m.csv

These are used to identify Bluetooth device manufacturers.

The Settings page provides controls for updating the manufacturer databases.

## Database

The application uses SQLite.

The runtime database is:

    rf_monitor.db

The database is intentionally NOT included in the Git repository.

A new installation therefore starts with a fresh database.

This means that device data from one Raspberry Pi is not automatically transferred to another installation.

## Project structure

    rf-monitor/
    ├── ble_collector.py
    ├── signature_matcher.py
    ├── requirements.txt
    ├── data/
    │   ├── company_ids.json
    │   └── ieee/
    │       ├── ma-l.csv
    │       └── ma-m.csv
    └── web/
        ├── app.py
        ├── config.py
        ├── database_manager.py
        ├── manufacturer_lookup.py
        ├── offline_database.py
        ├── static/
        │   └── style.css
        └── templates/
            ├── device.html
            ├── index.html
            └── settings.html

## What is not stored in Git

The following are deliberately excluded from the repository:

    .venv/
    __pycache__/
    *.pyc
    *.db-journal
    *.log
    rf_monitor.db

The virtual environment is recreated during installation.

The SQLite database is created locally on each installation.

## Running the application

Each time you want to run RF Monitor:

    cd ~/rf-monitor
    source .venv/bin/activate
    python -m web.app

Then open the dashboard from another computer.

## Current status

The application has been tested on Raspberry Pi hardware.

The following have been verified:

- Web dashboard loads
- API endpoints respond
- Settings page loads
- Manufacturer databases can be updated
- Device detail pages load
- Devices can be deleted
- Collector can be started
- Collector can be stopped

## Development server

The application currently uses the Flask development server.

This is suitable for development and use on a trusted local network.

A production web-server configuration can be added later if required.

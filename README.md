## OctoPrint-BambuPrinter

![Plugin Logo](https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/logo/logo.png)

This plugin is an attempt to connect BambuLab printers to OctoPrint. It's still a work in progress, and there may be bugs/quirks that you will have to work around while using the plugin and during development. 

## About This Repository

This repository hosts OctoPrint-BambuPrinter, a plugin designed to bridge the gap between OctoPrint and Bambu Lab 3D printers. [OctoPrint](https://octoprint.org) is a powerful web-based interface used primarily on Raspberry Pi mini-PCs to control and monitor 3D printers. It provides remote access, G-code streaming, webcam monitoring, plugin support, and more—making it a popular choice for makers and 3D printing enthusiasts looking to enhance the capabilities of their printers.

## What This Plugin Does

OctoPrint-BambuPrinter adds native-like support for Bambu Lab printers within the OctoPrint ecosystem. Bambu Lab printers, such as the X1 Carbon and P1P series, are known for their closed ecosystem and advanced multi-material capabilities via the AMS (Automatic Material System). This plugin aims to open up those devices to greater interoperability, offering OctoPrint users features such as:

Real-time Status Monitoring: Track current print progress, temperatures, printer states, and more directly within the OctoPrint interface.

Basic Remote Control: Pause, resume, and cancel prints remotely.


Network Integration: Communicate with the Bambu Lab printer over the local network using its known endpoints and messaging format.



## Why This Matters

Bambu Lab printers are extremely capable machines but are limited in third-party integration. This plugin brings the flexibility and openness of OctoPrint to the Bambu ecosystem, allowing makers to:

Combine multiple printers (including Bambu and non-Bambu) under one interface.

Integrate with webcam monitoring, plugins like OctoLapse, or custom dashboards.

Log and track print jobs over time with OctoPrint’s robust history and analytics tools.


## Technical Highlights

Built with the OctoPrint plugin architecture

Communicates with the Bambu Lab printer via its WebSocket and HTTP APIs

Uses threads to poll status updates without blocking OctoPrint’s main thread

Structured for modularity and future expansions like AMS control, print queuing, and multi-printer management






## System Requirements

* Python 3.9 or higher (OctoPi 1.0.0)

## Setup

Install manually using this URL:

    https://github.com/jneilliii/OctoPrint-BambuPrinter/archive/master.zip

## Go to BambuONEclickADDon&CAM folder

You need to set up the script with your information for bambucam.py

Bambu lan access code

Bambu printer IP

install a python environment at the location of /home/$USER/bambucam/bin/activate
## Install Python environment 
```sh
sudo apt update
sudo apt install python3-venv
```

```sh
python3 -m venv /home/$USER/bambucam
```

## It will appear like this

```sh
/home/$USER/bambucam/
├── bin/
│   ├── activate
│   ├── python
│   └── pip
└── lib/
    └── pythonX.Y/
```

## To activate manually 

```sh
# Activate environment
source ~/bambucam/bin/activate

# Install core dependencies
pip install --upgrade pip
pip install flask opencv-python requests watchdog

# Deactivate when finished
deactivate
```
## Set up paths
```sh
VENV_PATH = "/home/your_username/bambucam/bin/activate"  # Replace your_username
SCRIPT_PATH = "/home/your_username/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/bambucam.py"  # Set your actual script path
```
## Test the env activation and install requirements 

```sh
source ~/bambucam/bin/activate
pip install -r requirements-bambucam.txt
pip install -r requirements-watchdog.txt
```
## Start up script for BambuCAM+watchdog
```sh
# Make script executable
chmod +x auto-bambu-cam-and-watchdog.sh

# Start the service
./auto-bambu-cam-and-watchdog.sh
```
## Running working-watchdog.py at start-up 


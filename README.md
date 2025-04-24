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

    https://github.com/CodeMasterCody3D/OctoPrint-BambuPrinter/archive/refs/heads/rc.zip

## Go to BambuONEclickADDon&CAM folder

You need to set up the script with your information for bambucam.py

Bambu lan access code

Bambu printer IP

install a python environment at the location of /home/$USER/bambucam/bin/activate
## Install Python environment 
## Ubuntu/Debian/Octopi

```sh
curl -sSL "https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/BambuONEclickADDon%26CAM/install_bambucam_env.sh" -o install_bambucam_env.sh && chmod +x install_bambucam_env.sh && ./install_bambucam_env.sh
```

## Octoprint Docker

```sh
curl -sSL "https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/BambuONEclickADDon%26CAM/install_bambucam_env_docker.sh" -o install_bambucam_env_docker.sh && chmod +x install_bambucam_env_docker.sh && ./install_bambucam_env_docker.sh
```

## To activate manually 

```sh
# Activate environment
source ~/bambucam/bin/activate

# Deactivate when finished
deactivate
```

##  Install python requirements 

```sh
source ~/bambucam/bin/activate && curl -sSL "https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/BambuONEclickADDon%26CAM/requirements-bambucam.txt" -o requirements.txt && pip install -r requirements.txt
```
## Download script for bambucam.py
```sh
mkdir -p "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM" && curl -sSL "https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/BambuONEclickADDon%26CAM/bambucam.py" -o "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/bambucam.py"

# Start the service
./auto-bambu-cam-and-watchdog.sh
```
## Download auto-bambu-cam.sh script

```sh
mkdir -p "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM" && curl -sSL "https://raw.githubusercontent.com/CodeMasterCody3D/OctoPrint-BambuPrinter/refs/heads/rc/BambuONEclickADDon%26CAM/auto-bambu-cam.sh" -o "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/auto-bambu-cam.sh" && chmod +x "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/auto-bambu-cam.sh"
```


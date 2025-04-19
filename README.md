# OctoPrint-BambuPrinter

This plugin is an attempt to connect BambuLab printers to OctoPrint. It's still a work in progress, and there may be bugs/quirks that you will have to work around while using the plugin and during development. 

## System Requirements

* Python 3.9 or higher (OctoPi 1.0.0)

## Setup

Install manually using this URL:

    https://github.com/jneilliii/OctoPrint-BambuPrinter/archive/master.zip

## I plan to make a config file soon for easier set up.
## Ensure your putting working-watchdog.py, bambucam.py and auto-bambu-cam.sh to be in yiur home directory for east access. 

You need to set up the scripts with your information. 

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
SCRIPT_PATH = "/path/to/your/bambucam.py"  # Set your actual script path
```
## Test the env activation 

```sh
source ~/bambucam/bin/activate
python --version  # Should show Python 3.x
which pip        # Should show path to your venv
deactivate
```
## Start up script for BambuCAM
```sh
# Make script executable
chmod +x auto-bambu-cam.sh

# Start the service
./auto-bambu-cam.sh
```


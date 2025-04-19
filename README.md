# OctoPrint-BambuPrinter

This plugin is an attempt to connect BambuLab printers to OctoPrint. It's still a work in progress, and there may be bugs/quirks that you will have to work around while using the plugin and during development. 

## System Requirements

* Python 3.9 or higher (OctoPi 1.0.0)

## Setup

Install manually using this URL:

    https://github.com/jneilliii/OctoPrint-BambuPrinter/archive/master.zip


You need to set up the scripts with your information.

Bambu lan access code
Bambu printer IP
install a python environment at the location of /home/$USER/bambucam/bin/activate
#Install Python environment 
```sh
sudo apt update
sudo apt install python3-venv
```

```sh
python3 -m venv /home/$USER/bambucam
```

#It will appear like this

```sh
/home/$USER/bambucam/
├── bin/
│   ├── activate
│   ├── python
│   └── pip
└── lib/
    └── pythonX.Y/
```

#To activate manually 

```sh
source /home/$USER/bambucam/bin/activate
```




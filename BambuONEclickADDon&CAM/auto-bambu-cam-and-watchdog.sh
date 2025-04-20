#!/bin/bash

source "$HOME/bambucam/bin/activate"
python "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/bambucam.py" &
python "$HOME/OctoPrint-BambuPrinter/BambuONEclickADDon&CAM/working-watchdog.py" &

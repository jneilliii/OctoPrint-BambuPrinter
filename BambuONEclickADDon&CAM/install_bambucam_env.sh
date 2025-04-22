#!/bin/bash

# Update package lists and install python3-venv
sudo apt update
sudo apt install -y python3-venv

# Create directory if it doesn't exist
mkdir -p "$HOME/bambucam"

# Create Python virtual environment
python3 -m venv "$HOME/bambucam"

# Output success message
echo "Python environment created at: $HOME/bambucam"
echo "To activate it, run:"
echo "source \$HOME/bambucam/bin/activate"

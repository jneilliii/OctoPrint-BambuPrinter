from __future__ import annotations
from unittest.mock import MagicMock

import octoprint.settings

from octoprint_bambu_printer.bambu_print_plugin import BambuPrintPlugin
from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
from octoprint_bambu_printer.printer.states.idle_state import IdleState
from octoprint_bambu_printer.printer.states.paused_state import PausedState
from octoprint_bambu_printer.printer.states.print_finished_state import (
    PrintFinishedState,
)
from octoprint_bambu_printer.printer.states.printing_state import PrintingState
from pytest import fixture


@fixture
def plugin():
    plugin = BambuPrintPlugin()
    plugin._settings = MagicMock()
    plugin._settings.get(["serial"]).return_value = "login"
    plugin._settings.get(["access_code"]).return_value = "token"
    plugin._settings.get(["host"]).return_value = "192.168.0.20"
    plugin._settings.get_plugin_logfile_path(["host"]).return_value = "./test_log.log"
    return plugin


@fixture
def printer(plugin):
    printer = plugin.virtual_printer_factory(None, "BAMBU", 115200, 5)
    assert printer is not None
    return printer


def test_initial_state(printer: BambuVirtualPrinter):
    assert isinstance(printer.current_state, IdleState)


def test_list_sd_card(printer: BambuVirtualPrinter):
    printer.write(b"M20\n")  # GCode for listing SD card
    result = printer.readline()
    assert result == ""  # Replace with the actual expected result


def test_start_print(printer: BambuVirtualPrinter):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    result = printer.readline()
    assert isinstance(printer.current_state, PrintingState)


def test_pause_print(printer: BambuVirtualPrinter):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    printer.write(b"M25\n")  # GCode for pausing the print
    result = printer.readline()
    assert isinstance(printer.current_state, PausedState)


def test_get_printing_info(printer: BambuVirtualPrinter):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    printer.write(b"M27\n")  # GCode for getting printing info
    result = printer.readline()
    assert result == ""


def test_abort_print(printer: BambuVirtualPrinter):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    printer.write(b"M26\n")  # GCode for aborting the print
    result = printer.readline()
    assert isinstance(printer.current_state, IdleState)


def test_print_finished(printer: BambuVirtualPrinter):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    result = printer.readline()
    assert isinstance(printer.current_state, PrintFinishedState)

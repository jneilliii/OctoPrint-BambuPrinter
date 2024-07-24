from __future__ import annotations
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock
import unittest.mock

from octoprint_bambu_printer.bambu_print_plugin import BambuPrintPlugin
from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
from octoprint_bambu_printer.printer.remote_sd_card_file_list import FileInfo
from octoprint_bambu_printer.printer.states.idle_state import IdleState
from octoprint_bambu_printer.printer.states.paused_state import PausedState
from octoprint_bambu_printer.printer.states.print_finished_state import (
    PrintFinishedState,
)
from octoprint_bambu_printer.printer.states.printing_state import PrintingState
from pytest import fixture


@fixture
def output_test_folder(output_folder: Path):
    folder = output_folder / "test_gcode"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@fixture
def log_test():
    return logging.getLogger("gcode_unittest")


class DictGetter:
    def __init__(self, options: dict) -> None:
        self._options = options

    def __call__(self, key: str | list[str]):
        if isinstance(key, list):
            key = "_".join(key)
        return self._options.get(key, None)


@fixture
def settings(output_test_folder):
    _settings = MagicMock()
    _settings.get.side_effect = DictGetter(
        {
            "serial": "BAMBU",
            "host": "localhost",
            "access_code": "12345",
        }
    )
    _settings.get_boolean.side_effect = DictGetter({"forceChecksum": False})

    log_file_path = output_test_folder / "log.txt"
    log_file_path.touch()
    _settings.get_plugin_logfile_path.return_value = log_file_path.as_posix()
    return _settings


@fixture
def profile_manager():
    _profile_manager = MagicMock()
    _profile_manager.get_current.side_effect = MagicMock()
    _profile_manager.get_current().get.side_effect = DictGetter(
        {
            "heatedChamber": False,
        }
    )
    return _profile_manager


@fixture
def files_info_ftp():
    def _f_date(dt: datetime):
        return dt.replace(tzinfo=timezone.utc).strftime("%Y%m%d%H%M%S")

    return {
        "print.3mf": (1000, _f_date(datetime(2024, 5, 6))),
        "print2.3mf": (1200, _f_date(datetime(2024, 5, 7))),
    }


@fixture
def ftps_session_mock(files_info_ftp):
    with unittest.mock.patch(
        "octoprint_bambu_printer.printer.ftpsclient.ftpsclient.IoTFTPSClient"
    ) as ftps_client:
        ftps_session = MagicMock()
        ftps_session.size.side_effect = DictGetter(
            {file: info[0] for file, info in files_info_ftp.items()}
        )
        ftps_session.sendcmd.side_effect = DictGetter(
            {f"MDTM {file}": info[1] for file, info in files_info_ftp.items()}
        )

        all_files = list(files_info_ftp.keys())
        ftps_client.list_files.side_effect = DictGetter(
            {
                ("", ".3mf"): all_files,
                ("cache/", ".3mf"): [f"cache/{file}" for file in all_files],
            }
        )
        ftps_client.ftps_session = ftps_session
        yield


@fixture
def printer(output_test_folder, settings, profile_manager, log_test, ftps_session_mock):
    async def _mock_connection(self):
        pass

    BambuVirtualPrinter._create_connection_async = _mock_connection
    serial_obj = BambuVirtualPrinter(
        settings,
        profile_manager,
        data_folder=output_test_folder,
        serial_log_handler=log_test,
        read_timeout=5.0,
        faked_baudrate=115200,
    )
    serial_obj._bambu_client = MagicMock()
    return serial_obj


def test_initial_state(printer: BambuVirtualPrinter):
    assert isinstance(printer.current_state, IdleState)


def test_list_sd_card(printer: BambuVirtualPrinter):
    printer.write(b"M20\n")  # GCode for listing SD card
    time.sleep(0.1)
    result = printer.readlines()
    assert result == ""  # Replace with the actual expected result


def test_start_print(printer: BambuVirtualPrinter):
    printer.write(b"M\n")
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

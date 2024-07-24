from __future__ import annotations
from collections.abc import Callable
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any
import unittest
from unittest.mock import MagicMock, Mock
import unittest.mock

import pybambu
import pybambu.commands
from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
from octoprint_bambu_printer.printer.remote_sd_card_file_list import (
    RemoteSDCardFileList,
)
from octoprint_bambu_printer.printer.states.idle_state import IdleState
from octoprint_bambu_printer.printer.states.paused_state import PausedState
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
        self._options: dict[str | tuple[str, ...], Any] = options

    def __call__(self, key: str | list[str] | tuple[str, ...]):
        if isinstance(key, list):
            key = tuple(key)
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
        "cache/print.3mf": (1200, _f_date(datetime(2024, 5, 7))),
        "cache/print2.3mf": (1200, _f_date(datetime(2024, 5, 7))),
    }


@fixture
def ftps_session_mock(files_info_ftp):
    with unittest.mock.patch(
        "octoprint_bambu_printer.printer.ftpsclient.ftpsclient.IoTFTPSClient"
    ) as ftps_client_mock:
        ftps_session = MagicMock()
        ftps_session.size.side_effect = DictGetter(
            {file: info[0] for file, info in files_info_ftp.items()}
        )

        ftps_session.sendcmd.side_effect = DictGetter(
            {f"MDTM {file}": info[1] for file, info in files_info_ftp.items()}
        )

        all_files = list(files_info_ftp.keys())
        file_registry = DictGetter(
            {
                ("", ".3mf"): list(
                    filter(lambda f: Path(f).parent == Path("."), all_files)
                ),
                ("cache/", ".3mf"): list(
                    map(
                        lambda f: Path(f).name,
                        filter(lambda f: Path(f).parent == Path("cache/"), all_files),
                    )
                ),
            }
        )
        ftps_client_mock.list_files.side_effect = lambda folder, ext: file_registry(
            (folder, ext)
        )
        ftps_client_mock.ftps_session = ftps_session
        RemoteSDCardFileList._connect_ftps_server = MagicMock(
            return_value=ftps_client_mock
        )
        yield


@fixture(scope="function")
def print_job_mock():
    print_job = MagicMock()
    print_job.subtask_name = ""
    print_job.print_percentage = 0
    return print_job


@fixture(scope="function")
def temperatures_mock():
    temperatures = MagicMock()
    temperatures.nozzle_temp = 0
    temperatures.target_nozzle_temp = 0
    temperatures.bed_temp = 0
    temperatures.target_bed_temp = 0
    temperatures.chamber_temp = 0
    return temperatures


@fixture(scope="function")
def bambu_client_mock(print_job_mock, temperatures_mock) -> pybambu.BambuClient:
    bambu_client = MagicMock()
    bambu_client.connected = True
    device_mock = MagicMock()
    device_mock.print_job = print_job_mock
    device_mock.temperatures = temperatures_mock
    bambu_client.get_device.return_value = device_mock
    return bambu_client


@fixture(scope="function")
def printer(
    output_test_folder,
    settings,
    profile_manager,
    log_test,
    ftps_session_mock,
    bambu_client_mock,
):
    async def _mock_connection(self):
        pass

    BambuVirtualPrinter._create_client_connection_async = _mock_connection
    printer_test = BambuVirtualPrinter(
        settings,
        profile_manager,
        data_folder=output_test_folder,
        serial_log_handler=log_test,
        read_timeout=0.01,
        faked_baudrate=115200,
    )
    printer_test._bambu_client = bambu_client_mock
    printer_test.flush()
    printer_test.readlines()
    yield printer_test
    printer_test.close()


def test_initial_state(printer: BambuVirtualPrinter):
    assert isinstance(printer.current_state, IdleState)


def test_list_sd_card(printer: BambuVirtualPrinter):
    printer.write(b"M20\n")  # GCode for listing SD card
    printer.flush()
    result = printer.readlines()
    assert result[0] == b"Begin file list"
    assert result[1].endswith(b'"print.3mf"')
    assert result[2].endswith(b'"print2.3mf"')
    assert result[3] == b"End file list"
    assert result[4] == b"ok"


def test_cannot_start_print_without_file(printer: BambuVirtualPrinter):
    printer.write(b"M24\n")
    printer.flush()
    result = printer.readlines()
    assert result[0] == b"ok"
    assert isinstance(printer.current_state, IdleState)


def test_non_existing_file_not_selected(printer: BambuVirtualPrinter):
    assert printer.file_system.selected_file is None

    printer.write(b"M23 non_existing.3mf\n")
    printer.flush()
    result = printer.readlines()
    assert result[-2] != b"File selected"
    assert result[-1] == b"ok"
    assert printer.file_system.selected_file is None


def test_print_started_with_selected_file(printer: BambuVirtualPrinter, print_job_mock):
    assert printer.file_system.selected_file is None

    printer.write(b"M20\n")
    printer.flush()
    printer.readlines()

    printer.write(b"M23 print.3mf\n")
    printer.flush()
    result = printer.readlines()
    assert result[-2] == b"File selected"
    assert result[-1] == b"ok"

    assert printer.file_system.selected_file is not None
    assert printer.file_system.selected_file.file_name == "print.3mf"

    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M24\n")
    printer.flush()

    result = printer.readlines()
    assert result[0] == b"ok"
    assert isinstance(printer.current_state, PrintingState)


def test_pause_print(printer: BambuVirtualPrinter, bambu_client_mock, print_job_mock):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M20\n")
    printer.write(b"M23 print.3mf\n")
    printer.write(b"M24\n")
    printer.flush()
    printer.readlines()
    assert isinstance(printer.current_state, PrintingState)

    bambu_client_mock.publish.return_value = True
    printer.write(b"M25\n")  # GCode for pausing the print
    printer.flush()
    result = printer.readlines()
    assert result[0] == b"ok"
    assert isinstance(printer.current_state, PausedState)


def test_events_update_printer_state(printer: BambuVirtualPrinter, print_job_mock):
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PrintingState)

    print_job_mock.gcode_state = "PAUSE"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PausedState)

    print_job_mock.gcode_state = "IDLE"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, IdleState)

    print_job_mock.gcode_state = "FINISH"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, IdleState)

    print_job_mock.gcode_state = "FAILED"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, IdleState)


def test_printer_info_check(printer: BambuVirtualPrinter):
    printer.write(b"M27\n")  # printer get info
    printer.flush()

    result = printer.readlines()
    assert result[-1] == b"ok"
    assert isinstance(printer.current_state, IdleState)


def test_abort_print(printer: BambuVirtualPrinter):
    printer.write(b"M26\n")  # GCode for aborting the print
    printer.flush()

    result = printer.readlines()
    assert result[-1] == b"ok"
    assert isinstance(printer.current_state, IdleState)


def test_regular_move(printer: BambuVirtualPrinter, bambu_client_mock):
    gcode = b"G28\nG1 X10 Y10\n"
    printer.write(gcode)
    printer.flush()
    result = printer.readlines()
    assert result[-1] == b"ok"

    gcode_command = pybambu.commands.SEND_GCODE_TEMPLATE
    gcode_command["print"]["param"] = "G28\n"
    bambu_client_mock.publish.assert_called_with(gcode_command)

    gcode_command["print"]["param"] = "G1 X10 Y10\n"
    bambu_client_mock.publish.assert_called_with(gcode_command)

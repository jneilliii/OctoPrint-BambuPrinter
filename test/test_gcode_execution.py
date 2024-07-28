from __future__ import annotations
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from octoprint_bambu_printer.printer.file_system.cached_file_view import CachedFileView
import pybambu
import pybambu.commands
from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
from octoprint_bambu_printer.printer.file_system.file_info import FileInfo
from octoprint_bambu_printer.printer.file_system.ftps_client import IoTFTPSClient
from octoprint_bambu_printer.printer.file_system.remote_sd_card_file_list import (
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
    def __init__(self, options: dict, default_value=None) -> None:
        self.options: dict[str | tuple[str, ...], Any] = options
        self._default_value = default_value

    def __call__(self, key: str | list[str] | tuple[str, ...]):
        if isinstance(key, list):
            key = tuple(key)
        return self.options.get(key, self._default_value)


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


def _ftp_date_format(dt: datetime):
    return dt.replace(tzinfo=timezone.utc).strftime("%Y%m%d%H%M%S")


@fixture
def project_files_info_ftp():
    return {
        "print.3mf": (1000, _ftp_date_format(datetime(2024, 5, 6))),
        "print2.3mf": (1200, _ftp_date_format(datetime(2024, 5, 7))),
    }


@fixture
def cache_files_info_ftp():
    return {
        "cache/print.3mf": (1200, _ftp_date_format(datetime(2024, 5, 7))),
        "cache/print2.3mf": (1200, _ftp_date_format(datetime(2024, 5, 7))),
    }


@fixture
def ftps_session_mock(project_files_info_ftp, cache_files_info_ftp):
    all_file_info = dict(**project_files_info_ftp, **cache_files_info_ftp)
    ftps_session = MagicMock()
    ftps_session.size.side_effect = DictGetter(
        {file: info[0] for file, info in all_file_info.items()}
    )

    ftps_session.sendcmd.side_effect = DictGetter(
        {f"MDTM {file}": info[1] for file, info in all_file_info.items()}
    )

    ftps_session.nlst.side_effect = DictGetter(
        {
            "": list(map(lambda p: Path(p).name, project_files_info_ftp))
            + ["Mock folder"],
            "cache/": list(map(lambda p: Path(p).name, cache_files_info_ftp))
            + ["Mock folder"],
            "timelapse/": ["video.mp4", "video.avi"],
        }
    )
    IoTFTPSClient.open_ftps_session = MagicMock(return_value=ftps_session)
    yield ftps_session


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


def test_list_ftp_paths_p1s(settings, ftps_session_mock):
    file_system = RemoteSDCardFileList(settings)
    file_view = CachedFileView(file_system).with_filter("timelapse/", ".avi")

    timelapse_files = ["timelapse/video.avi", "timelapse/video2.avi"]
    ftps_session_mock.size.side_effect = DictGetter(
        {file: 100 for file in timelapse_files}
    )
    ftps_session_mock.sendcmd.side_effect = DictGetter(
        {
            f"MDTM {file}": _ftp_date_format(datetime(2024, 5, 7))
            for file in timelapse_files
        }
    )
    ftps_session_mock.nlst.side_effect = DictGetter(
        {"timelapse/": [Path(f).name for f in timelapse_files]}
    )

    timelapse_paths = list(map(Path, timelapse_files))
    result_files = file_view.get_all_info()
    assert len(timelapse_files) == len(result_files) and all(
        file_info.path in timelapse_paths for file_info in result_files
    )


def test_list_ftp_paths_x1(settings, ftps_session_mock):
    file_system = RemoteSDCardFileList(settings)
    file_view = CachedFileView(file_system).with_filter("timelapse/", ".mp4")

    timelapse_files = ["timelapse/video.mp4", "timelapse/video2.mp4"]
    ftps_session_mock.size.side_effect = DictGetter(
        {file: 100 for file in timelapse_files}
    )
    ftps_session_mock.sendcmd.side_effect = DictGetter(
        {
            f"MDTM {file}": _ftp_date_format(datetime(2024, 5, 7))
            for file in timelapse_files
        }
    )
    ftps_session_mock.nlst.side_effect = DictGetter({"timelapse/": timelapse_files})

    timelapse_paths = list(map(Path, timelapse_files))
    result_files = file_view.get_all_info()
    assert len(timelapse_files) == len(result_files) and all(
        file_info.path in timelapse_paths for file_info in result_files
    )


def test_cannot_start_print_without_file(printer: BambuVirtualPrinter):
    printer.write(b"M24\n")
    printer.flush()
    result = printer.readlines()
    assert result[0] == b"ok"
    assert isinstance(printer.current_state, IdleState)


def test_non_existing_file_not_selected(printer: BambuVirtualPrinter):
    assert printer.selected_file is None

    printer.write(b"M23 non_existing.3mf\n")
    printer.flush()
    result = printer.readlines()
    assert result[-2] != b"File selected"
    assert result[-1] == b"ok"
    assert printer.selected_file is None


def test_print_started_with_selected_file(printer: BambuVirtualPrinter, print_job_mock):
    assert printer.selected_file is None

    printer.write(b"M20\n")
    printer.flush()
    printer.readlines()

    printer.write(b"M23 print.3mf\n")
    printer.flush()
    result = printer.readlines()
    assert result[-2] == b"File selected"
    assert result[-1] == b"ok"

    assert printer.selected_file is not None
    assert printer.selected_file.file_name == "print.3mf"

    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M24\n")
    printer.flush()
    result = printer.readlines()
    assert result[-1] == b"ok"

    # emulate printer reporting it's status
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PrintingState)


def test_pause_print(printer: BambuVirtualPrinter, bambu_client_mock, print_job_mock):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M20\n")
    printer.write(b"M23 print.3mf\n")
    printer.write(b"M24\n")
    printer.flush()

    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PrintingState)

    printer.write(b"M25\n")  # pausing the print
    printer.flush()
    result = printer.readlines()
    assert result[-1] == b"ok"

    print_job_mock.gcode_state = "PAUSE"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PausedState)
    bambu_client_mock.publish.assert_called_with(pybambu.commands.PAUSE)


def test_events_update_printer_state(printer: BambuVirtualPrinter, print_job_mock):
    print_job_mock.subtask_name = "print.3mf"
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


def test_abort_print_during_printing(printer: BambuVirtualPrinter, print_job_mock):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M20\nM23 print.3mf\nM24\n")
    printer.flush()
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    printer.readlines()
    assert isinstance(printer.current_state, PrintingState)

    printer.write(b"M26 S0\n")
    printer.flush()
    result = printer.readlines()
    assert result[-1] == b"ok"
    assert isinstance(printer.current_state, IdleState)


def test_abort_print_during_pause(printer: BambuVirtualPrinter, print_job_mock):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M20\nM23 print.3mf\nM24\n")
    printer.flush()
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()

    printer.write(b"M25\n")
    printer.flush()
    print_job_mock.gcode_state = "PAUSE"
    printer.new_update("event_printer_data_update")
    printer.flush()

    printer.readlines()
    assert isinstance(printer.current_state, PausedState)

    printer.write(b"M26 S0\n")
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


def test_file_selection_does_not_affect_current_print(
    printer: BambuVirtualPrinter, print_job_mock
):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M23 print.3mf\nM24\n")
    printer.flush()
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PrintingState)
    assert printer.current_print_job is not None
    assert printer.current_print_job.file_info.file_name == "print.3mf"
    assert printer.current_print_job.progress == 0

    printer.write(b"M23 print2.3mf\n")
    printer.flush()
    assert printer.current_print_job is not None
    assert printer.current_print_job.file_info.file_name == "print.3mf"
    assert printer.current_print_job.progress == 0


def test_finished_print_job_reset_after_new_file_selected(
    printer: BambuVirtualPrinter, print_job_mock
):
    print_job_mock.subtask_name = "print.3mf"

    printer.write(b"M23 print.3mf\nM24\n")
    printer.flush()
    print_job_mock.gcode_state = "RUNNING"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, PrintingState)
    assert printer.current_print_job is not None
    assert printer.current_print_job.file_info.file_name == "print.3mf"
    assert printer.current_print_job.progress == 0

    print_job_mock.print_percentage = 100
    printer.current_state.update_print_job_info()
    assert isinstance(printer.current_state, PrintingState)
    assert printer.current_print_job.progress == 100

    print_job_mock.gcode_state = "FINISH"
    printer.new_update("event_printer_data_update")
    printer.flush()
    assert isinstance(printer.current_state, IdleState)
    assert printer.current_print_job is None
    assert printer.selected_file is not None
    assert printer.selected_file.file_name == "print.3mf"

    printer.write(b"M23 print2.3mf\n")
    printer.flush()
    assert printer.current_print_job is None
    assert printer.selected_file is not None
    assert printer.selected_file.file_name == "print2.3mf"

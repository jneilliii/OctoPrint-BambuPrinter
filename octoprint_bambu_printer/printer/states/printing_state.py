from __future__ import annotations

import threading

import pybambu
import pybambu.models
import pybambu.commands

from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
from octoprint_bambu_printer.printer.print_job import PrintJob
from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class PrintingState(APrinterState):

    def __init__(self, printer: BambuVirtualPrinter) -> None:
        super().__init__(printer)
        self._printingLock = threading.Event()
        self._print_job: PrintJob | None = None
        self._sd_printing_thread = None

    @property
    def print_job(self):
        return self._print_job

    def init(self):
        if not self._printingLock.is_set():
            self._printingLock.set()

    def finalize(self):
        if self._printingLock.is_set():
            self._printingLock.clear()

    def _start_worker_thread(self, from_printer: bool = False):
        if self._sd_printing_thread is None:
            self._sdPrinting = True
            self._sdPrintStarting = True
            self._sd_printing_thread = threading.Thread(
                target=self._printing_worker, kwargs={"from_printer": from_printer}
            )
            self._sd_printing_thread.start()

    def set_print_job_info(self, print_job_info):
        filename: str = print_job_info.get("subtask_name")
        project_file_info = self._printer.file_system.search_by_stem(
            filename, [".3mf", ".gcode.3mf"]
        )
        if project_file_info is None:
            self._log.debug(f"No 3mf file found for {print_job_info}")
            return

        if self._printer.file_system.select_file(filename):
            self._printer.sendOk()
        self.start_new_print(from_printer=True)

        # fuzzy math here to get print percentage to match BambuStudio
        progress = print_job_info.get("print_percentage")
        self._print_job = PrintJob(project_file_info, 0)
        self._print_job.progress = 

    def start_new_print(self, from_printer: bool = False):
        if self._printer.file_system.selected_file is not None:
            self._start_worker_thread(from_printer)

        if self._sd_printing_thread is not None:
            if self._printer.bambu_client.connected:
                if self._printer.bambu_client.publish(pybambu.commands.RESUME):
                    self._log.info("print resumed")
                else:
                    self._log.info("print resume failed")
        return True

    def _printing_worker(self, from_printer: bool = False):
        try:
            if not from_printer and self._printer.bambu_client.connected:
                selected_file = self._printer.file_system.selected_file
                print_command = {
                    "print": {
                        "sequence_id": 0,
                        "command": "project_file",
                        "param": "Metadata/plate_1.gcode",
                        "md5": "",
                        "profile_id": "0",
                        "project_id": "0",
                        "subtask_id": "0",
                        "task_id": "0",
                        "subtask_name": f"{selected_file}",
                        "file": f"{selected_file}",
                        "url": (
                            f"file:///mnt/sdcard/{selected_file}"
                            if self._printer._settings.get_boolean(["device_type"])
                            in ["X1", "X1C"]
                            else f"file:///sdcard/{selected_file}"
                        ),
                        "timelapse": self._printer._settings.get_boolean(["timelapse"]),
                        "bed_leveling": self._printer._settings.get_boolean(["bed_leveling"]),
                        "flow_cali": self._printer._settings.get_boolean(["flow_cali"]),
                        "vibration_cali": self._printer._settings.get_boolean(
                            ["vibration_cali"]
                        ),
                        "layer_inspect": self._printer._settings.get_boolean(["layer_inspect"]),
                        "use_ams": self._printer._settings.get_boolean(["use_ams"]),
                    }
                }
                self._printer.bambu_client.publish(print_command)

            while self._selectedSdFilePos < self._selectedSdFileSize:
                if self._killed or not self._sdPrinting:
                    break

                # if we are paused, wait for resuming
                self._sdPrintingSemaphore.wait()
                self._reportSdStatus()
                time.sleep(3)
            self._log.debug(f"SD File Print: {self._selectedSdFile}")
        except AttributeError:
            if self.outgoing is not None:
                raise

        self._printer.change_state(self._printer._state_finished)

    def cancel(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.change_state(self._printer._state_finished)
                return True
            else:
                self._log.info("print cancel failed")
                return False
        return False

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octoprint_bambu_printer.printer.bambu_virtual_printer import (
        BambuVirtualPrinter,
    )

import threading

import pybambu
import pybambu.models
import pybambu.commands

from octoprint_bambu_printer.printer.print_job import PrintJob
from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class PrintingState(APrinterState):

    def __init__(self, printer: BambuVirtualPrinter) -> None:
        super().__init__(printer)
        self._current_print_job = None
        self._is_printing = False
        self._sd_printing_thread = None

    def init(self):
        self._is_printing = True
        self._printer.remove_project_selection()
        self.update_print_job_info()
        self._start_worker_thread()

    def finalize(self):
        if self._sd_printing_thread is not None and self._sd_printing_thread.is_alive():
            self._is_printing = False
            self._sd_printing_thread.join()
            self._sd_printing_thread = None
        self._printer.current_print_job = None

    def _start_worker_thread(self):
        if self._sd_printing_thread is None:
            self._is_printing = True
            self._sd_printing_thread = threading.Thread(target=self._printing_worker)
            self._sd_printing_thread.start()

    def _printing_worker(self):
        while (
            self._is_printing
            and self._printer.current_print_job is not None
            and self._printer.current_print_job.progress < 100
        ):
            self.update_print_job_info()
            self._printer.report_print_job_status()
            time.sleep(3)

        self.update_print_job_info()
        if (
            self._printer.current_print_job is not None
            and self._printer.current_print_job.progress >= 100
        ):
            self._printer.finalize_print_job()

    def update_print_job_info(self):
        print_job_info = self._printer.bambu_client.get_device().print_job
        task_name: str = print_job_info.subtask_name
        project_file_info = self._printer.project_files.get_file_by_stem(
            task_name, [".gcode", ".3mf"]
        )
        if project_file_info is None:
            self._log.debug(f"No 3mf file found for {print_job_info}")
            self._current_print_job = None
            self._printer.change_state(self._printer._state_idle)
            return

        progress = print_job_info.print_percentage
        self._printer.current_print_job = PrintJob(project_file_info, progress)
        self._printer.select_project_file(project_file_info.path.as_posix())

    def pause_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.PAUSE):
                self._log.info("print paused")
            else:
                self._log.info("print pause failed")

    def cancel_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.finalize_print_job()
            else:
                self._log.info("print cancel failed")

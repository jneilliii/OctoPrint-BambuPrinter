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
        self._printing_lock = threading.Event()
        self._print_job: PrintJob | None = None
        self._sd_printing_thread = None

    @property
    def print_job(self):
        return self._print_job

    def init(self):
        self._printing_lock.set()
        self.update_print_job_info()
        self._start_worker_thread()

    def finalize(self):
        self._printing_lock.clear()

        if self._sd_printing_thread is not None and self._sd_printing_thread.is_alive():
            self._sd_printing_thread.join()
            self._sd_printing_thread = None

    def _start_worker_thread(self):
        if self._sd_printing_thread is None:
            if not self._printing_lock.is_set():
                self._printing_lock.set()
            self._sd_printing_thread = threading.Thread(target=self._printing_worker)
            self._sd_printing_thread.start()

    def update_print_job_info(self):
        print_job_info = self._printer.bambu_client.get_device().print_job
        filename: str = print_job_info.get("subtask_name")
        project_file_info = self._printer.file_system.search_by_stem(
            filename, [".3mf", ".gcode.3mf"]
        )
        if project_file_info is None:
            self._log.debug(f"No 3mf file found for {print_job_info}")
            self._print_job = None
            return

        if self._printer.file_system.select_file(filename):
            self._printer.sendOk()

        # fuzzy math here to get print percentage to match BambuStudio
        progress = print_job_info.get("print_percentage")
        self._print_job = PrintJob(project_file_info, 0)
        self._print_job.progress = progress

    def _printing_worker(self):
        if self._print_job is not None:
            while (
                self._printer.is_running
                and self._print_job.file_info is not None
                and self._print_job.file_position < self._print_job.file_info.size
            ):
                self.update_print_job_info()
                self._printer.report_print_job_status()
                time.sleep(3)
                self._printing_lock.wait()
            self._log.debug(
                f"SD File Print finishing: {self._print_job.file_info.file_name}"
            )
        self._printer.change_state(self._printer._state_finished)

    def pause_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.PAUSE):
                self._log.info("print paused")
                self._printer.change_state(self._printer._state_finished)
            else:
                self._log.info("print pause failed")

    def resume_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.RESUME):
                self._log.info("print resumed")
            else:
                self._log.info("print resume failed")

    def cancel_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.change_state(self._printer._state_finished)
            else:
                self._log.info("print cancel failed")

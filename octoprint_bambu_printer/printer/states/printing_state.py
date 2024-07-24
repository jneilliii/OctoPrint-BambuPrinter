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
        self._is_printing = False
        self._print_job: PrintJob | None = None
        self._sd_printing_thread = None

    @property
    def print_job(self):
        return self._print_job

    def init(self):
        self._is_printing = True
        self._printer.update_print_job_info()
        self._start_worker_thread()

    def finalize(self):

        if self._sd_printing_thread is not None and self._sd_printing_thread.is_alive():
            self._is_printing = False
            self._sd_printing_thread.join()
            self._sd_printing_thread = None

    def _start_worker_thread(self):
        if self._sd_printing_thread is None:
            self._is_printing = True
            self._sd_printing_thread = threading.Thread(target=self._printing_worker)
            self._sd_printing_thread.start()

    def _printing_worker(self):
        while (
            self._is_printing
            and self._printer.current_print_job is not None
            and self._printer.current_print_job.file_position
            < self._printer.current_print_job.file_info.size
        ):
            self._printer.update_print_job_info()
            self._printer.report_print_job_status()
            time.sleep(3)

        if self._printer.current_print_job is None:

            self._log.warn("Printing state was triggered with empty print job")
            return

        if (
            self._printer.current_print_job.file_position
            >= self._printer.current_print_job.file_info.size
        ):
            self._finish_print()

    def pause_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.PAUSE):
                self._log.info("print paused")
                self._printer.change_state(self._printer._state_paused)
            else:
                self._log.info("print pause failed")

    def cancel_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.change_state(self._printer._state_finished)
            else:
                self._log.info("print cancel failed")

    def _finish_print(self):
        if self._printer.current_print_job is not None:
            self._log.debug(
                f"SD File Print finishing: {self._printer.current_print_job.file_info.file_name}"
            )

        self._printer.change_state(self._printer._state_idle)

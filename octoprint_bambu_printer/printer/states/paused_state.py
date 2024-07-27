from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octoprint_bambu_printer.printer.bambu_virtual_printer import (
        BambuVirtualPrinter,
    )

import threading

import pybambu.commands
from octoprint.util import RepeatedTimer

from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class PausedState(APrinterState):

    def __init__(self, printer: BambuVirtualPrinter) -> None:
        super().__init__(printer)
        self._pausedLock = threading.Event()
        self._paused_repeated_report = None

    def init(self):
        if not self._pausedLock.is_set():
            self._pausedLock.set()

        self._printer.sendIO("// action:paused")
        self._sendPaused()

    def finalize(self):
        if self._pausedLock.is_set():
            self._pausedLock.clear()
            if self._paused_repeated_report is not None:
                self._paused_repeated_report.join()
                self._paused_repeated_report = None

    def _sendPaused(self):
        if self._printer.current_print_job is None:
            self._log.error("job paused, but no print job available?")
            self._printer.change_state(self._printer._state_printing)
            return
        self._paused_repeated_report = RepeatedTimer(
            interval=3.0,
            function=self._printer.report_print_job_status,
            run_first=True,
            condition=self._pausedLock.is_set,
        )
        self._paused_repeated_report.start()

    def start_new_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.RESUME):
                self._log.info("print resumed")
                self._printer.change_state(self._printer._state_printing)
            else:
                self._log.info("print resume failed")

    def cancel_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.finalize_print_job()
            else:
                self._log.info("print cancel failed")

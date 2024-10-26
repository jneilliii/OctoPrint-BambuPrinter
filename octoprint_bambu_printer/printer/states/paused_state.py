from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octoprint_bambu_printer.printer.bambu_virtual_printer import (
        BambuVirtualPrinter,
    )

import threading

import octoprint_bambu_printer.printer.pybambu.commands
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
        self._printer.start_continuous_status_report(3)

    def finalize(self):
        if self._pausedLock.is_set():
            self._pausedLock.clear()
            if self._paused_repeated_report is not None:
                self._paused_repeated_report.join()
                self._paused_repeated_report = None

    def start_new_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.RESUME):
                self._log.info("print resumed")
            else:
                self._log.info("print resume failed")

    def cancel_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.STOP):
                self._log.info("print cancelled")
                self._printer.finalize_print_job()
            else:
                self._log.info("print cancel failed")

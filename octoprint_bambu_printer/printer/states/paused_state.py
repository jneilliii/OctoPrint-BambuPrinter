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

    def init(self):
        if not self._pausedLock.is_set():
            self._pausedLock.set()

        self._printer.sendIO("// action:paused")
        self._sendPaused()

    def finalize(self):
        if self._pausedLock.is_set():
            self._pausedLock.clear()

    def _sendPaused(self):
        if self._printer.current_print_job is None:
            self._log.warn("job paused, but no print job available?")
            return
        paused_timer = RepeatedTimer(
            interval=3.0,
            function=self._printer.report_print_job_status,
            daemon=True,
            run_first=True,
            condition=self._pausedLock.is_set,
        )
        paused_timer.start()

    def start_new_print(self):
        if self._printer.bambu_client.connected:
            if self._printer.bambu_client.publish(pybambu.commands.RESUME):
                self._log.info("print resumed")
                self._printer.change_state(self._printer._state_printing)
            else:
                self._log.info("print resume failed")

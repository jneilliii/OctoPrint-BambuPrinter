from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octoprint_bambu_printer.printer.bambu_virtual_printer import (
        BambuVirtualPrinter,
    )


class APrinterState:
    def __init__(self, printer: BambuVirtualPrinter) -> None:
        self._log = logging.getLogger(
            "octoprint.plugins.bambu_printer.BambuPrinter.states"
        )
        self._printer = printer

    def init(self):
        pass

    def finalize(self):
        pass

    def handle_gcode(self, gcode):
        self._log.debug(f"{self.__class__.__name__} gcode execution disabled")

    def update_print_job_info(self):
        self._log_skip_state_transition("start_new_print")

    def start_new_print(self):
        self._log_skip_state_transition("start_new_print")

    def pause_print(self):
        self._log_skip_state_transition("pause_print")

    def cancel_print(self):
        self._log_skip_state_transition("cancel_print")

    def resume_print(self):
        self._log_skip_state_transition("resume_print")

    def _log_skip_state_transition(self, method):
        self._log.debug(
            f"skipping {self.__class__.__name__} state transition for '{method}'"
        )

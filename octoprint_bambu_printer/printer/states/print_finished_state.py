from __future__ import annotations

from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class PrintFinishedState(APrinterState):
    def init(self):
        if self._printer.current_print_job is not None:
            self._printer.current_print_job.progress = 100
        self._finishSdPrint()

    def _finishSdPrint(self):
        if self._printer.is_running:
            self._printer.sendIO("Done printing file")
            self._selectedSdFilePos = 0
            self._selectedSdFileSize = 0
            self._sdPrinting = False
            self._sdPrintStarting = False
            self._sdPrinter = None

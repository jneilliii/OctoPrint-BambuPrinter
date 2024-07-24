from __future__ import annotations

from dataclasses import dataclass
from octoprint_bambu_printer.printer.remote_sd_card_file_list import FileInfo


@dataclass
class PrintJob:
    file_info: FileInfo
    file_position: int

    @property
    def progress(self):
        if self.file_info.size is None:
            return 100
        return 100 * self.file_position / self.file_info.size

    @progress.setter
    def progress(self, value):
        self.file_position = int(self.file_info.size * value / 100)

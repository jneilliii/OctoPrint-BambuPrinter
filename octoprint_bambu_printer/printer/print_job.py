from __future__ import annotations

from dataclasses import dataclass
from octoprint_bambu_printer.printer.file_system.remote_sd_card_file_list import (
    FileInfo,
)


@dataclass
class PrintJob:
    file_info: FileInfo
    progress: int
    remaining_time: int
    current_layer: int
    total_layers: int

    @property
    def file_position(self):
        if self.file_info.size is None:
            return 0
        return int(self.file_info.size * self.progress / 100)

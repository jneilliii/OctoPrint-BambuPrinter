from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from octoprint.util.files import unix_timestamp_to_m20_timestamp


@dataclass(frozen=True)
class FileInfo:
    dosname: str
    path: Path
    size: int
    date: datetime

    @property
    def file_name(self):
        return self.path.name

    @property
    def timestamp(self) -> float:
        return self.date.timestamp()

    @property
    def timestamp_m20(self) -> str:
        return unix_timestamp_to_m20_timestamp(int(self.timestamp))

    def get_gcode_info(self) -> str:
        return f'{self.dosname} {self.size} {self.timestamp_m20} "{self.file_name}"'

    def to_dict(self):
        return asdict(self)

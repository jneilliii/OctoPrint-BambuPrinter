from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .file_info import FileInfo

from octoprint.util import get_formatted_size, get_formatted_datetime


@dataclass(frozen=True)
class BambuTimelapseFileInfo:
    bytes: int
    date: str | None
    name: str
    size: str
    thumbnail: str
    timestamp: float
    url: str

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_file_info(file_info: FileInfo):
        return BambuTimelapseFileInfo(
            bytes=file_info.size,
            date=get_formatted_datetime(file_info.date),
            name=file_info.file_name,
            size=get_formatted_size(file_info.size),
            thumbnail=f"/plugin/bambu_printer/thumbnail/{file_info.path.stem}.jpg",
            timestamp=file_info.timestamp,
            url=f"/plugin/bambu_printer/timelapse/{file_info.file_name}",
        )

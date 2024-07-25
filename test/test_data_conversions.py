from __future__ import annotations
from datetime import datetime
from pathlib import Path

from octoprint.util import get_formatted_size, get_formatted_datetime
from octoprint_bambu_printer.printer.file_system.bambu_timelapse_file_info import (
    BambuTimelapseFileInfo,
)
from octoprint_bambu_printer.printer.file_system.file_info import FileInfo


def test_timelapse_info_valid():
    file_name = "part.mp4"
    file_size = 1000
    file_date = datetime(2020, 1, 1)
    file_timestamp = file_date.timestamp()

    file_info = FileInfo(file_name, Path(file_name), file_size, file_date)
    timelapse = BambuTimelapseFileInfo.from_file_info(file_info)

    assert timelapse.to_dict() == {
        "bytes": file_size,
        "date": get_formatted_datetime(datetime.fromtimestamp(file_timestamp)),
        "name": file_name,
        "size": get_formatted_size(file_size),
        "thumbnail": "/plugin/bambu_printer/thumbnail/"
        + file_name.replace(".mp4", ".jpg").replace(".avi", ".jpg"),
        "timestamp": file_timestamp,
        "url": f"/plugin/bambu_printer/timelapse/{file_name}",
    }

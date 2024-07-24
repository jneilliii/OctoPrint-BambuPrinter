from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime
import itertools
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
import logging.handlers

from octoprint.util import get_dos_filename
from octoprint.util.files import unix_timestamp_to_m20_timestamp

from .ftpsclient import IoTFTPSClient


@dataclass(frozen=True)
class FileInfo:
    dosname: str
    path: Path
    size: int
    timestamp: str

    @property
    def file_name(self):
        return self.path.name

    def get_log_info(self):
        return f'{self.dosname} {self.size} {self.timestamp} "{self.file_name}"'

    def to_dict(self):
        return asdict(self)


class RemoteSDCardFileList:

    def __init__(self, settings) -> None:
        self._settings = settings
        self._file_alias_cache = {}
        self._file_data_cache = {}
        self._selected_file_info: FileInfo | None = None
        self._logger = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")

    @property
    def selected_file(self):
        return self._selected_file_info

    @property
    def has_selected_file(self):
        return self._selected_file_info is not None

    def _get_ftp_file_info(
        self, ftp: IoTFTPSClient, file_path: Path, existing_files: list[str]
    ):
        file_size = ftp.ftps_session.size(file_path.as_posix())
        date_str = ftp.ftps_session.sendcmd(f"MDTM {file_path.as_posix()}").replace(
            "213 ", ""
        )
        filedate = (
            datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
        file_name = file_path.name.lower()
        dosname = get_dos_filename(file_name, existing_filenames=existing_files).lower()
        return FileInfo(
            dosname,
            file_path,
            file_size if file_size is not None else 0,
            unix_timestamp_to_m20_timestamp(int(filedate)),
        )

    def _scan_ftp_file_list(
        self, ftp, files: list[str], existing_files: list[str]
    ) -> Iterator[FileInfo]:
        for entry in files:
            file_info = self._get_ftp_file_info(ftp, Path(entry), existing_files)

            yield file_info
            existing_files.append(file_info.file_name)
            existing_files.append(file_info.dosname)

    def _get_existing_files_info(self):
        ftp = self._connect_ftps_server()

        file_list = []
        file_list.extend(ftp.list_files("", ".3mf"))
        file_list.extend(ftp.list_files("cache/", ".3mf"))

        existing_files = []
        return list(self._scan_ftp_file_list(ftp, file_list, existing_files))

    def _connect_ftps_server(self):
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        ftp = IoTFTPSClient(f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True)
        return ftp

    def _get_cached_file_data(self, file_name: str) -> FileInfo | None:
        self._logger.debug(f"get data for path: {file_name}")

        # replace if name is an alias
        file_name = Path(file_name).name
        file_name = self._file_alias_cache.get(file_name, file_name)

        data = self._file_data_cache.get(file_name, None)
        self._logger.debug(f"get file data: {data}")
        return data

    def get_all_files(self):
        self._update_existing_files_info()
        self._logger.debug(f"get_all_files return: {self._file_data_cache}")
        return list(self._file_data_cache.values())

    def _update_existing_files_info(self):
        file_info_list = self._get_existing_files_info()
        self._file_alias_cache = {
            info.dosname: info.file_name for info in file_info_list
        }
        self._file_data_cache = {info.file_name: info for info in file_info_list}

    def _get_cached_data_by_suffix(self, file_stem: str, allowed_suffixes: list[str]):
        for suffix in allowed_suffixes:
            file_data = self._get_cached_file_data(
                Path(file_stem).with_suffix(suffix).as_posix()
            )
            if file_data is not None:
                return file_data
        return None

    def get_data_by_suffix(self, file_stem: str, allowed_suffixes: list[str]):
        file_data = self._get_cached_data_by_suffix(file_stem, allowed_suffixes)
        if file_data is None:
            self._update_existing_files_info()
            file_data = self._get_cached_data_by_suffix(file_stem, allowed_suffixes)
        return file_data

    def select_file(self, file_path: str, check_already_open: bool = False) -> bool:
        self._logger.debug(
            f"_selectSdFile: {file_path}, check_already_open={check_already_open}"
        )
        file_name = Path(file_path).name
        file_info = self._get_cached_file_data(file_name)
        if file_info is None:
            self._logger.error(f"{file_name} open failed")
            return False

        if (
            self._selected_file_info is not None
            and self._selected_file_info.path == file_info.path
            and check_already_open
        ):
            return True

        self._selected_file_info = file_info
        return True

    def delete_file(self, file_path: str) -> None:
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])

        file_info = self._get_cached_file_data(file_path)
        if file_info is not None:
            ftp = IoTFTPSClient(
                f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
            )
            try:
                if ftp.delete_file(str(file_info.path)):
                    self._logger.debug(f"{file_path} deleted")
                else:
                    raise Exception("delete failed")
            except Exception as e:
                self._logger.debug(f"Error deleting file {file_path}")

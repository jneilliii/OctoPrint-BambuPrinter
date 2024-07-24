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
        return self.path.name.lower()

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
        self, ftp: IoTFTPSClient, ftp_path, file_path: Path, existing_files: list[str]
    ):
        file_size = ftp.ftps_session.size(ftp_path)
        date_str = ftp.ftps_session.sendcmd(f"MDTM {ftp_path}").replace("213 ", "")
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
            ftp_path = Path(entry)
            file_info = self._get_ftp_file_info(ftp, entry, ftp_path, existing_files)

            yield file_info
            existing_files.append(file_info.file_name)

    def _get_existing_files_info(self):
        ftp = self._connect_ftps_server()

        all_files_info: list[FileInfo] = []
        existing_files = []

        filelist = ftp.list_files("", ".3mf") or []
        all_files_info.extend(self._scan_ftp_file_list(ftp, filelist, existing_files))

        filelist_cache = ftp.list_files("cache/", ".3mf") or []
        all_files_info.extend(
            self._scan_ftp_file_list(ftp, filelist_cache, existing_files)
        )

        return all_files_info

    def _connect_ftps_server(self):
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        ftp = IoTFTPSClient(str(host), 990, "bblp", str(access_code), ssl_implicit=True)
        return ftp

    def _get_file_data(self, file_path: str) -> FileInfo | None:
        self._logger.debug(f"_getSdFileData: {file_path}")
        file_name = Path(file_path).name.lower()
        full_file_name = self._file_alias_cache.get(file_name, None)
        if full_file_name is not None:
            data = self._file_data_cache.get(file_name, None)
        self._logger.debug(f"_getSdFileData: {data}")
        return data

    def get_all_files(self):
        self._update_existing_files_info()
        self._logger.debug(f"_getSdFiles return: {self._file_data_cache}")
        return list(self._file_data_cache.values())

    def _update_existing_files_info(self):
        file_info_list = self._get_existing_files_info()
        self._file_alias_cache = {
            info.dosname: info.file_name for info in file_info_list
        }
        self._file_data_cache = {info.file_name: info for info in file_info_list}

    def search_by_stem(self, file_stem: str, allowed_suffixes: list[str]):
        for file_name in self._file_data_cache:
            file_data = self._get_file_data(file_name)
            if file_data is None:
                continue
            file_path = file_data.path
            if file_path.stem == file_stem and any(
                s in allowed_suffixes for s in file_path.suffixes
            ):
                return file_data
        return None

    def select_file(self, file_path: str, check_already_open: bool = False) -> bool:
        self._logger.debug(
            f"_selectSdFile: {file_path}, check_already_open={check_already_open}"
        )
        file_name = Path(file_path).name
        file_info = self._get_file_data(file_name)
        if file_info is None:
            file_info = self._get_file_data(file_name)
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
        self._logger.info(
            f"File opened: {self._selected_file_info.file_name}  Size: {self._selected_file_info.size}"
        )
        return True

    def delete_file(self, file_path: str) -> None:
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])

        file_info = self._get_file_data(file_path)
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

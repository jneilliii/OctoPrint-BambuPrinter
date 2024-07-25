from __future__ import annotations

import datetime
from pathlib import Path
from typing import Iterable, Iterator
import logging.handlers

from octoprint.util import get_dos_filename

from octoprint_bambu_printer.printer.file_system.cached_file_view import CachedFileView

from .ftps_client import IoTFTPSClient, IoTFTPSConnection
from .file_info import FileInfo


class RemoteSDCardFileList:

    def __init__(self, settings) -> None:
        self._settings = settings
        self._file_alias_cache = {}
        self._file_data_cache = {}
        self._selected_project_file: FileInfo | None = None
        self._logger = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")
        self._project_files_view = (
            CachedFileView(self).with_filter("", ".3mf").with_filter("cache/", ".3mf")
        )
        self._timelapse_files_view = CachedFileView(self)
        if self._settings.get(["device_type"]) in ["X1", "X1C"]:
            self._timelapse_files_view.with_filter("timelapse/", ".mp4")
        else:
            self._timelapse_files_view.with_filter("timelapse/", ".avi")

    @property
    def selected_file(self):
        return self._selected_project_file

    @property
    def has_selected_file(self):
        return self._selected_project_file is not None

    @property
    def project_files(self):
        return self._project_files_view

    def remove_file_selection(self):
        self._selected_project_file = None

    def get_all_project_files(self):
        self._project_files_view.update()
        files = self._project_files_view.get_all_cached_info()
        self._logger.debug(f"get project files return: {files}")
        return files

    def get_all_timelapse_files(self):
        self._timelapse_files_view.update()
        files = self._timelapse_files_view.get_all_cached_info()
        self._logger.debug(f"get timelapse files return: {files}")
        return files

    def select_project_file(self, file_path: str) -> bool:
        self._logger.debug(f"_selectSdFile: {file_path}")
        file_name = Path(file_path).name
        file_info = self._project_files_view.get_cached_file_data(file_name)
        if file_info is None:
            self._logger.error(f"{file_name} open failed")
            return False

        self._selected_project_file = file_info
        return True

    def delete_file(self, file_path: str) -> None:
        file_info = self._project_files_view.get_cached_file_data(file_path)
        if file_info is not None:
            try:
                with self.get_ftps_client() as ftp:
                    if ftp.delete_file(str(file_info.path)):
                        self._logger.debug(f"{file_path} deleted")
                    else:
                        raise RuntimeError(f"Deleting file {file_path} failed")
            except Exception as e:
                self._logger.exception(e)

    def list_files(
        self,
        folder: str,
        extensions: str | list[str] | None,
        ftp: IoTFTPSConnection,
        existing_files=None,
    ):
        if existing_files is None:
            existing_files = []

        return list(
            self.get_file_info_for_names(
                ftp, ftp.list_files(folder, extensions), existing_files
            )
        )

    def _get_ftp_file_info(
        self,
        ftp: IoTFTPSConnection,
        file_path: Path,
        existing_files: list[str] | None = None,
    ):
        file_size = ftp.ftps_session.size(file_path.as_posix())
        date_str = ftp.ftps_session.sendcmd(f"MDTM {file_path.as_posix()}").replace(
            "213 ", ""
        )
        date = datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S").replace(
            tzinfo=datetime.timezone.utc
        )
        file_name = file_path.name.lower()
        dosname = get_dos_filename(file_name, existing_filenames=existing_files).lower()
        return FileInfo(
            dosname,
            file_path,
            file_size if file_size is not None else 0,
            date,
        )

    def get_file_info_for_names(
        self,
        ftp: IoTFTPSConnection,
        files: Iterable[Path],
        existing_files: list[str] | None = None,
    ) -> Iterator[FileInfo]:
        if existing_files is None:
            existing_files = []

        for entry in files:
            file_info = self._get_ftp_file_info(ftp, entry, existing_files)
            yield file_info
            existing_files.append(file_info.file_name)
            existing_files.append(file_info.dosname)

    def get_ftps_client(self):
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        return IoTFTPSClient(
            f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
        )

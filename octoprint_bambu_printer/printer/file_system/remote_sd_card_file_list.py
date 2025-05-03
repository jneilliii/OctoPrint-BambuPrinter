from __future__ import annotations

import datetime
from pathlib import Path
from typing import Iterable, Iterator
import logging.handlers

from octoprint.util import get_dos_filename

from .ftps_client import IoTFTPSClient, IoTFTPSConnection
from .file_info import FileInfo


class RemoteSDCardFileList:

    def __init__(self, settings) -> None:
        self._settings = settings
        self._selected_project_file: FileInfo | None = None
        self._logger = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")

    def delete_file(self, file_path: Path) -> None:
        try:
            with self.get_ftps_client() as ftp:
                if ftp.delete_file(file_path.as_posix()):
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
        file_size = ftp.get_file_size(file_path.as_posix())
        date = ftp.get_file_date(file_path.as_posix())
        file_name = file_path.name.lower()
        dosname = get_dos_filename(file_name, existing_filenames=existing_files, **{'max_power': 3}).lower()
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
            try:
                file_info = self._get_ftp_file_info(ftp, entry, existing_files)
                yield file_info
                existing_files.append(file_info.file_name)
                existing_files.append(file_info.dosname)
            except Exception as e:
                self._logger.exception(e, exc_info=False)

    def get_ftps_client(self):
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        return IoTFTPSClient(
            f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
        )

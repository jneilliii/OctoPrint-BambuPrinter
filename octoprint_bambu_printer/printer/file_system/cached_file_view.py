from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from octoprint_bambu_printer.printer.file_system.remote_sd_card_file_list import (
        RemoteSDCardFileList,
    )

from dataclasses import dataclass, field
from pathlib import Path
from octoprint_bambu_printer.printer.file_system.file_info import FileInfo


@dataclass
class CachedFileView:
    file_system: RemoteSDCardFileList
    folder_view: set[tuple[str, str | list[str] | None]] = field(default_factory=set)
    on_update: Callable[[], None] | None = None

    def __post_init__(self):
        self._file_alias_cache: dict[str, str] = {}
        self._file_data_cache: dict[str, FileInfo] = {}

    def with_filter(
        self, folder: str, extensions: str | list[str] | None = None
    ) -> "CachedFileView":
        self.folder_view.add((folder, extensions))
        return self

    def list_all_views(self):
        existing_files: list[str] = []
        result: list[FileInfo] = []

        with self.file_system.get_ftps_client() as ftp:
            for filter in self.folder_view:
                result.extend(self.file_system.list_files(*filter, ftp, existing_files))
        return result

    def update(self):
        file_info_list = self.list_all_views()
        self._update_file_list_cache(file_info_list)
        if self.on_update:
            self.on_update()

    def _update_file_list_cache(self, files: list[FileInfo]):
        self._file_alias_cache = {info.dosname: info.file_name for info in files}
        self._file_data_cache = {info.file_name: info for info in files}

    def get_all_info(self):
        self.update()
        return self.get_all_cached_info()

    def get_all_cached_info(self):
        return list(self._file_data_cache.values())

    def get_file_by_suffix(self, file_stem: str, allowed_suffixes: list[str]):
        if file_stem == "":
            return None

        file_data = self._get_file_by_suffix_cached(file_stem, allowed_suffixes)
        if file_data is None:
            self.update()
            file_data = self._get_file_by_suffix_cached(file_stem, allowed_suffixes)
        return file_data

    def get_cached_file_data(self, file_name: str) -> FileInfo | None:
        file_name = Path(file_name).name
        file_name = self._file_alias_cache.get(file_name, file_name)
        return self._file_data_cache.get(file_name, None)

    def _get_file_by_suffix_cached(self, file_stem: str, allowed_suffixes: list[str]):
        for suffix in allowed_suffixes:
            file_data = self.get_cached_file_data(
                Path(file_stem).with_suffix(suffix).as_posix()
            )
            if file_data is not None:
                return file_data
        return None

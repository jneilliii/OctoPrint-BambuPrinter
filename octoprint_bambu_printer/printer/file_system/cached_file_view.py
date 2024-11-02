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
    folder_view: dict[tuple[str, str | list[str] | None], None] = field(
        default_factory=dict
    )  # dict preserves order, but set does not. We use only dict keys as storage
    on_update: Callable[[], None] | None = None

    def __post_init__(self):
        self._file_alias_cache: dict[str, str] = {}
        self._file_data_cache: dict[str, FileInfo] = {}

    def with_filter(
        self, folder: str, extensions: str | list[str] | None = None
    ) -> "CachedFileView":
        self.folder_view[(folder, extensions)] = None
        return self

    def list_all_views(self):
        existing_files: list[str] = []
        result: list[FileInfo] = []

        with self.file_system.get_ftps_client() as ftp:
            for filter in self.folder_view.keys():
                result.extend(self.file_system.list_files(*filter, ftp, existing_files))
        return result

    def update(self):
        file_info_list = self.list_all_views()
        self._update_file_list_cache(file_info_list)
        if self.on_update:
            self.on_update()

    def _update_file_list_cache(self, files: list[FileInfo]):
        self._file_alias_cache = {info.dosname: info.path.as_posix() for info in files}
        self._file_data_cache = {info.path.as_posix(): info for info in files}

    def get_all_info(self):
        self.update()
        return self.get_all_cached_info()

    def get_all_cached_info(self):
        return list(self._file_data_cache.values())

    def get_file_data(self, file_path: str | Path) -> FileInfo | None:
        file_data = self.get_file_data_cached(file_path)
        if file_data is None:
            self.update()
            file_data = self.get_file_data_cached(file_path)
        return file_data

    def get_file_data_cached(self, file_path: str | Path) -> FileInfo | None:
        if isinstance(file_path, str):
            file_path = Path(file_path).as_posix().strip("/")
        else:
            file_path = file_path.as_posix().strip("/")

        if file_path not in self._file_data_cache:
            file_path = self._file_alias_cache.get(file_path, file_path)
        return self._file_data_cache.get(file_path, None)

    def get_file_by_stem(self, file_stem: str, allowed_suffixes: list[str]):
        if file_stem == "":
            return None

        file_stem = Path(file_stem).with_suffix("").stem
        file_data = self._get_file_by_stem_cached(file_stem, allowed_suffixes)
        if file_data is None:
            self.update()
            file_data = self._get_file_by_stem_cached(file_stem, allowed_suffixes)
        return file_data

    def _get_file_by_stem_cached(self, file_stem: str, allowed_suffixes: list[str]):
        for file_path_str in list(self._file_data_cache.keys()) + list(self._file_alias_cache.keys()):
            file_path = Path(file_path_str)
            if file_stem == file_path.with_suffix("").stem and any(
                suffix in allowed_suffixes for suffix in file_path.suffixes
            ):
                return self.get_file_data_cached(file_path)
        return None

# -*- coding: utf-8 -*-
"""
Simple cloud browser example using GDAL virtual filesystem and PyQt6.
This file implements the model for the tree view and other helper functions.
@author: Robin Princeley
"""
from pathlib import Path, PurePosixPath
import traceback
from typing import Any, List, Dict
from PyQt6.QtCore import QObject, pyqtSlot, QAbstractItemModel, QModelIndex, QRunnable, pyqtSignal, Qt
from PyQt6.QtGui import QIcon
from datetime import datetime
import os


from osgeo import gdal
from humanfriendly import format_size


extension_mapping: Dict[str, str] = {}


# Create a extension to short name mapping for drivers
def update_extension_mapping() -> bool:
    global extension_mapping
    if len(extension_mapping) != 0:
        return
    count = gdal.GetDriverCount()
    for i in reversed(range(0, count)):
        driver = gdal.GetDriver(i)
        if driver:
            name = driver.ShortName
            metadata = driver.GetMetadata()
            if gdal.DMD_EXTENSION in metadata:
                ext = metadata[gdal.DMD_EXTENSION].lower()
                extension_mapping[ext] = name
            if gdal.DMD_EXTENSIONS in metadata:
                ext_string = metadata[gdal.DMD_EXTENSIONS]
                if ext_string:
                    ext_string = ext_string.lower()
                    ext_string = ext_string.replace(',', ' ')
                    ext_string = ext_string.replace(';', ' ')
                    tmp_list = ext_string.split()
                    for ext in tmp_list:
                        extension_mapping[ext] = name
    extension_mapping.pop('xml', None)
    extension_mapping.pop('bin', None)
    extension_mapping.pop('txt', None)


# Match exxtention of a url to driver short name
def find_driver_for_url(url: PurePosixPath) -> str:
    global extension_mapping
    ext = url.suffix
    if not ext:
        return None
    if ext[:1] == '.':
        ext = ext[1:]
    return extension_mapping.get(ext)


# Get a blob given path to container/bucket
def find_a_object_in_container(url: str) -> str:
    dir_handle = gdal.OpenDir(url, -1, [''])
    if dir_handle is None:
        return None
    entry = gdal.GetNextDirEntry(dir_handle)
    while entry is not None:
        if not entry.IsDirectory():
            obj_url = url + '/' + entry.name
            gdal.CloseDir(dir_handle)
            return obj_url
        entry = gdal.GetNextDirEntry(dir_handle)
    gdal.CloseDir(dir_handle)
    return None


# Structure to repersent a VSI filesystem object
class VSIItem(object):
    def __init__(self, url: PurePosixPath, parent) -> None:
        self._url = url
        self._parent = parent
        self._children = []
        self._metadata = {}
        self._is_dir = False
        self._mtime = 0
        self._size = 0
        # stat() to figure out details
        stat = gdal.VSIStatL(
            str(url), gdal.VSI_STAT_EXISTS_FLAG | gdal.VSI_STAT_NATURE_FLAG)
        if stat:
            self._mtime = stat.mtime
            self._size = stat.size
            self._is_dir = stat.IsDirectory()
        # See if the extension matches a driver
        self._driver = find_driver_for_url(self._url)
        self._row = 0
        self._attempted_listing = False
        self._attempted_metadata = False

    # Populate one level of children in on this node
    def populate_children(self) -> bool:
        if not self._attempted_listing:
            self._attempted_listing = True
            if self._is_dir:
                dir_handle = gdal.OpenDir(str(self._url), 0)
                if dir_handle is None:
                    return False
                entry = gdal.GetNextDirEntry(dir_handle)
                row = 0
                while entry is not None:
                    child = VSIItem(self._url / entry.name, self)
                    child._parent = self
                    child._row = row
                    self._children.append(child)
                    row = row + 1
                    entry = gdal.GetNextDirEntry(dir_handle)
                gdal.CloseDir(dir_handle)
        return len(self._children) != 0

    # Fetch object metadata
    def get_metadata(self) -> bool:
        if not self._attempted_metadata:
            self._attempted_metadata = True
            if not self.isDir():
                for domain in ['HEADERS', 'TAGS', 'STATUS', 'ACL', 'METADATA']:
                    meta = gdal.GetFileMetadata(str(self._url), domain)
                    if meta and len(meta) != 0:
                        self._metadata = self._metadata | meta
        return len(self._metadata) != 0

    def data(self, column) -> str | int | None:
        if column == 0:
            return self._url.name
        elif column == 1:
            if self._is_dir:
                return 'Folder'
            elif self._driver:
                return '{driver} Dataset'.format(driver=self._driver)
            else:
                return 'Object'
        elif column == 2:
            if self._is_dir:
                return self.childCount()
            else:
                return format_size(self._size)
        elif column == 3:
            if self._mtime != 0:
                return str(datetime.fromtimestamp(self._mtime))

    def columnCount(self) -> int:
        return 4

    def childCount(self) -> int:
        return len(self._children)

    def child(self, row):
        if row >= 0 and row < self.childCount():
            return self._children[row]

    def parent(self):
        return self._parent

    def row(self) -> int:
        return self._row

    def canFetchMore(self) -> bool:
        if self._is_dir and not self._attempted_listing:
            return True
        return False

    def isDir(self) -> bool:
        return self._is_dir

    def metadata(self) -> Dict[str, str]:
        return self._metadata

    def canFetchMetadata(self) -> bool:
        if not self._is_dir and not self._attempted_metadata:
            return True
        return False

    def isDriverKnown(self) -> bool:
        return self._driver is not None

    def url(self) -> PurePosixPath:
        return self._url


class VSIFileSystemModel(QAbstractItemModel):
    def __init__(self, url: PurePosixPath) -> None:
        super(VSIFileSystemModel, self).__init__()
        self._root = VSIItem(url, None)

    def rowCount(self, index):
        if index.isValid() and index.internalPointer():
            return index.internalPointer().childCount()
        return self._root.childCount()

    def index(self, row, column, parent=None) -> QModelIndex:
        if not parent or not parent.isValid():
            node = self._root
        else:
            node = parent.internalPointer()

        if not QAbstractItemModel.hasIndex(self, row, column, parent):
            return QModelIndex()

        child = node.child(row)
        if child:
            return QAbstractItemModel.createIndex(self, row, column, child)
        else:
            return QModelIndex()

    def parent(self, index) -> QModelIndex:
        if index.isValid():
            p = index.internalPointer().parent()
            if p:
                return QAbstractItemModel.createIndex(self, p.row(), 0, p)
        return QModelIndex()

    def columnCount(self, index) -> int:
        return self._root.columnCount()

    def data(self, index, role) -> str | int | Qt.AlignmentFlag | QIcon | None:
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return node.data(index.column())
        elif role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 2:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        elif role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            if node.isDir():
                return QIcon('ui:folder-blue.svg')
            elif node.isDriverKnown():
                return QIcon('ui:image-x-generic.svg')
            else:
                return QIcon('ui:unknown.svg')
        return None

    def headerData(self, section, orientation, role) -> str | Qt.AlignmentFlag | Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if section == 0:
                return 'Name'
            elif section == 1:
                return 'Type'
            elif section == 2:
                return 'Size'
            elif section == 3:
                return 'Date / Time'
        elif role == Qt.ItemDataRole.TextAlignmentRole and section == 2:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return super().headerData(section, orientation, role)

    def canFetchMore(self, parent) -> bool:
        if not parent or not parent.isValid():
            node = self._root
        else:
            node = parent.internalPointer()
        return node.canFetchMore()

    def fetchMore(self, parent) -> None:
        if not parent or not parent.isValid():
            node = self._root
        else:
            node = parent.internalPointer()
        if node.canFetchMore():
            node.populate_children()
            count = len(node._children)
            self.beginInsertRows(parent, 0, count)
            self.endInsertRows()

    def hasChildren(self, parent) -> bool:
        if not parent or not parent.isValid():
            node = self._root
        else:
            node = parent.internalPointer()
        if node.canFetchMore() or node.childCount():
            return True
        return False


# Signals for Sync() worker
class VSISyncSignals(QObject):
    finished = pyqtSignal(str, str, int)
    # progress = pyqtSignal(int)


# Worker thread to perform Sync() asynchronously
class VSISyncWorker(QRunnable):
    def __init__(self, src: str, dest: str, options: List[str] = None):
        super(VSISyncWorker, self).__init__()
        self.src = src
        self.dest = dest
        self.options = options
        self.signals = VSISyncSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = gdal.Sync(self.src, self.dest)
        except:
            traceback.print_exc()
            self.signals.finished.emit(self.src, self.dest, 0)
        else:
            self.signals.finished.emit(self.src, self.dest, result)

    # def progress_cb(self, complete, message, cb_data) -> None:
    #     self.signals.progress.emit(complete)

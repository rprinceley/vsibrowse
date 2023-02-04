# -*- coding: utf-8 -*-
"""
Simple cloud browser example using GDAL virtual filesystem and PyQt6.
@author: Robin Princeley
"""
import json
from pathlib import Path, PurePosixPath
import traceback
from typing import Any, List, Dict
from PyQt6.QtCore import QModelIndex, QThreadPool, QDir, Qt, QTimer
from PyQt6.QtGui import QIcon, QCursor
from PyQt6.QtWidgets import QApplication, QMainWindow, QTableWidgetItem, QMenu, QAbstractScrollArea, QFileDialog, QDialog, QTreeWidgetItem, QLineEdit, QErrorMessage, QMessageBox
from PyQt6 import uic
from datetime import datetime
import os
import msal

from osgeo import gdal

from vsistuff import *


class SSLCertificateDialog(QDialog):
    def __init__(self, parent, ssl_json=None):
        super(SSLCertificateDialog, self).__init__(parent)
        ui_path = os.path.join(os.path.dirname(__file__), 'ui', 'ssldialog.ui')
        uic.loadUi(ui_path, self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if ssl_json:
            root_item = QTreeWidgetItem(self.sslTreeWidget)
            root_item.setText(0, '/')
            self.sslTreeWidget.addTopLevelItem(root_item)
            item_parent = root_item
            for certificate in reversed(ssl_json['Certificates']):
                name = certificate['Subject'].split('/CN=', 1)[1]
                cert_item = QTreeWidgetItem(item_parent)
                cert_item.setText(0, name)
                item_parent = cert_item
                for field in certificate:
                    if field == 'PEM':
                        continue
                    elif field == 'Extensions':
                        ext_root_item = QTreeWidgetItem(cert_item)
                        ext_root_item.setText(0, 'Extensions')
                        extensions = certificate[field]
                        for i in range(len(extensions)):
                            ext_dict = extensions[i]
                            for key, value in ext_dict.items():
                                ext_item = QTreeWidgetItem(ext_root_item)
                                ext_item.setText(0, key)
                                ext_item.setText(1, str(value))
                    else:
                        field_item = QTreeWidgetItem(cert_item)
                        field_item.setText(0, field)
                        field_item.setText(1, str(certificate[field]))
        self.sslTreeWidget.expandAll()
        self.sslTreeWidget.resizeColumnToContents(0)
        self.sslTreeWidget.resizeColumnToContents(1)


class AzureOAuthDialog(QDialog):
    def __init__(self, parent):
        super(AzureOAuthDialog, self).__init__(parent)
        ui_path = os.path.join(os.path.dirname(__file__), 'ui', 'az_oauth.ui')
        uic.loadUi(ui_path, self)
        self.connectButton.setIcon(QIcon('ui:network-acquiring-symbolic.svg'))
        self.cancelButton.setIcon(QIcon('ui:window-close.svg'))
        self.connectButton.clicked.connect(self.onConnect)
        self.cancelButton.clicked.connect(self.reject)

    def onConnect(self) -> None:
        if not self.applicationIDEdit.text():
            QMessageBox.warning(self, 'Missing Application ID',
                                'Please enter an Application ID')
            return
        if not self.authorityEdit.text():
            QMessageBox.warning(self, 'Missing Authority', 'Please enter an Authority')
            return
        if not self.scopeEdit.text():
            QMessageBox.warning(self, 'Missing Scope', 'Please enter a scope')
            return
        if not self.accountEdit.text():
            QMessageBox.warning(self, 'Missing Account', 'Please enter an account')
            return
        if not self.containerEdit.text():
            QMessageBox.warning(self, 'Missing Container', 'Please enter a container')
            return
        self.done(QDialog.DialogCode.Accepted)


class VSIFileSystemBrowser(QMainWindow):
    def __init__(self):
        super(VSIFileSystemBrowser, self).__init__()
        ui_path = os.path.join(os.path.dirname(__file__), 'ui', 'main.ui')
        uic.loadUi(ui_path, self)
        self.url = ''
        self.model = None
        self.threadpool = QThreadPool()
        self.tabWidget.setTabIcon(0, QIcon('ui:help-info-symbolic.svg'))
        self.tabWidget.setTabIcon(1, QIcon('ui:text-x-log.svg'))
        self.refreshButton.setIcon(QIcon('ui:view-refresh-symbolic.svg'))
        self.connectMenu = QMenu(self)
        azureOAuthAction = self.connectMenu.addAction('Connect to Azure (OAuth)')
        azureOAuthAction.triggered.connect(self.connectToAzureOAuth)
        azureOAuthAction.setIcon(QIcon('ui:oauth_logo.svg'))
        self.connectButton.setIcon(QIcon('ui:folder-remote-symbolic.svg'))
        self.connectButton.setMenu(self.connectMenu)
        self.treeView.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.treeView.customContextMenuRequested.connect(self.context_menu)
        self.treeView.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.propertiesTable.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.mainSplitter.setStretchFactor(0, 1)
        self.mainSplitter.setStretchFactor(1, 0)
        self.refreshButton.clicked.connect(self.refreshButtonClicked)
        self.urlEdit.returnPressed.connect(self.refreshButtonClicked)
        self.sslAction = self.urlEdit.addAction(
            QIcon('ui:channel-insecure-symbolic.svg'), QLineEdit.ActionPosition.LeadingPosition)
        self.sslAction.triggered.connect(self.sslActionTriggered)
        self.sslJSON = None
        self.msalApp = None
        self.oAuthTimer = QTimer()
        self.oAuthTimer.timeout.connect(self.refreshAzureOAuth)
        self.oAuthTimer.setSingleShot(True)
        self.oAuthScope = None
        self.oAuthBucketPath = None

    def populate(self, url: str) -> None:
        self.url = url
        self.model = VSIFileSystemModel(PurePosixPath(url))
        self.treeView.setModel(self.model)
        self.urlEdit.setText(url)
        self.treeView.setSortingEnabled(False)
        self.treeView.selectionModel().currentChanged.connect(self.currentChanged)
        self.treeView.resizeColumnToContents(0)
        self.treeView.resizeColumnToContents(1)

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:
        if current.isValid():
            self.propertiesTable.setRowCount(0)
            node = current.internalPointer()
            metadata = node.metadata()
            if not metadata or len(metadata) == 0:
                if node.canFetchMetadata():
                    node.get_metadata()
                    metadata = node.metadata()
            if metadata and len(metadata):
                for name, value in metadata.items():
                    pos = self.propertiesTable.rowCount()
                    self.propertiesTable.insertRow(pos)
                    self.propertiesTable.setItem(
                        pos, 0, QTableWidgetItem(name))
                    self.propertiesTable.setItem(
                        pos, 1, QTableWidgetItem(value))
            self.propertiesTable.resizeColumnsToContents()

    def updateSSLStatus(self) -> None:
        if not self.url:
            return
        if not 'GetSSLCertificates' in dir(gdal): # Esri builds only
            return
        object_url = find_a_object_in_container(self.url)
        if object_url:
            http_url = gdal.GetActualURL(object_url)
            if http_url:
                cert_list = gdal.GetSSLCertificates(http_url, ['ACTION=OCSP_CHECK'])
                if cert_list:
                    self.sslJSON = json.loads(cert_list)
                    if 'OCSP' in self.sslJSON and self.sslJSON['OCSP'] == 'passed':
                        self.sslAction.setIcon(
                            QIcon('ui:channel-secure-validated-symbolic.svg'))
                        self.sslAction.setToolTip(
                            'Connection is secure, passed OCSP status check.')
                    elif 'Verify' in self.sslJSON and self.sslJSON['Verify'] == 'passed':
                        self.sslAction.setIcon(QIcon('ui:channel-secure-symbolic.svg'))
                        self.sslAction.setToolTip(
                            'Connection is secure, passed SSL certificate validation check.')
                    else:
                        self.sslAction.setIcon(
                            QIcon('ui:channel-insecure-symbolic.svg'))
                        self.sslAction.setToolTip('Connection appears to be insecure.')

    def sslActionTriggered(self) -> None:
        if self.sslJSON:
            dialog = SSLCertificateDialog(self, self.sslJSON)
            dialog.show()

    def refreshButtonClicked(self) -> None:
        if self.url != self.urlEdit.text():
            self.populate(self.urlEdit.text())
            self.updateSSLStatus()

    def connectToAzureOAuth(self) -> None:
        dialog = AzureOAuthDialog(self)
        app_id = os.getenv('GDALTEST_AZ_APP_ID')
        if app_id is not None:
            dialog.applicationIDEdit.setText(app_id)
        authority = os.getenv('GDALTEST_AZ_AUTHORITY')
        if authority is not None:
            dialog.authorityEdit.setText(authority)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.msalApp = msal.PublicClientApplication(client_id=dialog.applicationIDEdit.text(),
                                                        authority=dialog.authorityEdit.text())
            result = self.msalApp.acquire_token_interactive([dialog.scopeEdit.text()])
            if not 'access_token' in result:
                error_dialog = QErrorMessage(self)
                error_string = 'Failed to get access token: '
                if 'error' in result:
                    error_string += result['error']
                if 'error_description' in result:
                    error_string += ' - ' + result['error_description']
                error_dialog.showMessage(error_string)
            else:
                self.oAuthScope = dialog.scopeEdit.text()
                access_token = result['access_token']
                vsi_path = '/vsiaz/'
                if dialog.typeComboBox.currentText() == 'ADLS':
                    vsi_path = '/vsiadls/'
                vsi_path += dialog.containerEdit.text()
                gdal.SetPathSpecificOption(
                    vsi_path, 'AZURE_STORAGE_ACCOUNT', dialog.accountEdit.text())
                gdal.SetPathSpecificOption(
                    vsi_path, 'AZURE_STORAGE_ACCESS_TOKEN', access_token)
                self.populate(vsi_path)
                self.updateSSLStatus()
                time_left = int(result['expires_in']) - 60
                self.oAuthTimer.setInterval(time_left * 1000)

    def refreshAzureOAuth(self) -> None:
        if not self.msalApp:
            return
        if not self.oAuthScope or self.oAuthScope == '':
            return
        if self.url != self.oAuthBucketPath:
            return
        result = self.msalApp.acquire_token_silent(self.oAuthScope, account=None)
        if not result:
            access_token = result['access_token']
            gdal.SetPathSpecificOption(
                self.oAuthBucketPath, 'AZURE_STORAGE_ACCESS_TOKEN', access_token)
            time_left = int(result['expires_in']) - 60
            self.oAuthTimer.setInterval(time_left * 1000)

    def context_menu(self) -> None:
        menu = QMenu()
        open = menu.addAction(
            QIcon('ui:document-save-symbolic.svg'), 'Download')
        open.triggered.connect(self.download)
        cursor = QCursor()
        menu.exec(cursor.pos())

    def download(self) -> None:
        index = self.treeView.currentIndex()
        if index.isValid():
            node = index.internalPointer()
            if not node.isDir():
                default_filename = node.url().name
                dest_path = QFileDialog.getSaveFileName(
                    self, 'Download object', default_filename)
                dest = dest_path[0]
                worker = VSISyncWorker(str(node.url()), dest, None)
                worker.signals.finished.connect(self.syncFinished)
                # worker.signals.progress.connect(self.progress_fn)
                self.logTextEdit.append('Starting a thread to run Sync(), copying {src} to {dest}.'.format(
                    src=str(node.url()), dest=dest))
                self.threadpool.start(worker)

    def errorHandler(self, eErrClass, err_no, msg: str) -> None:
        if msg:
            self.logTextEdit.append(msg)

    def syncFinished(self, src: str, dest: str, result: int) -> None:
        if result == 1:
            self.logTextEdit.append(
                'Thread running Sync(), copying {src} to {dest} was successful.'.format(src=src, dest=dest))
        else:
            self.logTextEdit.append(
                'Thread running Sync(), copying {src} to {dest} failed.'.format(src=src, dest=dest))


class BrowserApplication(QApplication):
    def __init__(self, argv: List[str]) -> None:
        super().__init__(argv)

    def connectGDAL(self, browser: VSIFileSystemBrowser) -> None:
        gdal.PushErrorHandler(browser.errorHandler)


if __name__ == '__main__':
    QDir.addSearchPath('ui', os.fspath(Path(__file__).resolve().parent / 'ui'))
    update_extension_mapping()
    url = PurePosixPath('/vsis3') / 'gdal-io-test'
    app = BrowserApplication([])
    browser = VSIFileSystemBrowser()
    app.connectGDAL(browser)
    browser.populate(str(url))
    browser.updateSSLStatus()
    browser.show()
    app.exec()

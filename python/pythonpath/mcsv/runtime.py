import copy
import itertools
import logging
import os
import shutil
import tempfile
from pathlib import Path

import uno
import unohelper
from com.sun.star.awt import XCallback
from com.sun.star.document import XDocumentEventListener
from com.sun.star.util import XModifyListener

from . import core, ui


LOG = logging.getLogger("mcsv")
if not LOG.handlers:
    try:
        suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
        handler = logging.FileHandler(
            str(Path(tempfile.gettempdir()) / ("mcsv-" + suffix + ".log")),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    except OSError:
        handler = logging.NullHandler()
    LOG.addHandler(handler)

LOG.setLevel(logging.INFO)
SESSIONS = []


def properties(values):
    return {p.Name: p.Value for p in values}


def local_path(url):
    if not str(url).startswith("file:"):
        raise core.FormatError(
            "MCSV поддерживает локальные файлы. Выберите папку на компьютере."
        )
    return str(Path(uno.fileUrlToSystemPath(url)).absolute())


def sheets(doc):
    return [doc.Sheets.getByIndex(i) for i in range(doc.Sheets.Count)]


def code_name(sheet):
    value = sheet.CodeName
    if not value:
        value = "mcsv_" + core.new_id()
        sheet.CodeName = value
    return value


def sheet_rows(sheet):
    ranges = sheet.queryContentCells(1 | 2 | 4 | 16)
    addresses = ranges.getRangeAddresses()
    if not addresses:
        return
    last_row = max(a.EndRow for a in addresses)
    last_col = max(a.EndColumn for a in addresses)
    for start in range(0, last_row + 1, 1000):
        values = sheet.getCellRangeByPosition(
            0, start, last_col, min(last_row, start + 999)
        ).getDataArray()
        for row in values:
            yield [
                (
                    str(int(v))
                    if isinstance(v, float) and v.is_integer()
                    else "" if v is None else str(v)
                )
                for v in row
            ]


def fill_sheet(sheet, rows):
    source = iter(rows)
    start = 0
    while True:
        chunk = list(itertools.islice(source, 1000))
        if not chunk:
            break
        width = max((len(row) for row in chunk), default=0)
        if start + len(chunk) > sheet.Rows.Count or width > sheet.Columns.Count:
            raise core.FormatError(
                "CSV превышает допустимое число строк или столбцов Calc."
            )
        if width:
            rectangular = tuple(
                tuple(row) + ("",) * (width - len(row)) for row in chunk
            )
            sheet.getCellRangeByPosition(
                0, start, width - 1, start + len(chunk) - 1
            ).setDataArray(rectangular)
        start += len(chunk)


DEFAULT_PROPERTY = "MCSV: разделитель новых листов"
SHEET_PROPERTY = "MCSV: разделитель листа "


def read_property(doc, name, default):
    try:
        properties = doc.DocumentProperties.UserDefinedProperties
    except AttributeError:
        return default
    if properties.getPropertySetInfo().hasPropertyByName(name):
        return properties.getPropertyValue(name)
    return default


def delimiter_value(value):
    return core.validate_delimiter("\t" if value in ("TAB", "\\t") else value)


def document_default(doc, config):
    return delimiter_value(
        read_property(doc, DEFAULT_PROPERTY, core.default_delimiter(config))
    )


def sheet_delimiter(doc, sheet, known, default):
    previous_name = known["name"] if known else sheet.Name
    previous_value = known["delimiter"] if known else default
    if known is None:
        return delimiter_value(default)
    return delimiter_value(
        read_property(doc, SHEET_PROPERTY + previous_name, previous_value)
    )


def sync_properties(doc, config):
    try:
        properties = doc.DocumentProperties.UserDefinedProperties
    except AttributeError:
        return
    modified = doc.isModified()
    values = {DEFAULT_PROPERTY: core.default_delimiter(config)}
    values.update(
        {SHEET_PROPERTY + i["name"]: i["delimiter"] for i in config["sheets"]}
    )
    try:
        for descriptor in properties.getPropertySetInfo().getProperties():
            if (
                descriptor.Name.startswith(SHEET_PROPERTY)
                and descriptor.Name not in values
            ):
                properties.removeProperty(descriptor.Name)
        for key, value in values.items():
            value = "TAB" if value == "\t" else value
            if properties.getPropertySetInfo().hasPropertyByName(key):
                if properties.getPropertyValue(key) != value:
                    properties.setPropertyValue(key, value)
            else:
                properties.addProperty(key, 128, value)
    finally:
        doc.setModified(modified)


def get_session(doc):
    for session in SESSIONS:
        if session.doc == doc:
            return session
    return None


def hidden(doc):
    return bool(properties(doc.getArgs()).get("Hidden", False))


class Session(unohelper.Base, XModifyListener, XDocumentEventListener, XCallback):
    def __init__(self, ctx, doc, path=None, config=None, mapping=None):
        self.ctx, self.doc = ctx, doc
        self.path, self.config = path, config
        self.mapping = mapping or {}
        self.busy = False
        self.queued = False
        self.disposed = False
        self.pending = None
        self.pending_snapshot = None
        self.pending_relocation = None
        self.pending_reload = False
        self.reload_required = False
        self.relocate_to = None
        self.baseline = core.fingerprint(path, config) if path and config else None
        self.async_service = ui.service(ctx, "com.sun.star.awt.AsyncCallback")
        SESSIONS.append(self)
        doc.addModifyListener(self)
        doc.addDocumentEventListener(self)

    def active(self):
        if self.disposed or self.path is None or self.config is None:
            return False
        try:
            return local_path(
                self.doc.URL
            ) == self.path and self.doc.URL.lower().endswith(".mcsv")
        except Exception:
            return False

    def parent(self):
        if self.doc is None:
            return None
        controller = self.doc.getCurrentController()
        return controller.Frame.ContainerWindow if controller else None

    def modified(self, event):
        if self.relocate_to and not self.busy and not self.queued and self.active():
            self.queued = True
            self.async_service.addCallback(self, None)

    def notify(self, data):
        self.queued = False
        if (
            self.disposed
            or self.busy
            or not self.active()
            or not (self.relocate_to or self.reload_required)
        ):
            return
        self.busy = True
        try:
            if self.relocate_to:
                self.relocate_bundle()
            if self.reload_required:
                self.reload_saved_document()
        except Exception as exc:
            LOG.exception("Post-save relocation or reload failed")
            ui.message(
                self.ctx,
                "Данные сохранены, но завершить перенос или переоткрытие не удалось.\n"
                "Оформление на экране может отличаться от сохранённого CSV.\n"
                + str(exc),
                self.parent(),
            )
        finally:
            self.busy = False

    def reload_saved_document(self):
        if self.doc.isModified():
            LOG.info("Reload deferred: newer unsaved edits exist")
            return
        core.check_unchanged(self.path, self.baseline, self.config)
        controller = self.doc.getCurrentController()
        if controller is None:
            raise core.FormatError("Нет окна документа для переоткрытия.")
        frame = controller.Frame
        dispatcher = ui.service(self.ctx, "com.sun.star.frame.DispatchHelper")
        self.reload_required = False
        try:
            result = dispatcher.executeDispatch(frame, ".uno:Reload", "", 0, ())
            if result is False:
                raise core.FormatError("LibreOffice отклонил переоткрытие документа.")
        except Exception:
            if not self.disposed:
                self.reload_required = True
            raise
        LOG.info("Saved MCSV reload dispatched")

    def relocate_bundle(self):
        target = Path(self.relocate_to)
        source = Path(self.path)
        self.relocate_to = None
        if target.exists():
            raise core.FormatError(
                "Папка назначения уже содержит конфигурацию: " + str(target)
            )
        saved_bytes = source.read_bytes()
        source_config = copy.deepcopy(self.config)
        source_baseline = self.baseline
        core.check_unchanged(source, source_baseline, source_config)
        target.parent.mkdir(parents=True, exist_ok=True)

        def prop(name, value):
            p = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            p.Name, p.Value = name, value
            return p

        self.doc.storeAsURL(
            uno.systemPathToFileUrl(str(target)),
            (
                prop("FilterName", "MCSV Workspace"),
                prop("Overwrite", False),
                prop("FilterData", (prop("BundleRelocation", True),)),
            ),
        )
        if local_path(self.doc.URL) != str(target) or not target.is_file():
            raise core.FormatError(
                "Не удалось переключить документ на папку набора. Исходный файл сохранён."
            )
        source_root = core.data_root(source, source_config)
        source_lock = core.SaveLock(
            source_root.parent / ".locks" / (source_root.name + ".lock")
        )
        try:
            core.check_unchanged(source, source_baseline, source_config)
            core.check_data_aliases(source, source_root, source_config)
            target_config = core.read_manifest(target)
            target_root = core.data_root(target, target_config)
            target_lock = core.SaveLock(
                target_root.parent / ".locks" / (target_root.name + ".lock")
            )
            try:
                core.check_unchanged(target, self.baseline, target_config)
                baseline = dict(self.baseline)
                listed = {item["file"] for item in source_config["sheets"]}
                for file in source_root.iterdir():
                    if file.name in listed:
                        continue
                    if core.filename_key(file.name) in {
                        core.filename_key(p.name) for p in target_root.iterdir()
                    }:
                        raise core.ConflictError(
                            "Файл уже существует в папке назначения: " + file.name
                        )
                    shutil.copy2(file, target_root / file.name)
                    baseline["extra:" + file.name] = source_baseline[
                        "extra:" + file.name
                    ]
                core.check_unchanged(target, baseline, target_config)
                self.baseline = baseline
                history = core.backup_root(source)
                if history.exists():
                    destination = core.backup_root(target)
                    destination.mkdir(parents=True, exist_ok=True)
                    for archive in history.glob("*.zip"):
                        dest = destination / archive.name
                        if dest.exists() and core.file_digest(dest) != core.file_digest(
                            archive
                        ):
                            raise core.ConflictError(
                                "Совпало имя резервной копии: " + archive.name
                            )
                        shutil.copy2(archive, dest)
                    core.prune_backups(target)
                core.check_unchanged(source, source_baseline, source_config)
                if source.read_bytes() != saved_bytes:
                    raise core.ConflictError(
                        "Исходная конфигурация изменилась; она не удалена."
                    )
                if source_config["version"] != 2:
                    raise core.FormatError(
                        "Старый исходный набор оставлен без удаления."
                    )
                source.unlink()
                shutil.rmtree(source_root)
                if history.exists():
                    for archive in history.glob("*.zip"):
                        archive.unlink()
                    try:
                        history.rmdir()
                    except OSError:
                        pass
                core.remove_empty_service_dirs(source.parent)
            finally:
                target_lock.release()
        finally:
            source_lock.release()
        LOG.info("MCSV document relocated into its bundle folder")

    def documentEventOccured(self, event):
        if (
            event.EventName in ("OnSaveDone", "OnSaveAsDone", "OnSaveToDone")
            and self.pending
        ):
            path, config, mapping = self.pending
            try:
                if self.pending_snapshot:
                    self.pending_snapshot.finish()
                if local_path(self.doc.URL) == path:
                    self.path, self.config = path, config
                    self.mapping.update(mapping)
                    self.baseline = (
                        self.pending_snapshot.saved_baseline
                        if self.pending_snapshot
                        else core.fingerprint(path, config)
                    )
                    try:
                        sync_properties(self.doc, config)
                    except Exception:
                        LOG.exception(
                            "Could not update CSV settings in document properties"
                        )
                    self.relocate_to = self.pending_relocation
                    self.reload_required = self.reload_required or self.pending_reload
                    if (
                        (self.relocate_to or self.reload_required)
                        and not self.busy
                        and not self.queued
                    ):
                        self.queued = True
                        self.async_service.addCallback(self, None)
            except Exception:
                LOG.exception("Applying saved state or pruning backups failed")
            self.pending = None
            self.pending_snapshot = None
            self.pending_relocation = None
            self.pending_reload = False
        elif event.EventName in ("OnSaveFailed", "OnSaveAsFailed", "OnSaveToFailed"):
            if self.pending_snapshot:
                self.pending_snapshot.abort()
            self.pending = None
            self.pending_snapshot = None
            self.pending_relocation = None
            self.pending_reload = False
        elif event.EventName in ("OnLoadFinished", "OnViewCreated"):
            self.modified(event)

    def disposing(self, event):
        if self.pending_snapshot:
            self.pending_snapshot.release()
            try:
                core.recover(self.pending_snapshot.path)
            except Exception:
                LOG.exception("Deferred save recovery failed")
        self.pending = None
        self.pending_snapshot = None
        self.pending_relocation = None
        self.pending_reload = False
        self.reload_required = False
        self.disposed = True
        if self in SESSIONS:
            SESSIONS.remove(self)
        self.doc = None

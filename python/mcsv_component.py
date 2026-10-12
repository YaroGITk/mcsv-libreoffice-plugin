"""Registered UNO import/export filter for the .mcsv document type."""
import copy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'pythonpath'))
import uno
import unohelper
from com.sun.star.document import XFilter, XImporter, XExporter, XExtendedFilterDetection
from com.sun.star.lang import XServiceInfo
from mcsv import core, ui, runtime

TYPE = 'org.csvworkspace.mcsv'
FILTER = 'MCSV Workspace'
IMPLEMENTATION = 'org.csvworkspace.MCSVFilter'
SERVICES = ('com.sun.star.document.ImportFilter', 'com.sun.star.document.ExportFilter',
            'com.sun.star.document.ExtendedTypeDetection')


class MCSVFilter(unohelper.Base, XFilter, XImporter, XExporter, XExtendedFilterDetection, XServiceInfo):
    def __init__(self, ctx):
        self.ctx = ctx
        self.doc = None
        self.mode = None
        self.cancelled = False
        runtime.LOG.info('MCSV filter instance created')

    def getImplementationName(self):
        return IMPLEMENTATION

    def supportsService(self, name):
        return name in SERVICES

    def getSupportedServiceNames(self):
        return SERVICES

    def setTargetDocument(self, doc):
        self.doc, self.mode = doc, 'import'
        runtime.LOG.info('MCSV target document attached for import')

    def setSourceDocument(self, doc):
        self.doc, self.mode = doc, 'export'
        runtime.LOG.info('MCSV source document attached for export')

    def cancel(self):
        self.cancelled = True

    def detect(self, descriptor):
        values = runtime.properties(descriptor)
        url = values.get('URL', '')
        return (TYPE if str(url).lower().endswith('.mcsv') else '', descriptor)

    def filter(self, descriptor):
        args = {}
        runtime.LOG.info('MCSV filter entered: mode=%s', self.mode)
        try:
            args = runtime.properties(descriptor)
            
            if self.cancelled:
                return False
            
            path = runtime.local_path(args.get('URL', ''))
            if self.mode == 'import':
                return self.load(path, args)
            if self.mode == 'export':
                return self.save(path, args)
            
            raise core.FormatError('Не задан документ для фильтра MCSV.')
        except Exception as exc:
            runtime.LOG.exception('MCSV filter failed')
            try:
                if not args.get('Hidden', False) and self.doc is not None and not runtime.hidden(self.doc):
                    ui.message(self.ctx, str(exc))
            except Exception:
                runtime.LOG.exception('Could not display MCSV error dialog')
            return False

    def load(self, path, args):
        core.recover(path)
        was_empty = False
        config = core.read_manifest(path)
        if config is None:
            was_empty = True
            if args.get('ReadOnly', False):
                raise core.FormatError('Нельзя создать конфигурацию в пустом файле, открытом только для чтения.')
            config = core.initialize_empty(path)

        baseline = core.fingerprint(path, config)
        for item in config['sheets']:
            if baseline.get('csv:' + item['file']) is None:
                raise core.FormatError('Отсутствует CSV листа «' + item['name'] + '». Перенесите папку набора целиком или восстановите резервную копию.')

        self.doc.lockControllers()
        try:
            old_names = list(self.doc.Sheets.getElementNames())
            temporary = '_mcsv_' + core.new_id()[:12]
            self.doc.Sheets.insertNewByName(temporary, 0)
            for name in old_names:
                self.doc.Sheets.removeByName(name)
            mapping = {}
            for index, item in enumerate(config['sheets']):
                if self.cancelled:
                    return False
                self.doc.Sheets.insertNewByName(item['name'], index)
                sheet = self.doc.Sheets.getByName(item['name'])
                sheet.CodeName = 'mcsv_' + core.new_id()
                runtime.fill_sheet(sheet, core.iter_sheet(core.csv_path(path, config, item), item))
                mapping[runtime.code_name(sheet)] = copy.deepcopy(item)
            self.doc.Sheets.removeByName(temporary)
        finally:
            self.doc.unlockControllers()
        core.check_unchanged(path, baseline, config)
        runtime.sync_properties(self.doc, config)
        session = runtime.Session(self.ctx, self.doc, path, config, mapping)
        session.baseline = baseline
        if was_empty and core.bundle_path(path) != Path(path):
            session.relocate_to = str(core.bundle_path(path))
        return True

    def save(self, path, args):
        session = runtime.get_session(self.doc)
        is_copy = session is None or session.path != path
        config = copy.deepcopy(session.config) if session and session.config else core.new_config()
        if is_copy: config['data_directory'] = Path(path).name
        mapping = {}
        rows = []
        headless = args.get('Hidden', False) or runtime.hidden(self.doc)
        filter_data = runtime.properties(args.get('FilterData', ()))
        default_delimiter = runtime.document_default(self.doc, config)
        config['sheets'] = []
        if 'DefaultDelimiter' in filter_data:
            default_delimiter = runtime.delimiter_value(filter_data['DefaultDelimiter'])
        if session is None and not headless:
            controller = self.doc.getCurrentController()
            default_delimiter = ui.workspace_settings(self.ctx, default_delimiter, controller.Frame.ContainerWindow if controller else None)
            if default_delimiter is None: return False
        config['default_delimiter'] = default_delimiter
        if session:
            session.busy = True
        snapshot = None
        try:
            used_keys = set()
            for sheet in runtime.sheets(self.doc):
                if self.cancelled:
                    return False
                key = runtime.code_name(sheet)
                if key in used_keys:
                    sheet.CodeName = 'mcsv_' + core.new_id()
                    key = runtime.code_name(sheet)
                used_keys.add(key)
                known = session.mapping.get(key) if session else None
                item = copy.deepcopy(known) if known else core.new_sheet(sheet.Name, default_delimiter)
                item['name'] = sheet.Name
                item['delimiter'] = runtime.sheet_delimiter(self.doc, sheet, known, default_delimiter)
                config['sheets'].append(item)
                mapping[key] = item
                rows.append(runtime.sheet_rows(sheet))
            if session and not is_copy:
                core.check_unchanged(path, session.baseline, session.config)
            relocation = None
            if is_copy and not filter_data.get('BundleRelocation', False) and core.bundle_path(path) != Path(path):
                relocation = core.bundle_path(path)
                if relocation.exists():
                    raise core.FormatError('В папке набора уже есть файл: ' + str(relocation))
            snapshot = core.stage_snapshot(path, config, rows, expected=session.baseline if session and not is_copy else None)
            raw = core.encode(snapshot.data)
            if self.cancelled:
                snapshot.abort()
                return False
            snapshot.install()
            stream = args.get('OutputStream')
            if stream:
                stream.writeBytes(uno.ByteSequence(raw))
                stream.flush()
            else:
                core.atomic_write(path, raw)
            mapping = {key: copy.deepcopy(item) for key, item in zip(mapping, snapshot.data['sheets'])}
            if session is None:
                session = runtime.Session(self.ctx, self.doc)
            session.pending = (path, snapshot.data, mapping)
            session.pending_snapshot = snapshot
            session.pending_relocation = str(relocation) if relocation else None
            session.pending_reload = is_copy and not filter_data.get('BundleRelocation', False)
            runtime.LOG.info('MCSV export completed: %d sheets', len(snapshot.data['sheets']))

            return True
        except Exception:
            if snapshot:
                snapshot.abort()
            raise
        finally:
            if session:
                session.busy = False


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MCSVFilter, IMPLEMENTATION, SERVICES)

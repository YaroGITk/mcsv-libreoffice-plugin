import codecs
import copy
import csv
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath

FORMAT = "mcsv"
VERSION = 1
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ID_RE = re.compile(r"^[a-f0-9]{32}$")


class FormatError(ValueError):
    pass


def new_id():
    return uuid.uuid4().hex


def validate_name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 31:
        raise FormatError("Имя листа должно содержать от 1 до 31 символа.")
    if any(c in name for c in ":\\/?*[]") or name.startswith("'") or name.endswith("'"):
        raise FormatError(
            "Недопустимые символы в имени листа: : \\ / ? * [ ] или апостроф по краям."
        )
    if any(ord(c) < 32 for c in name):
        raise FormatError("Имя листа содержит управляющий символ.")
    return name


def validate_delimiter(value):
    if not isinstance(value, str) or len(value) != 1 or value in '\r\n\x00"':
        raise FormatError(
            "Разделитель — один символ, кроме кавычки, переноса строки и NUL."
        )
    return value


def new_sheet(name="Лист1", delimiter=";"):
    return {
        "id": new_id(),
        "name": validate_name(name),
        "delimiter": validate_delimiter(delimiter),
        "encoding": "utf-8",
    }


def new_config():
    return {"format": FORMAT, "version": VERSION, "id": new_id(), "sheets": []}


def validate(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FormatError("Это не конфигурация MCSV.")
    if type(data.get("version")) is not int or data["version"] != VERSION:
        raise FormatError("Неподдерживаемая версия MCSV.")
    if not isinstance(data.get("id"), str) or not ID_RE.fullmatch(data["id"]):
        raise FormatError("Некорректный идентификатор конфигурации.")
    sheets = data.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise FormatError("Конфигурация должна содержать хотя бы один лист.")
    ids, names, paths = set(), set(), set()
    for item in sheets:
        if not isinstance(item, dict):
            raise FormatError("Некорректное описание листа.")
        sid = item.get("id")
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid) or sid in ids:
            raise FormatError("Некорректный или повторяющийся идентификатор листа.")
        ids.add(sid)
        name = validate_name(item.get("name"))
        if name.casefold() in names:
            raise FormatError("Повторяющееся имя листа: " + name)
        names.add(name.casefold())
        validate_delimiter(item.get("delimiter"))
        try:
            codecs.lookup(item["encoding"])
        except (KeyError, TypeError, LookupError):
            raise FormatError("Неизвестная кодировка листа: " + name)
        rel = item.get("file")
        if not isinstance(rel, str) or "\\" in rel or "\x00" in rel:
            raise FormatError("Некорректный путь CSV.")
        parts = PurePosixPath(rel).parts
        if (
            len(parts) < 3
            or parts[:2] != (".mcsv", data["id"])
            or ".." in parts
            or "." in parts
            or not rel.endswith(".csv")
        ):
            raise FormatError("CSV должен находиться внутри .mcsv/" + data["id"])
        if rel in paths:
            raise FormatError("Один CSV назначен нескольким листам.")
        paths.add(rel)
    return data


def csv_path(manifest, data, item):
    parent = Path(manifest).absolute().parent.resolve()
    expected = parent / ".mcsv" / data["id"]
    if (parent / ".mcsv").is_symlink() or expected.is_symlink():
        raise FormatError("Каталог данных не должен быть символической ссылкой.")
    target = parent.joinpath(*PurePosixPath(item["file"]).parts).resolve()
    try:
        target.relative_to(expected)
    except ValueError:
        raise FormatError("CSV находится за пределами каталога конфигурации.")
    return target


def decode(raw):
    if len(raw) > MAX_MANIFEST_BYTES:
        raise FormatError("Файл конфигурации слишком большой.")
    try:
        text = raw.decode("utf-8-sig")
        if not text.strip():
            return None
        return validate(json.loads(text))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormatError("Повреждён JSON конфигурации: " + str(exc)) from exc


def encode(data):
    validate(data)
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_manifest(path):
    with open(path, "rb") as f:
        return decode(f.read(MAX_MANIFEST_BYTES + 1))


def read_sheet(path, item):
    try:
        with open(path, "r", encoding=item["encoding"], newline="") as f:
            return list(
                csv.reader(f, delimiter=item["delimiter"], quotechar='"', strict=True)
            )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise FormatError(
            "Не удалось прочитать лист «{}»: {}\n{}".format(item["name"], path, exc)
        ) from exc


def read_workspace(path, data=None):
    data = read_manifest(path) if data is None else validate(data)
    if data is None:
        return None, []
    return data, [
        read_sheet(csv_path(path, data, item), item) for item in data["sheets"]
    ]


def csv_bytes(rows, item):
    buf = io.StringIO(newline="")
    writer = csv.writer(
        buf,
        delimiter=validate_delimiter(item["delimiter"]),
        quotechar='"',
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerows(rows)
    return buf.getvalue().encode(item["encoding"], errors="strict")


def atomic_write(path, raw):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Snapshot:
    def __init__(self, data, directory):
        self.data = data
        self.directory = directory

    def abort(self):
        if self.directory is not None:
            shutil.rmtree(self.directory)
            self.directory = None


def hide_data_directory(path):
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    kernel.SetFileAttributesW.restype = wintypes.BOOL
    attrs = kernel.GetFileAttributesW(str(path))
    if attrs == 0xFFFFFFFF or not kernel.SetFileAttributesW(
        str(path), (attrs & ~0x80) | 0x2
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def stage_snapshot(manifest, config, rows_by_sheet):
    data = copy.deepcopy(config)
    if len(data["sheets"]) != len(rows_by_sheet):
        raise FormatError("Количество листов не совпадает с данными.")
    parent = Path(manifest).absolute().parent
    root = parent / ".mcsv" / data["id"]

    for item in data["sheets"]:
        item["file"] = ".mcsv/{}/check/{}.csv".format(data["id"], item["id"])
    validate(data)
    for item in data["sheets"]:
        csv_path(manifest, data, item)
    root.mkdir(parents=True, exist_ok=True)
    hide_data_directory(parent / ".mcsv")
    generation = root / new_id()
    generation.mkdir()
    try:
        for item, rows in zip(data["sheets"], rows_by_sheet):
            item["file"] = ".mcsv/{}/{}/{}.csv".format(
                data["id"], generation.name, item["id"]
            )
            target = csv_path(manifest, data, item)
            raw = csv_bytes(rows, item)
            with open(target, "xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        validate(data)
        return Snapshot(data, generation)
    except BaseException:
        shutil.rmtree(generation)
        raise


def save_workspace(path, config, rows):
    snapshot = stage_snapshot(path, config, rows)
    try:
        atomic_write(path, encode(snapshot.data))
    except BaseException:
        snapshot.abort()
        raise
    return snapshot.data


def initialize_empty(path):

    if read_manifest(path) is not None:
        raise FormatError("Конфигурация уже существует.")
    data = new_config()
    data["sheets"].append(new_sheet())
    return save_workspace(path, data, [[]])

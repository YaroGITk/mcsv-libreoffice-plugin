import copy
import csv
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import legacy
from .guard import ConflictError, SaveLock
from .legacy import (
    FormatError,
    atomic_write,
    csv_bytes,
    hide_data_directory,
    new_id,
    read_sheet,
    validate_delimiter,
    validate_name,
)

MAX_MANIFEST_BYTES = legacy.MAX_MANIFEST_BYTES
VERSION = 2
BACKUP_LIMIT = 3


def filename_key(value):
    return unicodedata.normalize("NFC", value).casefold()


def new_config():
    return {"format": "mcsv", "version": VERSION, "sheets": []}


def new_sheet(name="Лист1", delimiter=";"):
    return {
        "name": validate_name(name),
        "file": filename(name),
        "delimiter": validate_delimiter(delimiter),
        "encoding": "utf-8",
    }


def filename(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "Лист"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"} | {
        "COM" + str(i) for i in range(1, 10)
    } | {"LPT" + str(i) for i in range(1, 10)}:
        value = "_" + value
    return value + ".csv"


def normalize(config):
    result = new_config()
    result["default_delimiter"] = default_delimiter(config)
    if "data_directory" in config:
        result["data_directory"] = config["data_directory"]
    used = set()
    for old in config["sheets"]:
        entry = new_sheet(old["name"], old["delimiter"])
        entry["encoding"] = old["encoding"]
        base = filename(old["name"])
        candidate, index = base, 2
        while filename_key(candidate) in used:
            candidate = base[:-4] + "-" + str(index) + ".csv"
            index += 1
        entry["file"] = candidate
        used.add(filename_key(candidate))
        result["sheets"].append(entry)
    return result


def validate(data):
    if isinstance(data, dict) and data.get("version") == 1:
        return legacy.validate(data)
    if (
        not isinstance(data, dict)
        or data.get("format") != "mcsv"
        or type(data.get("version")) is not int
        or data["version"] != 2
    ):
        raise FormatError("Неподдерживаемый формат MCSV.")
    if "data_directory" in data:
        validate_directory(data["data_directory"])
    if "default_delimiter" in data:
        validate_delimiter(data["default_delimiter"])
    if not isinstance(data.get("sheets"), list) or not data["sheets"]:
        raise FormatError("Нужен хотя бы один лист.")
    names, files = set(), set()
    import codecs

    for item in data["sheets"]:
        if not isinstance(item, dict):
            raise FormatError("Некорректное описание листа.")
        name = validate_name(item.get("name"))
        validate_delimiter(item.get("delimiter"))
        try:
            codecs.lookup(item["encoding"])
        except (KeyError, TypeError, LookupError):
            raise FormatError("Неизвестная кодировка.")
        file = item.get("file")
        if (
            not isinstance(file, str)
            or file != Path(file).name
            or any(c in file for c in "/\\:\x00")
            or not file.endswith(".csv")
            or file in (".csv", "..csv")
        ):
            raise FormatError("В конфигурации допустимы только имена CSV, без путей.")
        if filename_key(name) in names or filename_key(file) in files:
            raise FormatError("Повторяющееся имя листа или CSV.")
        names.add(filename_key(name))
        files.add(filename_key(file))
    return data


def decode(raw):
    if len(raw) > MAX_MANIFEST_BYTES:
        raise FormatError("Слишком большой JSON.")
    try:
        text = raw.decode("utf-8-sig")
        return validate(json.loads(text)) if text.strip() else None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormatError("Повреждён JSON: " + str(exc)) from exc


def encode(data):
    validate(data)
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def default_delimiter(config):
    value = config.get("default_delimiter")
    if value is None:
        entries = config.get("sheets", [])
        value = entries[0]["delimiter"] if entries else ";"
    return validate_delimiter(value)


def validate_directory(value):
    if (
        not isinstance(value, str)
        or value != Path(value).name
        or any(c in value for c in "/\\:\x00")
        or value in (".", "..")
        or not value.lower().endswith(".mcsv")
    ):
        raise FormatError("Некорректное имя каталога данных.")
    return value


def peek_manifest(path):
    if not Path(path).exists():
        return None
    with open(path, "rb") as f:
        return decode(f.read(MAX_MANIFEST_BYTES + 1))


def data_root(path, data=None):
    path = Path(path).absolute()
    if data is None:
        try:
            data = peek_manifest(path)
        except FormatError:
            data = None
    directory = (data or {}).get("data_directory", path.name)
    validate_directory(directory)
    hidden = path.parent / ".mcsv"
    root = hidden / directory
    if hidden.is_symlink() or root.is_symlink():
        raise FormatError("Каталог данных не должен быть символической ссылкой.")
    return root


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path, data=None):
    path = Path(path)
    result = {"manifest": file_digest(path) if path.exists() else None}
    data = peek_manifest(path) if data is None else data
    if data:
        for item in data["sheets"]:
            file = csv_path(path, data, item)
            result["csv:" + item["file"]] = file_digest(file) if file.exists() else None
    root = data_root(path, data)
    if root.is_dir():
        known = {i["file"] for i in (data or {}).get("sheets", [])}
        for file in root.iterdir():
            if file.is_symlink() or not file.is_file():
                raise ConflictError(
                    "В каталоге данных есть вложенная папка или ссылка: " + file.name
                )
            if file.name not in known:
                result["extra:" + file.name] = file_digest(file)
    return result


def check_unchanged(path, expected, data=None):
    try:
        actual = fingerprint(path, data)
    except (OSError, FormatError) as exc:
        raise ConflictError("Не удалось проверить файлы набора: " + str(exc)) from exc
    changed = [
        key
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    ]
    if changed:
        labels = [
            "конфигурация .mcsv" if key == "manifest" else key.split(":", 1)[1]
            for key in sorted(changed)
        ]
        raise ConflictError(
            "Файлы изменены вне этого окна: "
            + ", ".join(labels[:8])
            + ". Сохранение отменено. Сохраните свои правки под новым именем через «Сохранить как…», затем переоткройте исходный набор."
        )


def iter_sheet(path, item):
    try:
        with open(path, "r", encoding=item["encoding"], newline="") as f:
            yield from csv.reader(
                f, delimiter=item["delimiter"], quotechar='"', strict=True
            )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise FormatError(
            "Не удалось прочитать лист «{}»: {}".format(item["name"], exc)
        ) from exc


def csv_path(path, data, item):
    if data["version"] == 1:
        return legacy.csv_path(path, data, item)
    validate(data)
    root = data_root(path, data)
    target = root / item["file"]
    if target.is_symlink():
        raise FormatError("CSV не должен быть символической ссылкой.")
    return target


def read_manifest(path):
    recover(path)
    with open(path, "rb") as f:
        return decode(f.read(MAX_MANIFEST_BYTES + 1))


def read_workspace(path, data=None):
    recover(path)
    data = read_manifest(path) if data is None else validate(data)
    if data is None:
        return None, []
    return data, [
        read_sheet(csv_path(path, data, item), item) for item in data["sheets"]
    ]


def backup_root(path):
    current = data_root(path)
    root = current.parent / ".backups" / current.name
    if root.parent.is_symlink() or root.is_symlink():
        raise FormatError("Недопустимый каталог резервных копий.")
    return root


def make_backup(path, config, source):
    if config is None:
        return
    root = backup_root(path)
    root.mkdir(parents=True, exist_ok=True)
    target = root / (str(time.time_ns()) + ".zip")
    tmp = target.with_suffix(".tmp")
    try:
        portable = normalize(config)
        portable["data_directory"] = Path(path).name
        with zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as z:
            z.writestr(Path(path).name, encode(portable))
            for old, new in zip(config["sheets"], portable["sheets"]):
                file = (
                    source / old["file"]
                    if config["version"] == 2
                    else legacy.csv_path(path, config, old)
                )
                z.write(file, ".mcsv/" + Path(path).name + "/" + new["file"])
        os.replace(tmp, target)
        return target
    finally:
        if tmp.exists():
            tmp.unlink()


class Snapshot:
    def __init__(self, path, data, previous, stage, old, lease=None, baseline=None):
        self.path = Path(path)
        self.data = data
        self.previous = previous
        self.stage = Path(stage)
        self.old = Path(old)
        self.root = data_root(path, data)
        self.journal = self.root.parent / (".txn-" + self.root.name + ".json")
        self.installed = False
        self.lease = lease
        self.baseline = baseline
        self.backup = None
        self.noop = False

    def release(self):
        if self.lease:
            self.lease.release()
        self.lease = None

    def install(self):
        if self.baseline is not None:
            check_unchanged(self.path, self.baseline, self.previous)
        if self.noop:
            return
        atomic_write(
            self.journal,
            json.dumps(
                {
                    "data": self.data,
                    "previous": self.previous,
                    "stage": self.stage.name,
                    "old": self.old.name,
                    "backup": self.backup.name if self.backup else None,
                }
            ).encode(),
        )
        if self.root.exists():
            os.replace(self.root, self.old)
        try:
            os.replace(self.stage, self.root)
        except BaseException:
            if self.old.exists():
                os.replace(self.old, self.root)
            self.journal.unlink(missing_ok=True)
            raise
        self.installed = True

    def abort(self):
        try:
            if self.installed and peek_manifest(self.path) == self.data:
                self.finish()
                return
            if self.installed and self.root.exists():
                shutil.rmtree(self.root)
            if self.old.exists():
                os.replace(self.old, self.root)
            if self.stage.exists():
                shutil.rmtree(self.stage)
            self.journal.unlink(missing_ok=True)
            self.installed = False
            if self.backup:
                self.backup.unlink(missing_ok=True)
        finally:
            self.release()

    def finish(self):
        try:
            if peek_manifest(self.path) != self.data:
                raise ConflictError(
                    "Сохранение конфигурации не подтверждено. Данные оставлены для восстановления."
                )
            if self.old.exists():
                shutil.rmtree(self.old)
            if self.stage.exists():
                shutil.rmtree(self.stage)
            self.journal.unlink(missing_ok=True)
            self.installed = False
            cleanup_legacy(self.path, self.previous)
            prune_backups(self.path)
        finally:
            self.release()


def cleanup_legacy(path, previous):
    if not previous or previous.get("version") != 1:
        return
    for manifest in Path(path).parent.glob("*.mcsv"):
        if not manifest.is_file():
            continue
        try:
            other = decode(manifest.read_bytes())
        except (OSError, FormatError):
            return
        if other and other.get("id") == previous["id"]:
            return
    old_root = Path(path).parent / ".mcsv" / previous["id"]
    if old_root.is_dir() and not old_root.is_symlink():
        shutil.rmtree(old_root)


def prune_backups(path):
    root = backup_root(path)
    for old in sorted(root.glob("*.zip"), key=lambda p: p.name, reverse=True)[
        BACKUP_LIMIT:
    ]:
        old.unlink()


def recover(path, lease=None):
    root = data_root(path)
    journal = root.parent / (".txn-" + root.name + ".json")
    if not journal.exists():
        return
    if lease is None:
        lock = SaveLock(root.parent / ".locks" / (root.name + ".lock"))
        try:
            return recover(path, lease=lock)
        finally:
            lock.release()
    if journal.is_symlink():
        raise FormatError("Недопустимый журнал сохранения.")
    info = json.loads(journal.read_text(encoding="utf-8"))
    validate(info["data"])
    if info["previous"] is not None:
        validate(info["previous"])
    if data_root(path, info["data"]) != root or (
        info["previous"] and data_root(path, info["previous"]) != root
    ):
        raise FormatError("Журнал ссылается на другой каталог данных.")
    for key in ("stage", "old"):
        value = info[key]
        expected_prefix = "." + root.name + ("-new-" if key == "stage" else "-old-")
        if (
            not isinstance(value, str)
            or Path(value).name != value
            or not re.fullmatch(re.escape(expected_prefix) + "[a-f0-9]{32}", value)
        ):
            raise FormatError("Повреждён журнал сохранения.")
        if (root.parent / value).is_symlink():
            raise FormatError("Недопустимая ссылка в журнале.")
    snapshot = Snapshot(
        path,
        info["data"],
        info["previous"],
        root.parent / info["stage"],
        root.parent / info["old"],
    )
    if info.get("backup"):
        backup_name = info["backup"]
        if Path(backup_name).name != backup_name or not re.fullmatch(
            r"[0-9]+\.zip", backup_name
        ):
            raise FormatError("Повреждён путь резервной копии.")
        snapshot.backup = backup_root(path) / backup_name
    current = peek_manifest(path)
    if current == snapshot.data:
        snapshot.finish()
    elif current != snapshot.previous:
        raise ConflictError(
            "Конфигурация изменена после прерванного сохранения. Автоматическое восстановление остановлено; файлы сохранены."
        )
    elif snapshot.old.exists():
        snapshot.installed = True
        snapshot.abort()
    elif snapshot.stage.exists():
        snapshot.abort()
    else:
        snapshot.installed = True
        snapshot.abort()


def stage_snapshot(path, config, rows_by_sheet, expected=None):
    existing = peek_manifest(path)
    root = data_root(
        path, existing if existing else {"data_directory": Path(path).name}
    )
    lease = SaveLock(root.parent / ".locks" / (root.name + ".lock"))
    result = None
    try:
        recover(path, lease=lease)
        if expected is not None:
            check_unchanged(path, expected)
        baseline = fingerprint(path)
        previous = peek_manifest(path)
        if previous and data_root(path, previous) != root:
            raise ConflictError(
                "Каталог данных изменён во время начала сохранения. Повторите операцию."
            )
        data = normalize(config)
        data["data_directory"] = root.name
        validate(data)
        if len(data["sheets"]) != len(rows_by_sheet):
            raise FormatError("Количество листов не совпадает.")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        check_data_aliases(path, root, previous)
        root.parent.mkdir(parents=True, exist_ok=True)
        hide_data_directory(root.parent)
        if Path(path).parent.name == Path(path).stem:
            atomic_write(root.parent / ".bundle", b"MCSV bundle\n")
        stage = root.parent / ("." + root.name + "-new-" + new_id())
        old = root.parent / ("." + root.name + "-old-" + new_id())
        stage.mkdir()
        result = Snapshot(path, data, previous, stage, old, lease, baseline)
        for item, rows in zip(data["sheets"], rows_by_sheet):
            with open(
                stage / item["file"], "w", encoding=item["encoding"], newline=""
            ) as f:
                writer = csv.writer(
                    f, delimiter=item["delimiter"], quotechar='"', lineterminator="\n"
                )
                writer.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
        listed = {i["file"] for i in (previous or {}).get("sheets", [])}
        if root.is_dir():
            for file in root.iterdir():
                if file.name not in listed:
                    if filename_key(file.name) in {
                        filename_key(i["file"]) for i in data["sheets"]
                    }:
                        raise ConflictError(
                            "Новый лист конфликтует с существующим файлом: " + file.name
                        )
                    shutil.copy2(file, stage / file.name)
        comparable = copy.deepcopy(previous) if previous else None
        if comparable is not None:
            comparable.pop("saved_at", None)
        unchanged = (
            previous is not None
            and previous.get("version") == 2
            and previous.get("data_directory") == root.name
            and "default_delimiter" in previous
            and comparable == data
            and root.is_dir()
            and all(
                file_digest(root / i["file"]) == file_digest(stage / i["file"])
                for i in data["sheets"]
            )
        )
        check_unchanged(path, baseline, previous)
        if unchanged:
            result.data = copy.deepcopy(previous)
            result.noop = True
        else:
            if previous is not None:
                result.backup = make_backup(path, previous, root)
            data["saved_at"] = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
        result.saved_baseline = {
            "manifest": hashlib.sha256(encode(result.data)).hexdigest()
        }
        for file in stage.iterdir():
            prefix = (
                "csv:" if file.name in {i["file"] for i in data["sheets"]} else "extra:"
            )
            result.saved_baseline[prefix + file.name] = file_digest(file)
        return result
    except BaseException:
        if result:
            result.abort()
        else:
            lease.release()
        raise


def save_workspace(path, config, rows, expected=None):
    snapshot = stage_snapshot(path, config, rows, expected)
    try:
        snapshot.install()
        atomic_write(path, encode(snapshot.data))
    except BaseException:
        snapshot.abort()
        raise
    snapshot.finish()
    return snapshot.data


def initialize_empty(path):
    recover(path)
    raw = Path(path).read_bytes()
    if decode(raw) is not None:
        raise FormatError("Конфигурация уже существует.")
    expected = fingerprint(path)
    expected["manifest"] = hashlib.sha256(raw).hexdigest()
    data = new_config()
    data["sheets"] = [new_sheet()]
    return save_workspace(path, data, [[]], expected=expected)


def bundle_path(path):
    path = Path(path).absolute()
    if path.parent.name == path.stem or (path.parent / ".mcsv" / ".bundle").is_file():
        return path
    return path.parent / path.stem / path.name


def check_data_aliases(path, root, config):
    for other in Path(path).parent.iterdir():
        if (
            other == Path(path)
            or not other.is_file()
            or other.suffix.lower() != ".mcsv"
        ):
            continue
        try:
            candidate = peek_manifest(other)
        except (OSError, FormatError):
            continue
        if (
            candidate
            and candidate.get("version") == 2
            and data_root(other, candidate).resolve() == root.resolve()
        ):
            raise ConflictError(
                "Другой файл ссылается на эти же CSV: "
                + other.name
                + ". Сохраните независимую копию под новым именем в другой папке."
            )


def remove_empty_service_dirs(parent):
    hidden = Path(parent) / ".mcsv"
    if hidden.is_symlink():
        return
    for path in (hidden / ".backups", hidden / ".locks", hidden):
        if path.is_symlink():
            continue
        try:
            path.rmdir()
        except OSError:
            pass

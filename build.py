"""Build an installable OXT with only registered runtime files."""
from pathlib import Path
import ast
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'
OUT = DIST / 'csvworkspace-mcsv.oxt'


def main():
    files = [ROOT / p for p in ('description.xml', 'META-INF/manifest.xml', 'Types.xcu', 'Filters.xcu')]
    files += sorted((ROOT / 'python').rglob('*.py'))
    for path in files:
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        elif path.suffix in ('.xml', '.xcu'):
            ET.parse(path)
    ns = {'oor': 'http://openoffice.org/2001/registry'}
    flags = ET.parse(ROOT / 'Filters.xcu').find(".//prop[@oor:name='Flags']/value", ns)
    if flags is None or not {'IMPORT', 'EXPORT', '3RDPARTYFILTER'} <= set((flags.text or '').split()):
        raise ValueError('MCSV requires IMPORT EXPORT 3RDPARTYFILTER flags for UNO dispatch')
    DIST.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, path.relative_to(ROOT).as_posix())
    print(OUT)


if __name__ == '__main__':
    main()

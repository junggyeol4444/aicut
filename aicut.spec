# PyInstaller spec: one executable that starts the program and opens its screens.
#
# Built and smoke-tested by the `desktop` CI job on windows, macOS and Linux -
# an executable nobody has run is not a deliverable.
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("aicut", includes=["resources/**/*.json", "ui/static/*", "db/*.sql"])

a = Analysis(
    ["aicut/desktop.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=["aicut.cli", "aicut.ui", "aicut.ui.server"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="aicut",
    console=True,            # the window IS the program's status log; see desktop.py
    upx=False,
)
